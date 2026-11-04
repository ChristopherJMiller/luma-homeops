package main

// The hold after a 202 (review I1).
//
// git-pages 0.10.1 answers a webhook 202 after 3 s while the update goes on in
// a goroutine under its own update-timeout (src/pages.go:914-934), and the
// update commits the manifest with no precondition (src/update.go:144). A
// second forward of the same site meanwhile starts a second update from the
// same old manifest, and whichever commits last wins, possibly the older head.
// So after a 202 the site is held: no worker waits on it, but take() skips it
// until the update has ended. A hold ends
//
//   - completed, on evidence that the update ended: the site's health moved
//     from the baseline read before the forward (every update that gets as far
//     as storing commits the manifest, even an unchanged one, and the commit
//     moves its Last-Modified: update.go:73-86, manifest.go:468,
//     backend_s3.go:602-631); or git-pages' webhook update counter shows that
//     every update the relay started has ended (the ledger below: the only
//     evidence for an update that fails after its 202);
//   - aborted, when git-pages restarted after the forward went: the update died
//     with it, so the push is re-queued once (see abort);
//   - timeout, at HoldTimeout, with no evidence;
//   - released, when the drain starts at SIGTERM.
//
// One poller goroutine reads health and the counter, one request at a time,
// never sooner than PollGap after the last one, so polling stays at or under
// 1 req/s however many sites are held.

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// HoldConfig tunes the hold. Zero fields take defaultHold's.
type HoldConfig struct {
	Timeout      time.Duration // a hold's deadline after the 202
	PollGap      time.Duration // the least time between two poll requests
	PollJitter   time.Duration // up to this much more, at random
	HealthFirst  time.Duration // a held site's first health read after the 202
	HealthEvery  time.Duration // then this often (plus up to a fifth more)
	CounterEvery time.Duration // the update counter's reads while anything is held
	LedgerQuiet  time.Duration // an update's end reaches the counter within this
	LedgerWarmup time.Duration // a predecessor pod's updates may run this long after start
}

var defaultHold = HoldConfig{
	Timeout:      HoldTimeout,
	PollGap:      time.Second,
	PollJitter:   time.Second,
	HealthFirst:  2 * time.Second,
	HealthEvery:  10 * time.Second,
	CounterEvery: 3 * time.Second,
	LedgerQuiet:  30 * time.Second,
	// The old pod gets SIGTERM PreStopSleep after this one is Ready, drains for
	// up to DrainTimeout, and git-pages runs its last update for up to
	// HoldTimeout; a minute covers readiness.
	LedgerWarmup: PreStopSleep + DrainTimeout + HoldTimeout + time.Minute,
}

func (rl *Relay) holdConfig() HoldConfig {
	c, d := rl.Hold, defaultHold
	for _, f := range []struct{ v, d *time.Duration }{
		{&c.Timeout, &d.Timeout}, {&c.PollGap, &d.PollGap}, {&c.PollJitter, &d.PollJitter},
		{&c.HealthFirst, &d.HealthFirst}, {&c.HealthEvery, &d.HealthEvery}, {&c.CounterEvery, &d.CounterEvery},
		{&c.LedgerQuiet, &d.LedgerQuiet}, {&c.LedgerWarmup, &d.LedgerWarmup},
	} {
		if *f.v == 0 {
			*f.v = *f.d
		}
	}
	return c
}

func (rl *Relay) checkTimeout() time.Duration {
	if rl.CheckTimeout == 0 {
		return SiteCheckTimeout
	}
	return rl.CheckTimeout
}

func jitter(d time.Duration) time.Duration {
	if d <= 0 {
		return 0
	}
	return rand.N(d)
}

// answer is what git-pages' reply to a forward says of the update.
type answer int

const (
	ansNone    answer = iota // refused before any update started
	ansDone                  // the update ran and has ended
	ansRunning               // 202: the update goes on
	ansUnknown               // no answer, or one the relay does not know
)

