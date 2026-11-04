package main

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fakePages models what git-pages 0.10.1 does with a webhook and with the two
// reads the relay makes:
//   - POST (postPage, src/pages.go:828-950): the update clones the site's head
//     when it starts, then commits it with no precondition (update.go:144); git-pages
//     answers with the result, or 202 once its 3 s timer fires (scaled here) while
//     the update goes on;
//   - GET /<p>/.git-pages/health (:236-240): 404 until the site exists, then 200
//     "ok" with the manifest's Last-Modified, which every commit moves
//     (update.go:73-86 commits even an unchanged site);
//   - GET /metrics: git_pages_site_updates{via="webhook"}, counted when an update
//     ends whatever its outcome (:924, :52-53), absent until the first one, and
//     process_start_time_seconds.
//
// It records, per site, overlapping updates: the race the hold exists to stop.
type fakePages struct {
	timer time.Duration // git-pages' 3 s answer timer

	mu        sync.Mutex
	head      map[string]int // what a clone gets now; the test bumps it before a push
	committed map[string]int
	lm        map[string]int // commit sequence number, served as Last-Modified
	running   map[string]int
	updates   map[string]int // updates started, per site
	spans     map[string][]span
	overlaps  int
	ended     int // the webhook update counter
	start     float64
	gen       int // bumped by restart; an update of an older generation is dead
	seq       int
	accepted  int // 202 answers
	polls     []time.Time
	// plan decides, per site and update number, how long the update runs, whether
	// it fails (no commit) and an optional gate it waits on first.
	plan func(site string, n int) (d time.Duration, fail bool, gate <-chan struct{})
	bg   sync.WaitGroup
}

type span struct{ start, end time.Time }

func newFakePages(timer time.Duration) *fakePages {
	return &fakePages{timer: timer, head: map[string]int{}, committed: map[string]int{}, lm: map[string]int{},
		running: map[string]int{}, updates: map[string]int{}, spans: map[string][]span{},
		start: float64(time.Now().Unix()),
		plan:  func(string, int) (time.Duration, bool, <-chan struct{}) { return 0, false, nil }}
}

// key is the site as the tests name it: <host><project path>, e.g. "a.realliance.page/site/".
func pagesKey(r *http.Request) string {
	return r.Host + strings.TrimSuffix(r.URL.Path, ".git-pages/health")
}

func (f *fakePages) bump(owner string) {
	f.mu.Lock()
	f.head[owner+"."+domain+"/site/"]++
	f.mu.Unlock()
}

func (f *fakePages) restart() {
	f.mu.Lock()
	f.gen++
	f.ended = 0
	f.running = map[string]int{}
	f.start = float64(time.Now().Unix() + 1)
	f.mu.Unlock()
}

func (f *fakePages) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/metrics":
		f.mu.Lock()
		f.polls = append(f.polls, time.Now())
		if f.ended > 0 {
			fmt.Fprintf(w, "# TYPE git_pages_site_updates counter\ngit_pages_site_updates{via=\"webhook\"} %d\n", f.ended)
		}
		fmt.Fprintf(w, "# TYPE process_start_time_seconds gauge\nprocess_start_time_seconds %g\n", f.start)
		f.mu.Unlock()
	case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/.git-pages/health"):
		f.mu.Lock()
		f.polls = append(f.polls, time.Now())
		n := f.lm[pagesKey(r)]
		f.mu.Unlock()
		if n == 0 {
			w.WriteHeader(http.StatusNotFound)
			io.WriteString(w, "site not found\n")
			return
		}
		w.Header().Set("Last-Modified", time.Unix(1_790_000_000+int64(n), 0).UTC().Format(http.TimeFormat))
		w.Header().Set("ETag", fmt.Sprintf(`"%d"`, n))
		io.WriteString(w, "ok\n")
	case r.Method == http.MethodPost:
		f.update(w, r)
	default:
		w.WriteHeader(http.StatusNotFound)
	}
}

func (f *fakePages) update(w http.ResponseWriter, r *http.Request) {
	io.Copy(io.Discard, r.Body)
	site := pagesKey(r)
	f.mu.Lock()
	f.running[site]++
	if f.running[site] > 1 {
		f.overlaps++
	}
	f.updates[site]++
	n, v, gen := f.updates[site], f.head[site], f.gen
	f.spans[site] = append(f.spans[site], span{start: time.Now()})
	d, fail, gate := f.plan(site, n)
	f.mu.Unlock()
	done := make(chan bool, 1)
	f.bg.Add(1)
	go func() { // the update goroutine, detached from the request (context.WithoutCancel)
		defer f.bg.Done()
		if gate != nil {
			<-gate
		}
		time.Sleep(d)
		f.mu.Lock()
		if gen == f.gen { // a dead process neither commits nor counts
			if !fail {
				f.committed[site] = v
				f.seq++
				f.lm[site] = f.seq
			}
			f.running[site]--
			f.ended++
		}
		f.spans[site][n-1].end = time.Now()
		f.mu.Unlock()
		done <- fail
	}()
	select {
	case fail := <-done:
		if fail {
			w.WriteHeader(http.StatusServiceUnavailable)
			io.WriteString(w, "update error: clone failed\n")
		} else {
			io.WriteString(w, "replaced\n")
		}
	case <-time.After(f.timer):
		f.mu.Lock()
		f.accepted++
		f.mu.Unlock()
		w.WriteHeader(http.StatusAccepted)
		io.WriteString(w, "updating (taking longer than 3s)")
	}
}

// fastHold is the hold scaled for tests: git-pages' 3 s timer becomes tens of
// milliseconds, so the polling intervals shrink with it.
func fastHold() HoldConfig {
	return HoldConfig{Timeout: 5 * time.Second, PollGap: 2 * time.Millisecond, PollJitter: time.Millisecond,
		HealthFirst: 3 * time.Millisecond, HealthEvery: 10 * time.Millisecond, CounterEvery: 5 * time.Millisecond,
		LedgerQuiet: time.Nanosecond, LedgerWarmup: time.Nanosecond}
}

// pagesHarness puts fp behind a Relay with the hold config; counter wires the
// relay to fp's /metrics.
func pagesHarness(t *testing.T, fp *fakePages, cfg HoldConfig, counter bool) *harness {
	t.Helper()
	hits, gets := new(atomic.Int32), new(atomic.Int32)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			gets.Add(1)
		} else {
			hits.Add(1)
		}
		fp.ServeHTTP(w, r)
	}))
	t.Cleanup(func() { srv.Close(); fp.bg.Wait() })
	u, _ := url.Parse(srv.URL)
	buf := new(syncBuffer)
	rl := &Relay{Secret: []byte("s3cret"), Upstream: u, Domain: domain, Forge: forge,
		Client: srv.Client(), Counters: NewCounters(), Log: slog.New(slog.NewJSONHandler(buf, nil)), Hold: cfg}
	if counter {
		rl.Updates, _ = url.Parse(srv.URL + "/metrics")
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		rl.Shutdown(ctx)
	})
	return &harness{relay: rl, hits: hits, gets: gets, log: buf, up: srv}
}

func metricValue(m, line string) string {
	for _, l := range strings.Split(m, "\n") {
		if v, ok := strings.CutPrefix(l, line+" "); ok {
			return v
		}
	}
	return "absent"
}

func (h *harness) held() string { return metricValue(h.metrics(), "pages_relay_sites_held") }
func (h *harness) holds(o string) string {
	return metricValue(h.metrics(), fmt.Sprintf(`pages_relay_holds_total{outcome=%q}`, o))
}