// classify reads git-pages 0.10.1's webhook answer. Every 4xx comes before the
// update starts (src/pages.go:833-912); the update's own results are 200
// created/replaced/unchanged/deleted, 503 "update error", 504 "update timeout"
// (:936-950), and 202 (:931). Anything else, such as a 200 with no body (an
// error ServePages does not map, :1016-1031), is unknown, which the ledger
// treats as an update that may be running.
func classify(status int, body []byte) answer {
	first, _, _ := strings.Cut(string(body), "\n")
	first = strings.TrimSpace(first)
	switch {
	case status == http.StatusAccepted:
		return ansRunning
	case status == http.StatusOK && (first == "created" || first == "replaced" || first == "unchanged" || first == "deleted"):
		return ansDone
	case status == http.StatusServiceUnavailable && strings.HasPrefix(first, "update error"):
		return ansDone
	case status == http.StatusGatewayTimeout && first == "update timeout":
		return ansDone
	case status >= 400 && status < 500:
		return ansNone
	default:
		return ansUnknown
	}
}

// siteHealth is git-pages' answer to GET <site>/.git-pages/health.
type siteHealth struct {
	err      error
	status   int
	known    bool // git-pages said whether the site exists (200 or 404)
	ok       bool // it does: its health answer, "ok" with the manifest's Last-Modified
	lm, etag string
}

// movedFrom reports evidence that an update ended since base was read: the
// site appeared, went, or has a new Last-Modified or ETag. Without a known
// base, or without a known answer now, there is none.
func (h siteHealth) movedFrom(base siteHealth) bool {
	if !base.known || !h.known {
		return false
	}
	return h.ok != base.ok || (h.ok && (h.lm != base.lm || h.etag != base.etag))
}

func (h siteHealth) String() string {
	switch {
	case h.ok:
		return "ok " + h.lm
	case h.known:
		return "no site"
	default:
		return "unknown"
	}
}

// readHealth GETs the site's health in-cluster: /<repo>/.git-pages/health, or
// /.git-pages/health for a root site, with Host <owner>.<domain>
// (src/pages.go:228-240). Cache-Control: no-cache makes git-pages reload a
// manifest older than its 60 s site cache (backend_s3.go:516-521); a commit
// invalidates the cached one anyway (:631).
func (rl *Relay) readHealth(ctx context.Context, t Target) siteHealth {
	ctx, cancel := context.WithTimeout(ctx, rl.checkTimeout())
	defer cancel()
	u := *rl.Upstream
	u.Path = strings.TrimRight(rl.Upstream.Path, "/") + t.Path + ".git-pages/health"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return siteHealth{err: err}
	}
	req.Host = t.Host
	req.Header.Set("Cache-Control", "no-cache")
	resp, err := rl.Client.Do(req)
	if err != nil {
		return siteHealth{err: err}
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 64))
	io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
	h := siteHealth{status: resp.StatusCode}
	switch resp.StatusCode {
	case http.StatusOK:
		h.known = true
		h.lm, h.etag = resp.Header.Get("Last-Modified"), resp.Header.Get("ETag")
		// Anything else is a file of the index site at that path: no project site.
		h.ok = string(body) == "ok\n" && h.lm != ""
	case http.StatusNotFound:
		h.known = true
	}
	return h
}

// updatesSample is what the relay reads from git-pages' /metrics.
type updatesSample struct {
	value float64 // git_pages_site_updates{via="webhook"}: absent until the first update ends
	start float64 // process_start_time_seconds
}

func (rl *Relay) readUpdates(ctx context.Context) (updatesSample, error) {
	ctx, cancel := context.WithTimeout(ctx, rl.checkTimeout())
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rl.Updates.String(), nil)
	if err != nil {
		return updatesSample{}, err
	}
	resp, err := rl.Client.Do(req)
	if err != nil {
		return updatesSample{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return updatesSample{}, fmt.Errorf("status %d", resp.StatusCode)
	}
	var s updatesSample
	want := map[string]*float64{
		`git_pages_site_updates{via="webhook"}`:       &s.value,
		`git_pages_site_updates_total{via="webhook"}`: &s.value, // OpenMetrics naming, should it change
		"process_start_time_seconds":                  &s.start,
	}
	sc := bufio.NewScanner(io.LimitReader(resp.Body, 4<<20))
	sc.Buffer(make([]byte, 0, 64<<10), 1<<20)
	for sc.Scan() {
		for name, dst := range want {
			if rest, ok := strings.CutPrefix(sc.Text(), name+" "); ok {
				if f := strings.Fields(rest); len(f) > 0 {
					if v, err := strconv.ParseFloat(f[0], 64); err == nil {
						*dst = v
					}
				}
			}
		}
	}
	return s, sc.Err()
}