// I1: git-pages answers 202 while the update goes on. A push during that update
// must wait until it has committed, or two updates of one site run at once and
// the older one can commit last.
func TestHoldOrdersUpdates(t *testing.T) {
	fp := newFakePages(20 * time.Millisecond)
	gate := make(chan struct{})
	var opened sync.Once
	open := func() { opened.Do(func() { close(gate) }) }
	fp.plan = func(site string, n int) (time.Duration, bool, <-chan struct{}) {
		if n == 1 {
			return 0, false, gate // a big first publish
		}
		return 0, false, nil
	}
	h := pagesHarness(t, fp, fastHold(), false) // health alone must end the hold
	t.Cleanup(open)                             // after the harness, so it runs first: no update stays blocked
	const site = "a.realliance.page/site/"
	fp.bump("a")
	if w := h.pushTo(t, "a", "site"); w.Code != 202 {
		t.Fatal(w.Code)
	}
	waitFor(t, "git-pages to answer 202", func() bool { fp.mu.Lock(); defer fp.mu.Unlock(); return fp.accepted == 1 })
	fp.bump("a") // the quick fix, pushed while the first update runs
	if w := h.pushTo(t, "a", "site"); w.Code != 202 {
		t.Fatal(w.Code)
	}
	time.Sleep(80 * time.Millisecond) // many poll rounds: the hold must not end
	fp.mu.Lock()
	started := fp.updates[site]
	fp.mu.Unlock()
	if started != 1 {
		t.Fatalf("%d updates of the site started while the first still ran", started)
	}
	if h.held() != "1" || metricValue(h.metrics(), "pages_relay_queue_depth") != "1" {
		t.Fatalf("want the site held with its push queued:\n%s", h.metrics())
	}
	open()
	waitFor(t, "the second update to commit", func() bool {
		fp.mu.Lock()
		defer fp.mu.Unlock()
		return fp.committed[site] == 2
	})
	h.settle(t)
	fp.mu.Lock()
	defer fp.mu.Unlock()
	if fp.overlaps != 0 {
		t.Errorf("%d overlapping updates", fp.overlaps)
	}
	if s := fp.spans[site]; len(s) != 2 || s[1].start.Before(s[0].end) {
		t.Errorf("updates %v: the second must start after the first ends", s)
	}
	if h.holds("completed") != "1" || h.holds("timeout") != "0" || h.held() != "0" {
		t.Errorf("hold outcomes:\n%s", h.metrics())
	}
	if l := h.log.String(); !strings.Contains(l, `"evidence":"health"`) {
		t.Errorf("log: %s", l)
	}
}

// A hold is not a worker: an update that fails after its 202 (no commit, so its
// health never moves) holds only its own site, until the deadline.
func TestHoldFailingDoesNotBlockOtherSites(t *testing.T) {
	fp := newFakePages(20 * time.Millisecond)
	fp.plan = func(site string, n int) (time.Duration, bool, <-chan struct{}) {
		if n == 1 && (strings.HasPrefix(site, "a.") || strings.HasPrefix(site, "b.")) {
			return 40 * time.Millisecond, true, nil // 202, then the update fails
		}
		return 0, false, nil
	}
	cfg := fastHold()
	cfg.Timeout = 600 * time.Millisecond
	h := pagesHarness(t, fp, cfg, false) // no counter: only the deadline ends a failed hold
	for _, o := range []string{"a", "b"} {
		fp.bump(o)
		h.pushTo(t, o, "site")
	}
	waitFor(t, "both sites held", func() bool { return h.held() == "2" })
	heldAt := time.Now()
	fp.bump("a")
	h.pushTo(t, "a", "site") // waits for a's hold
	// Both workers are free: other sites publish at once.
	for _, o := range []string{"c", "d", "e"} {
		fp.bump(o)
		if w := h.pushTo(t, o, "site"); w.Code != 202 {
			t.Fatal(w.Code)
		}
	}
	waitFor(t, "c, d and e to publish", func() bool {
		fp.mu.Lock()
		defer fp.mu.Unlock()
		for _, o := range []string{"c", "d", "e"} {
			if fp.committed[o+".realliance.page/site/"] != 1 {
				return false
			}
		}
		return true
	})
	if d := time.Since(heldAt); d > 400*time.Millisecond {
		t.Errorf("other sites took %v behind two held sites", d)
	}
	if h.held() != "2" {
		t.Errorf("a and b should still be held:\n%s", h.metrics())
	}
	waitFor(t, "a's queued push to publish after its hold", func() bool {
		fp.mu.Lock()
		defer fp.mu.Unlock()
		return fp.committed["a.realliance.page/site/"] == 2
	})
	fp.mu.Lock()
	second := fp.spans["a.realliance.page/site/"][1].start
	fp.mu.Unlock()
	if second.Sub(heldAt) < cfg.Timeout-100*time.Millisecond {
		t.Errorf("a went again %v after its hold began, before the %v deadline", second.Sub(heldAt), cfg.Timeout)
	}
	if h.holds("timeout") != "2" || h.holds("completed") != "0" {
		t.Errorf("hold outcomes:\n%s", h.metrics())
	}
}

// With git-pages' webhook update counter, a failed update ends its hold as soon
// as the counter shows that every update the relay started has ended.
func TestHoldEndsOnUpdatesCounter(t *testing.T) {
	fp := newFakePages(20 * time.Millisecond)
	fp.plan = func(site string, n int) (time.Duration, bool, <-chan struct{}) {
		if n == 1 {
			return 40 * time.Millisecond, true, nil
		}
		return 0, false, nil
	}
	cfg := fastHold()
	cfg.Timeout = time.Minute
	h := pagesHarness(t, fp, cfg, true)
	fp.bump("a")
	h.pushTo(t, "a", "site")
	waitFor(t, "a held", func() bool { return h.held() == "1" })
	fp.bump("a")
	h.pushTo(t, "a", "site")
	start := time.Now()
	waitFor(t, "a's second push to publish", func() bool {
		fp.mu.Lock()
		defer fp.mu.Unlock()
		return fp.committed["a.realliance.page/site/"] == 2
	})
	if d := time.Since(start); d > 2*time.Second {
		t.Errorf("the failed hold ended after %v", d)
	}
	if h.holds("completed") != "1" || h.holds("timeout") != "0" {
		t.Errorf("hold outcomes:\n%s", h.metrics())
	}
	if l := h.log.String(); !strings.Contains(l, `"evidence":"updates_counter"`) {
		t.Errorf("log: %s", l)
	}
}

// The counter is global: once it counts more updates than the relay started
// (another client, or a predecessor pod's), it says nothing about the relay's
// own, and a hold waits for its site's health instead.
func TestHoldIgnoresForeignUpdates(t *testing.T) {
	fp := newFakePages(20 * time.Millisecond)
	gate := make(chan struct{})
	var opened sync.Once
	open := func() { opened.Do(func() { close(gate) }) }
	fp.plan = func(site string, n int) (time.Duration, bool, <-chan struct{}) {
		if n == 1 {
			return 0, false, gate
		}
		return 0, false, nil
	}
	cfg := fastHold()
	cfg.Timeout = time.Minute
	h := pagesHarness(t, fp, cfg, true)
	t.Cleanup(open) // after the harness, so it runs first
	fp.bump("a")
	h.pushTo(t, "a", "site")
	waitFor(t, "a held", func() bool { return h.held() == "1" })
	fp.bump("a")
	h.pushTo(t, "a", "site")
	fp.mu.Lock()
	fp.ended += 3 // updates the relay never started
	fp.mu.Unlock()
	time.Sleep(80 * time.Millisecond)
	fp.mu.Lock()
	started := fp.updates["a.realliance.page/site/"]
	fp.mu.Unlock()
	if started != 1 || h.held() != "1" {
		t.Fatalf("foreign updates ended the hold: %d updates started\n%s", started, h.metrics())
	}
	open()
	waitFor(t, "a's second push to publish", func() bool {
		fp.mu.Lock()
		defer fp.mu.Unlock()
		return fp.committed["a.realliance.page/site/"] == 2
	})
	fp.mu.Lock()
	defer fp.mu.Unlock()
	if fp.overlaps != 0 {
		t.Errorf("%d overlapping updates", fp.overlaps)
	}
}