type hold struct {
	job      job        // the forward that went (the target, and the clone URL for a re-queue)
	base     siteHealth // read before the forward
	sent     time.Time  // when the forward went
	since    time.Time  // the 202
	deadline time.Time
	due      time.Time // the next health read
}

// ended is a hold that ended, for the log.
type ended struct {
	site     string
	outcome  HoldOutcome
	evidence string
	held     time.Duration
}

// ledger matches the updates the relay starts in git-pages against git-pages'
// count of webhook updates that have ended, git_pages_site_updates{via="webhook"},
// which grows when an update ends, whatever its outcome (src/pages.go:924,
// :52-53). The relay is git-pages' only webhook client, so from a base taken
// while nothing of the relay's could still run (a hold that ends on health
// counts as running to its deadline: the update outlives its commit), the counter catching up with
// the updates started since proves that none of them runs any more.
//
// The ledger is not trusted (ok false) from start until it is quiet, after an
// unanswered forward (its update may run on), after git-pages restarts, and
// once the counter grows past what the relay started (another client, or a
// predecessor pod during a rollout). It then helps no hold until a new base.
type ledger struct {
	launched     int64 // forwards that may have started an update, refusals taken back
	ok           bool
	base         float64
	baseStart    float64
	baseLaunched int64
	seen         bool // the poller has read the counter
	last         float64
	lastStart    float64
	lastEnd      time.Time // when the latest of the relay's updates may end; only ever raised (raiseLastEnd)
	unknownAt    time.Time // a forward last went unanswered
	startedAt    time.Time
}

// raiseLastEnd records that an update of the relay's may run until t. It never
// lowers the bound: a later, earlier-ending update must not undo what a
// health-ended update's tail needs (review N1 of fix 2).
func (l *ledger) raiseLastEnd(t time.Time) {
	if t.After(l.lastEnd) {
		l.lastEnd = t
	}
}

// launch counts a forward about to go: until its answer says otherwise, it may
// start an update.
func (q *queue) launch() {
	q.mu.Lock()
	q.led.launched++
	q.mu.Unlock()
}

// finish ends a forward. A 202 holds the site, unless the drain has started,
// which releases it at once; any other answer frees it. The ledger learns what
// became of the update. It returns a copy of a new hold, and a released one.
func (q *queue) finish(j job, a answer, base siteHealth, sent, now time.Time, cfg HoldConfig) (*hold, []ended) {
	site := j.Target.Site()
	q.mu.Lock()
	defer q.mu.Unlock()
	defer q.wake.Broadcast() // the site's dirty entry may go, unless it is held
	delete(q.inFlight, site)
	switch a {
	case ansNone:
		q.led.launched--
	case ansDone:
		q.led.raiseLastEnd(now)
	case ansUnknown:
		q.led.ok = false
		q.led.unknownAt = now
	case ansRunning:
		if q.closed {
			q.c.HoldEnded(HeldReleased)
			return nil, []ended{{site: site, outcome: HeldReleased, evidence: "shutdown"}}
		}
		h := &hold{job: j, base: base, sent: sent, since: now, deadline: now.Add(cfg.Timeout),
			due: now.Add(cfg.HealthFirst)}
		q.held[site] = h
		q.c.SetHeld(len(q.held))
		select {
		case q.kick <- struct{}{}:
		default:
		}
		c := *h
		return &c, nil
	}
	return nil, nil
}

func (q *queue) endHold(site string, o HoldOutcome, evidence string, now time.Time) ended {
	h := q.held[site]
	delete(q.held, site)
	q.c.SetHeld(len(q.held))
	q.c.HoldEnded(o)
	if o != HeldReleased {
		q.led.raiseLastEnd(now)
	}
	q.wake.Broadcast()
	return ended{site: site, outcome: o, evidence: evidence, held: now.Sub(h.since)}
}