// A git-pages restart kills the updates it was running: holds on forwards sent
// before it started end as aborted (a later push of the site waits, so no re-queue).
func TestHoldEndsOnGitPagesRestart(t *testing.T) {
	fp := newFakePages(20 * time.Millisecond)
	never := make(chan struct{})
	fp.plan = func(site string, n int) (time.Duration, bool, <-chan struct{}) {
		if n == 1 {
			return 0, false, never
		}
		return 0, false, nil
	}
	cfg := fastHold()
	cfg.Timeout = time.Minute
	h := pagesHarness(t, fp, cfg, true)
	t.Cleanup(func() { close(never) }) // after the harness, so it runs first
	fp.bump("a")
	h.pushTo(t, "a", "site")
	waitFor(t, "a held", func() bool { return h.held() == "1" })
	time.Sleep(1100 * time.Millisecond) // process_start_time_seconds has 1 s resolution
	fp.bump("a")
	h.pushTo(t, "a", "site")
	fp.restart()
	waitFor(t, "a's second push to publish", func() bool {
		fp.mu.Lock()
		defer fp.mu.Unlock()
		return fp.committed["a.realliance.page/site/"] == 2
	})
	if h.holds("aborted") != "1" || h.holds("completed") != "0" {
		t.Errorf("hold outcomes:\n%s", h.metrics())
	}
	if l := h.log.String(); !strings.Contains(l, `"evidence":"git_pages_restarted, superseded by a waiting push"`) {
		t.Errorf("log: %s", l)
	}
}

// SIGTERM releases every hold when the drain starts: a held site's queued push
// is forwarded at once (the residual race is accepted), a 202 during the drain
// starts no hold, and the drain does not wait out a 5 min hold.
func TestShutdownReleasesHolds(t *testing.T) {
	fp := newFakePages(20 * time.Millisecond)
	never := make(chan struct{})
	fp.plan = func(site string, n int) (time.Duration, bool, <-chan struct{}) { return 0, false, never }
	cfg := fastHold()
	cfg.Timeout = time.Minute
	h := pagesHarness(t, fp, cfg, true)
	t.Cleanup(func() { close(never) }) // after the harness, so it runs first
	fp.bump("a")
	h.pushTo(t, "a", "site")
	waitFor(t, "a held", func() bool { return h.held() == "1" })
	fp.bump("a")
	h.pushTo(t, "a", "site")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	start := time.Now()
	if left := h.relay.Shutdown(ctx); len(left) != 0 {
		t.Errorf("left %v", left)
	}
	if d := time.Since(start); d > time.Second {
		t.Errorf("Shutdown took %v", d)
	}
	fp.mu.Lock()
	n := fp.updates["a.realliance.page/site/"]
	fp.mu.Unlock()
	if n != 2 {
		t.Errorf("a's queued push: %d updates, want 2", n)
	}
	if h.holds("released") != "2" || h.held() != "0" {
		t.Errorf("want both 202s released:\n%s", h.metrics())
	}
	l := h.log.String()
	if !strings.Contains(l, `"evidence":"shutdown"`) || !strings.Contains(l, `"msg":"shutdown: drained"`) {
		t.Errorf("log: %s", l)
	}
}

// Polling is one request at a time, at least PollGap apart (1 s by default, so
// never more than 1 req/s in all), however many sites are held.
func TestHoldPollRateBounded(t *testing.T) {
	if defaultHold.PollGap < time.Second || defaultHold.PollJitter <= 0 {
		t.Fatalf("default poll gap %v jitter %v: must stay at or under 1 req/s, jittered", defaultHold.PollGap, defaultHold.PollJitter)
	}
	if defaultHold.HealthEvery < 5*time.Second || defaultHold.CounterEvery < 2*time.Second {
		t.Errorf("default intervals too eager: %+v", defaultHold)
	}
	if defaultHold.Timeout != HoldTimeout || HoldTimeout != 5*time.Minute {
		t.Errorf("hold timeout %v, want git-pages' update-timeout 5m", defaultHold.Timeout)
	}
	fp := newFakePages(10 * time.Millisecond)
	never := make(chan struct{})
	fp.plan = func(site string, n int) (time.Duration, bool, <-chan struct{}) { return 0, false, never }
	cfg := fastHold()
	cfg.Timeout = time.Minute
	cfg.PollGap, cfg.PollJitter = 40*time.Millisecond, 20*time.Millisecond
	cfg.HealthFirst, cfg.HealthEvery, cfg.CounterEvery = time.Millisecond, time.Millisecond, time.Millisecond
	h := pagesHarness(t, fp, cfg, true)
	t.Cleanup(func() { close(never) }) // after the harness, so it runs first
	for _, o := range []string{"a", "b", "c", "d"} {
		fp.bump(o)
		h.pushTo(t, o, "site")
	}
	waitFor(t, "four sites held", func() bool { return h.held() == "4" })
	fp.mu.Lock()
	from := len(fp.polls)
	fp.mu.Unlock()
	time.Sleep(time.Second)
	fp.mu.Lock()
	polls := append([]time.Time(nil), fp.polls[from:]...)
	fp.mu.Unlock()
	if len(polls) < 10 || len(polls) > 26 {
		t.Errorf("%d poll requests in 1 s at a 40 ms gap", len(polls))
	}
	for i := 1; i < len(polls); i++ {
		if d := polls[i].Sub(polls[i-1]); d < cfg.PollGap-5*time.Millisecond {
			t.Errorf("poll %d came %v after the previous one", i, d)
		}
	}
}

// The review's stress test, with git-pages' 202: each pusher bumps a site's
// head, then delivers. No delivery is refused, git-pages never runs two
// updates of one site at once, and every site ends on its final head.
func TestHoldStressHeadWins(t *testing.T) {
	const sites = 12
	fp := newFakePages(10 * time.Millisecond)
	fp.plan = func(site string, n int) (time.Duration, bool, <-chan struct{}) {
		return time.Duration(rand.IntN(25000)) * time.Microsecond, false, nil // a third answer 202
	}
	cfg := fastHold()
	cfg.PollGap, cfg.HealthEvery = time.Millisecond, 3*time.Millisecond
	h := pagesHarness(t, fp, cfg, true)
	var wg sync.WaitGroup
	var refused atomic.Int32
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 60; i++ {
				o := fmt.Sprintf("s%d", rand.IntN(sites))
				fp.bump(o)
				if c := h.pushTo(t, o, "site").Code; c != 202 {
					refused.Add(1)
				}
				time.Sleep(time.Duration(rand.IntN(10000)) * time.Microsecond)
			}
		}()
	}
	wg.Wait()
	deadline := time.Now().Add(30 * time.Second)
	for !h.relay.idle() || h.held() != "0" {
		if time.Now().After(deadline) {
			t.Fatalf("never settled:\n%s", h.metrics())
		}
		time.Sleep(5 * time.Millisecond)
	}
	fp.bg.Wait()
	fp.mu.Lock()
	defer fp.mu.Unlock()
	if refused.Load() != 0 {
		t.Errorf("%d refused", refused.Load())
	}
	if fp.overlaps != 0 {
		t.Errorf("%d overlapping updates of one site", fp.overlaps)
	}
	for s, v := range fp.head {
		if fp.committed[s] != v {
			t.Errorf("%s: committed %d, head %d", s, fp.committed[s], v)
		}
	}
	// Every 202 ends in exactly one hold outcome.
	m := h.metrics()
	if got := fmt.Sprint(fp.accepted); h.holds("completed") != got || h.holds("timeout") != "0" || h.holds("released") != "0" {
		t.Errorf("%s 202s, outcomes:\n%s", got, m)
	}
	t.Logf("480 pushes -> %d updates, %d answered 202", func() (n int) {
		for _, u := range fp.updates {
			n += u
		}
		return
	}(), fp.accepted)
}