// abort ends a hold whose update died with git-pages (it restarted: SIGTERM
// kills updates without draining them, as does an OOM kill). Nothing stored the
// push and git-pages counts no failure, so the relay forwards it once more,
// from the branch head, unless a newer push of the site already waits (it
// supersedes this one) or this was already the second try (an update that
// kills git-pages must not do it in a loop). Called under q.mu.
func (q *queue) abort(site string, h *hold, now time.Time) ended {
	_, newer := q.pending[site]
	if !newer && h.job.retried {
		// Nothing will forward it: a human must redeliver (PagesRelayPushLost).
		e := q.endHold(site, HeldLost, "git_pages_restarted", now)
		e.evidence += ", already retried: redeliver the push from the forge's delivery history"
		return e
	}
	e := q.endHold(site, HeldAborted, "git_pages_restarted", now)
	if newer {
		e.evidence += ", superseded by a waiting push"
		return e
	}
	j := h.job
	j.retried = true
	q.pending[site] = j
	q.order = append(q.order, site)
	q.c.SetQueueDepth(len(q.pending))
	e.evidence += ", re-queued"
	return e
}

func (q *queue) expire(now time.Time) []ended {
	var out []ended
	for site, h := range q.held {
		if !now.Before(h.deadline) {
			out = append(out, q.endHold(site, HeldTimedOut, "deadline", now))
		}
	}
	return out
}

// nextDue returns the held site whose health read is due first, and the
// earliest deadline; any is false when nothing is held.
func (q *queue) nextDue() (site string, t Target, due, deadline time.Time, any bool) {
	for s, h := range q.held {
		if !any || h.due.Before(due) {
			site, t, due = s, h.job.Target, h.due
		}
		if !any || h.deadline.Before(deadline) {
			deadline = h.deadline
		}
		any = true
	}
	return
}

// healthSeen ends site's hold if its health moved from the baseline, else
// schedules its next read.
func (q *queue) healthSeen(site string, cur siteHealth, now time.Time, next time.Duration) []ended {
	h := q.held[site]
	if h == nil {
		return nil
	}
	if cur.movedFrom(h.base) {
		e := q.endHold(site, HeldCompleted, "health", now)
		// The commit moved the health, but the update runs on after it
		// (CreateDomain, AddSite) and is counted only when that is done, so
		// until its deadline it may still reach the counter: no base is sound
		// before then (review I1 of fix 2). The deadline is the 202 plus
		// HoldTimeout, at or after the update's own.
		q.led.raiseLastEnd(h.deadline)
		return []ended{e}
	}
	h.due = now.Add(next)
	return nil
}

// updatesSeen applies a counter reading: a restart ends the holds on forwards
// sent before git-pages started (their updates died with the old process); a
// trusted ledger that has caught up ends every hold; a counter that moved makes
// every held site's health due now. distrust reports that the counter outgrew
// the relay's updates.
func (q *queue) updatesSeen(s updatesSample, now time.Time) (out []ended, distrust bool) {
	l := &q.led
	newProcess := (l.seen && s.start != l.lastStart) || (l.ok && s.start != l.baseStart)
	dropped := (l.seen && s.value < l.last) || (l.ok && s.value < l.base)
	moved := l.seen && s.value != l.last
	l.seen, l.last, l.lastStart = true, s.value, s.start
	if newProcess || dropped {
		l.ok = false
	}
	if newProcess && s.start > 0 {
		// 1 s for clock skew between the nodes: a forward sent just before may
		// have reached the new process; its hold stays.
		cut := time.Unix(0, int64(s.start*float64(time.Second))).Add(-time.Second)
		for site, h := range q.held {
			if h.sent.Before(cut) {
				out = append(out, q.abort(site, h, now))
			}
		}
	}
	if l.ok {
		switch done, ours := s.value-l.base, float64(l.launched-l.baseLaunched); {
		case done > ours:
			l.ok, distrust = false, true
		case done == ours:
			for site := range q.held {
				out = append(out, q.endHold(site, HeldCompleted, "updates_counter", now))
			}
		}
	}
	if moved {
		for _, h := range q.held {
			h.due = now
		}
	}
	return out, distrust
}

// baseable reports whether nothing the relay started can still run in
// git-pages, so that a counter reading now is a sound base: the caller's
// forward is the only one in flight and has not gone yet, nothing is held, the
// last update's end has had LedgerQuiet to reach the counter, no forward went
// unanswered within Timeout + LedgerQuiet, and a predecessor pod's updates are
// over. It returns launched, which must not change before setBase.
func (q *queue) baseable(now time.Time, cfg HoldConfig) (int64, bool) {
	l := &q.led
	return l.launched, !l.ok && len(q.inFlight) == 1 && len(q.held) == 0 &&
		now.Sub(l.lastEnd) >= cfg.LedgerQuiet && now.Sub(l.unknownAt) >= cfg.Timeout+cfg.LedgerQuiet &&
		now.Sub(l.startedAt) >= cfg.LedgerWarmup
}