// The ledger is only as safe as classify: an answer taken for "no update
// started" when one did would let the counter catch up early, so only a 4xx
// (always before the update starts, src/pages.go:833-912) counts as none.
func TestClassifyAnswers(t *testing.T) {
	for _, tc := range []struct {
		status int
		body   string
		want   answer
	}{
		{202, "updating (taking longer than 3s)", ansRunning},
		{202, "", ansRunning},
		{200, "replaced\n", ansDone},
		{200, "unchanged\nproblems:\n- x\n", ansDone},
		{200, "created\n", ansDone},
		{200, "deleted\n", ansDone},
		{503, "update error: fetch: boom\n", ansDone},
		{504, "update timeout\n", ansDone},
		{401, "ref refs/heads/main not in allowlist", ansNone},
		{400, "only JSON payload is allowed", ansNone},
		{413, "request body too large", ansNone},
		{200, "", ansUnknown}, // an error ServePages does not map
		{200, "dry-run ok", ansUnknown},
		{503, "", ansUnknown},
		{500, "internal server error", ansUnknown},
		{302, "", ansUnknown},
	} {
		if got := classify(tc.status, []byte(tc.body)); got != tc.want {
			t.Errorf("%d %q: %d, want %d", tc.status, tc.body, got, tc.want)
		}
	}
}

// A forward that got no answer may have started an update that runs for up to
// HoldTimeout: no ledger base until that has passed, and the ledger stops
// helping at once. A refusal takes its launch back.
func TestLedgerUnanswered(t *testing.T) {
	cfg := defaultHold
	q := newQueue(QueueMax, NewCounters())
	q.led.startedAt = time.Now().Add(-time.Hour) // past the warm-up
	j := job{Target: Target{Host: "a." + domain, Path: "/site/"}}
	q.put(j)
	q.take()
	now := time.Now()
	if _, ok := q.baseable(now, cfg); !ok {
		t.Fatal("a quiet relay can take a base")
	}
	if !q.setBase(updatesSample{value: 7, start: 100}, 0) || !q.led.ok {
		t.Fatal("base not taken")
	}
	q.launch()
	q.finish(j, ansNone, siteHealth{}, now, now, cfg)
	if q.led.launched != 0 {
		t.Errorf("a refusal must take its launch back: %d", q.led.launched)
	}
	q.put(j)
	q.take()
	q.launch()
	q.finish(j, ansUnknown, siteHealth{}, now, now, cfg)
	if q.led.ok {
		t.Error("an unanswered forward must stop the ledger helping")
	}
	q.put(j)
	q.take()
	for _, d := range []time.Duration{time.Minute, cfg.Timeout} {
		if _, ok := q.baseable(now.Add(d), cfg); ok {
			t.Errorf("a base %v after an unanswered forward", d)
		}
	}
	if _, ok := q.baseable(now.Add(cfg.Timeout+cfg.LedgerQuiet), cfg); !ok {
		t.Error("no base once the update timeout and the quiet time have passed")
	}
	if q2 := newQueue(QueueMax, NewCounters()); func() bool { q2.put(j); q2.take(); _, ok := q2.baseable(time.Now(), cfg); return ok }() {
		t.Error("a new relay must wait out a predecessor pod's updates before a base")
	}
}