func (q *queue) setBase(s updatesSample, launched int64) bool {
	l := &q.led
	if l.ok || l.launched != launched || len(q.held) != 0 || len(q.inFlight) != 1 {
		return false // something went meanwhile: try again at a later forward
	}
	l.ok, l.base, l.baseStart, l.baseLaunched = true, s.value, s.start, launched
	return true
}

// maybeBase takes a ledger base before a forward when the ledger is not
// trusted and nothing of the relay's can still run.
func (rl *Relay) maybeBase() {
	if rl.Updates == nil {
		return
	}
	rl.q.mu.Lock()
	launched, ok := rl.q.baseable(time.Now(), rl.holdConfig())
	rl.q.mu.Unlock()
	if !ok {
		return
	}
	s, err := rl.readUpdates(rl.ctx)
	if err != nil {
		rl.Log.Warn("updates counter unreadable", "error", err.Error())
		return
	}
	rl.q.mu.Lock()
	based := rl.q.setBase(s, launched)
	rl.q.mu.Unlock()
	if based {
		rl.Log.Info("updates counter: base", "value", s.value)
	}
}

// poll runs the holds. Each turn it ends the holds past their deadline, then
// makes the request due first: the update counter (every CounterEvery) or the
// health of the held site due soonest. A request starts no sooner than PollGap
// plus jitter after the previous one started.
func (rl *Relay) poll() {
	defer rl.pollWG.Done()
	cfg := rl.holdConfig()
	var allowed, counterDue time.Time
	failing := false
	for {
		now := time.Now()
		rl.q.mu.Lock()
		e := rl.q.expire(now)
		site, t, due, deadline, any := rl.q.nextDue()
		rl.q.mu.Unlock()
		rl.logEnded(e)
		if !any {
			select {
			case <-rl.q.kick:
				continue
			case <-rl.pctx.Done():
				return
			}
		}
		counter := rl.Updates != nil && !counterDue.After(due)
		if counter {
			due = counterDue
		}
		start := due
		if allowed.After(start) {
			start = allowed
		}
		if start.After(now) { // deadline is after now: expire just ran
			wake := start
			if deadline.Before(wake) {
				wake = deadline
			}
			timer := time.NewTimer(wake.Sub(now))
			select {
			case <-timer.C:
			case <-rl.q.kick:
				timer.Stop()
			case <-rl.pctx.Done():
				timer.Stop()
				return
			}
			continue
		}
		allowed = time.Now().Add(cfg.PollGap + jitter(cfg.PollJitter))
		if counter {
			s, err := rl.readUpdates(rl.pctx)
			counterDue = time.Now().Add(cfg.CounterEvery)
			if err != nil {
				if !failing && rl.pctx.Err() == nil {
					rl.Log.Warn("updates counter unreadable", "error", err.Error())
				}
				failing = true
				continue
			}
			failing = false
			rl.q.mu.Lock()
			e, distrust := rl.q.updatesSeen(s, time.Now())
			rl.q.mu.Unlock()
			if distrust {
				rl.Log.Warn("updates counter: more webhook updates than the relay started; holds wait for health until the relay is quiet",
					"value", s.value)
			}
			rl.logEnded(e)
			continue
		}
		h := rl.readHealth(rl.pctx, t)
		rl.q.mu.Lock()
		e = rl.q.healthSeen(site, h, time.Now(), cfg.HealthEvery+jitter(cfg.HealthEvery/5))
		rl.q.mu.Unlock()
		rl.logEnded(e)
	}
}

func (rl *Relay) logEnded(es []ended) {
	for _, e := range es {
		lvl := slog.LevelInfo
		if e.outcome == HeldTimedOut || e.outcome == HeldAborted || e.outcome == HeldLost {
			lvl = slog.LevelWarn
		}
		rl.Log.Log(context.Background(), lvl, "hold ended", "site", e.site, "outcome", string(e.outcome),
			"evidence", e.evidence, "seconds", e.held.Seconds())
	}
}
