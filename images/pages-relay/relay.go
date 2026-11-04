// Command pages-relay sits between Forgejo's system webhook and git-pages.
//
// git-pages 0.10.1 picks the site from the request's Host and path (GetSite,
// src/pages.go:95) and reads only `ref` and `repository.clone_url` from the body
// (:880-885), then clones the branch head itself (src/update.go:137), while one
// Forgejo system webhook has a single fixed URL. The relay verifies Forgejo's
// signature (git-pages verifies none), maps owner/repo to a site, answers 202 and
// queues the site; two workers forward a minimal push in-cluster with the right
// Host.
//
// Forgejo 15 never retries a failed delivery, so nothing that passes the checks
// is refused for being busy: pushes to a site that is queued coalesce (latest
// wins), a site is never forwarded twice at once, and a push that lands during
// its site's forward is forwarded again afterwards, so git-pages always fetches a
// head at least as new as the last push.
//
// git-pages answers 202 after 3 s while its update goes on (src/pages.go:914-934).
// The relay then holds the site (hold.go): it is not forwarded again until that
// update has ended, so two updates of one site never race to commit.
package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"
)

// MaxBody is git-pages' own cap on a webhook body (pages.go:875). Larger
// deliveries get 413 and are counted too_large.
const MaxBody = 16 << 20

// MaxInFlight bounds the bodies being read at once (each up to MaxBody, in one
// buffer). A delivery waits up to DefaultSlotWait for a slot; only then is it
// refused as busy.
const MaxInFlight = 2

// DefaultSlotWait bounds waiting for a body slot. Forgejo 15 gives a delivery
// 5 s (DeliverTimeout), so a longer wait is a failed delivery anyway.
const DefaultSlotWait = 3 * time.Second

// DefaultBodyTimeout bounds reading the body once it has a slot.
const DefaultBodyTimeout = 10 * time.Second

// Workers forward queued sites to git-pages, at most one forward per site.
const Workers = 2

// QueueMax bounds the sites waiting for a forward. A push to a site that is
// not already waiting, nor being forwarded or held, is refused as busy (503)
// only when this many wait.
const QueueMax = 64

// HoldTimeout caps a hold after git-pages' 202: its update-timeout
// (cluster/pages/config.toml [limits]), counted from the 202, which comes 3 s
// after the update's own deadline started (src/pages.go:832, :915).
const HoldTimeout = 5 * time.Minute

// PreStopSleep is the pod's lifecycle.preStop sleep
// (cluster/pages/pages-relay.yaml): the relay keeps serving while the endpoint
// goes away, and SIGTERM comes after it.
const PreStopSleep = 5 * time.Second

// DrainTimeout is how long SIGTERM waits for the queue to drain: 66 forwards
// (64 queued + 2 in flight) on 2 workers at git-pages' 3 s answer
// (src/pages.go:832) is about 100 s. Holds end when the drain starts, so none
// waits out HoldTimeout. PreStopSleep + DrainTimeout must fit in
// terminationGracePeriodSeconds (120 in cluster/pages/pages-relay.yaml).
const DrainTimeout = 110 * time.Second

// SiteCheckTimeout bounds asking git-pages whether a site exists; Forgejo 15
// gives a delivery 5 s.
const SiteCheckTimeout = 3 * time.Second

// SignatureHeader carries the lowercase hex HMAC-SHA256 of the raw body, with
// no "sha256=" prefix.
const SignatureHeader = "X-Forgejo-Signature"

type Repo struct {
	Name            string
	Private         bool
	CloneURL        string
	OwnerLogin      string
	Internal        bool
	OwnerVisibility string
}

type Event struct {
	Name, Ref, After, RefType, Action string
	Repository                        Repo
}

type Target struct{ Host, Path string }

// Site is git-pages' name for the target (names.Site: "<domain>/<project>",
// ".index" for the root site), the name its admin CLI takes. It is the queue key.
func (t Target) Site() string {
	project := strings.Trim(t.Path, "/")
	if project == "" {
		project = ".index"
	}
	return t.Host + "/" + project
}

type Outcome string

// Per delivery the handler counts one of queued, coalesced, busy, the ignored_*,
// unpublish_requested and the refusals; per forward a worker counts forwarded
// or upstream_error.
const (
	Forwarded          Outcome = "forwarded"
	Queued             Outcome = "queued"
	Coalesced          Outcome = "coalesced"
	IgnoredEvent       Outcome = "ignored_event"
	IgnoredRef         Outcome = "ignored_ref"
	IgnoredPrivate     Outcome = "ignored_private"
	IgnoredName        Outcome = "ignored_name"
	IgnoredNoSite      Outcome = "ignored_no_site"
	UnpublishRequested Outcome = "unpublish_requested"
	BadSignature       Outcome = "bad_signature"
	BadRequest         Outcome = "bad_request"
	UpstreamError      Outcome = "upstream_error"
	TooLarge           Outcome = "too_large"
	Busy               Outcome = "busy"
)

var allOutcomes = []Outcome{Forwarded, Queued, Coalesced, IgnoredEvent, IgnoredRef, IgnoredPrivate,
	IgnoredName, IgnoredNoSite, UnpublishRequested, BadSignature, BadRequest, UpstreamError, TooLarge, Busy}

// ParseEvent decodes the fields the relay needs from a Forgejo webhook body.
func ParseEvent(name string, body []byte) (Event, error) {
	var p struct {
		Ref        string `json:"ref"`
		After      string `json:"after"`
		RefType    string `json:"ref_type"`
		Action     string `json:"action"`
		Repository struct {
			Name     string `json:"name"`
			Private  bool   `json:"private"`
			Internal bool   `json:"internal"`
			CloneURL string `json:"clone_url"`
			Owner    struct {
				Login      string `json:"login"`
				Visibility string `json:"visibility"`
			} `json:"owner"`
		} `json:"repository"`
	}
	if err := json.Unmarshal(body, &p); err != nil {
		return Event{}, err
	}
	return Event{Name: name, Ref: p.Ref, After: p.After, RefType: p.RefType, Action: p.Action,
		Repository: Repo{Name: p.Repository.Name, Private: p.Repository.Private,
			Internal: p.Repository.Internal, OwnerVisibility: p.Repository.Owner.Visibility,
			CloneURL: p.Repository.CloneURL, OwnerLogin: p.Repository.Owner.Login}}, nil
}

var ownerRE = regexp.MustCompile(`^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$`)
var repoRE = regexp.MustCompile(`^[a-z0-9_-][a-z0-9_.-]{0,99}$`)
var afterRE = regexp.MustCompile(`^[0-9a-f]{40}([0-9a-f]{24})?$`)

// Publishable reports whether a repo is fully public: not private, not
// internal, and owned by a public user or org. Fails closed on a missing
// owner visibility. Anything else is never forwarded and its names are never
// logged.
func (r Repo) Publishable() bool {
	return !r.Private && !r.Internal && r.OwnerVisibility == "public"
}

func isASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] >= 0x80 {
			return false
		}
	}
	return true
}

func allZeros(s string) bool {
	if len(s) != 40 && len(s) != 64 {
		return false
	}
	return strings.Trim(s, "0") == ""
}

// Route decides what to do with an event. It is pure. Rule order:
//
//  1. event not push/delete/repository -> ignored_event
//  2. private repo -> ignored_private, EXCEPT the two unpublish events
//     (delete of branch "pages"; repository "deleted"), which continue so a
//     site published while the repo was public still raises a takedown request
//  3. clone URL not under forge -> bad_request
//  4. owner not a valid DNS label, or "www" -> ignored_name
//  5. repo starting with "." -> ignored_name; derive Host and Path
//  6. per-event: push (ref, all-zero after), delete, repository
func Route(ev Event, domain, forge string) (Target, Outcome) {
	switch ev.Name {
	case "push", "delete", "repository":
	default:
		return Target{}, IgnoredEvent
	}
	unpublishEvent := (ev.Name == "delete" && ev.RefType == "branch" && (ev.Ref == "refs/heads/pages" || ev.Ref == "pages")) ||
		(ev.Name == "repository" && ev.Action == "deleted")
	if !ev.Repository.Publishable() && !unpublishEvent {
		return Target{}, IgnoredPrivate
	}
	if !isASCII(ev.Repository.OwnerLogin) || !isASCII(ev.Repository.Name) {
		return Target{}, IgnoredName
	}
	if len(ev.Repository.CloneURL) < len(forge) || !strings.EqualFold(ev.Repository.CloneURL[:len(forge)], forge) {
		return Target{}, BadRequest
	}
	if !isASCII(ev.Repository.CloneURL) {
		return Target{}, BadRequest
	}
	if !strings.EqualFold(ev.Repository.CloneURL, forge+ev.Repository.OwnerLogin+"/"+ev.Repository.Name+".git") {
		return Target{}, BadRequest
	}
	// Match the raw owner before lower-casing (U+212A lower-cases to "k").
	if !ownerRE.MatchString(ev.Repository.OwnerLogin) {
		return Target{}, IgnoredName
	}
	owner := strings.ToLower(ev.Repository.OwnerLogin)
	if owner == "www" {
		return Target{}, IgnoredName
	}
	repo := strings.ToLower(ev.Repository.Name)
	if !repoRE.MatchString(repo) {
		return Target{}, IgnoredName
	}
	t := Target{Host: owner + "." + domain, Path: "/" + repo + "/"}
	if repo == "pages" {
		t.Path = "/"
	}
	switch ev.Name {
	case "push":
		if ev.Ref != "refs/heads/pages" {
			return Target{}, IgnoredRef
		}
		if allZeros(ev.After) {
			return t, UnpublishRequested
		}
		if !afterRE.MatchString(ev.After) {
			return Target{}, BadRequest
		}
		return t, Forwarded
	case "delete":
		if unpublishEvent {
			return t, UnpublishRequested
		}
		return Target{}, IgnoredRef
	default: // repository
		if unpublishEvent {
			return t, UnpublishRequested
		}
		return Target{}, IgnoredEvent
	}
}

// Verify checks a hex (case-insensitive on decode) HMAC-SHA256 of body in constant time.
func Verify(body, secret []byte, sigHex string) bool {
	if len(secret) == 0 {
		return false
	}
	got, err := hex.DecodeString(sigHex)
	if err != nil || len(got) != sha256.Size {
		return false
	}
	m := hmac.New(sha256.New, secret)
	m.Write(body)
	return hmac.Equal(got, m.Sum(nil))
}

// HoldOutcome is how a hold ended: completed (evidence that git-pages' update
// ended), timeout (HoldTimeout, no evidence) or released (shutdown).
type HoldOutcome string

const (
	HeldCompleted HoldOutcome = "completed"
	HeldTimedOut  HoldOutcome = "timeout"
	HeldReleased  HoldOutcome = "released"
)

var allHoldOutcomes = []HoldOutcome{HeldCompleted, HeldTimedOut, HeldReleased}

type Counters struct {
	mu    sync.Mutex
	n     map[Outcome]uint64
	holds map[HoldOutcome]uint64
	depth int
	held  int
}

func NewCounters() *Counters {
	return &Counters{n: map[Outcome]uint64{}, holds: map[HoldOutcome]uint64{}}
}

func (c *Counters) Inc(o Outcome) {
	c.mu.Lock()
	c.n[o]++
	c.mu.Unlock()
}

// SetQueueDepth records how many sites wait for a forward.
func (c *Counters) SetQueueDepth(n int) {
	c.mu.Lock()
	c.depth = n
	c.mu.Unlock()
}

// SetHeld records how many sites are held after a 202.
func (c *Counters) SetHeld(n int) {
	c.mu.Lock()
	c.held = n
	c.mu.Unlock()
}

// HoldEnded counts how a hold ended.
func (c *Counters) HoldEnded(o HoldOutcome) {
	c.mu.Lock()
	c.holds[o]++
	c.mu.Unlock()
}

// WriteTo writes every outcome (from zero, so increase() sees the first event),
// the queue depth and the holds in the Prometheus text format. It writes after
// unlocking: the queue sets the gauges under its own lock (review M8).
func (c *Counters) WriteTo(w io.Writer) (int64, error) {
	var b strings.Builder
	c.mu.Lock()
	b.WriteString("# TYPE pages_relay_events_total counter\n")
	for _, o := range allOutcomes {
		fmt.Fprintf(&b, "pages_relay_events_total{outcome=%q} %d\n", string(o), c.n[o])
	}
	b.WriteString("# TYPE pages_relay_queue_depth gauge\n")
	fmt.Fprintf(&b, "pages_relay_queue_depth %d\n", c.depth)
	b.WriteString("# TYPE pages_relay_sites_held gauge\n")
	fmt.Fprintf(&b, "pages_relay_sites_held %d\n", c.held)
	b.WriteString("# TYPE pages_relay_holds_total counter\n")
	for _, o := range allHoldOutcomes {
		fmt.Fprintf(&b, "pages_relay_holds_total{outcome=%q} %d\n", string(o), c.holds[o])
	}
	c.mu.Unlock()
	n, err := io.WriteString(w, b.String())
	return int64(n), err
}

// NewClient returns the upstream client: bounded timeout, no redirects.
func NewClient() *http.Client {
	return &http.Client{
		Timeout:       30 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

// job is all the relay keeps of an accepted push: never the body.
type job struct {
	Target   Target
	CloneURL string // as Forgejo sent it; git-pages compares it case-insensitively
}

// forwardBody is a minimal push that git-pages 0.10.1 accepts: the only two
// fields postPage decodes (src/pages.go:880-885). git-pages then clones the
// branch head, so nothing else in Forgejo's payload matters.
func forwardBody(cloneURL string) []byte {
	var p struct {
		Ref        string `json:"ref"`
		Repository struct {
			CloneURL string `json:"clone_url"`
		} `json:"repository"`
	}
	p.Ref = "refs/heads/pages"
	p.Repository.CloneURL = cloneURL
	b, _ := json.Marshal(p) // two strings: cannot fail
	return b
}

type enqueued int

const (
	enqQueued enqueued = iota
	enqCoalesced
	enqFull
	enqClosed
)

// queue holds the sites waiting for a forward, once each, in first-arrival
// order. take never hands out a site that is being forwarded or held: a push
// that lands meanwhile waits as a pending ("dirty") entry until that forward,
// and the update git-pages may still run after its 202, has ended; then it goes
// again, so git-pages refetches the head after the last push. At most max sites
// wait before a new site is refused; a dirty entry of a site in flight or held
// is never refused, and Workers plus the holds bound those.
//
// The holds and the ledger (hold.go) live under the same lock.
type queue struct {
	mu       sync.Mutex
	wake     *sync.Cond
	max      int
	order    []string       // waiting sites, first arrival first
	pending  map[string]job // site -> latest job
	inFlight map[string]bool
	held     map[string]*hold
	closed   bool
	c        *Counters
	kick     chan struct{} // wakes the poller when a hold starts
	led      ledger
}

func newQueue(max int, c *Counters) *queue {
	q := &queue{max: max, pending: map[string]job{}, inFlight: map[string]bool{}, held: map[string]*hold{},
		c: c, kick: make(chan struct{}, 1)}
	q.wake = sync.NewCond(&q.mu)
	q.led.startedAt = time.Now()
	return q
}

func (q *queue) put(j job) enqueued {
	site := j.Target.Site()
	q.mu.Lock()
	defer q.mu.Unlock()
	if q.closed {
		return enqClosed
	}
	if _, ok := q.pending[site]; ok {
		q.pending[site] = j // latest wins
		return enqCoalesced
	}
	if len(q.pending) >= q.max && !q.inFlight[site] && q.held[site] == nil {
		return enqFull
	}
	q.pending[site] = j
	q.order = append(q.order, site)
	q.c.SetQueueDepth(len(q.pending))
	q.wake.Broadcast()
	return enqQueued
}

// take blocks until a waiting site is neither being forwarded nor held and
// hands it out, or returns false once the queue is closed and empty.
func (q *queue) take() (job, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	for {
		for i, site := range q.order {
			if q.inFlight[site] || q.held[site] != nil {
				continue
			}
			j := q.pending[site]
			delete(q.pending, site)
			q.order = append(q.order[:i:i], q.order[i+1:]...)
			q.inFlight[site] = true
			q.c.SetQueueDepth(len(q.pending))
			return j, true
		}
		if q.closed && len(q.pending) == 0 {
			return job{}, false
		}
		q.wake.Wait()
	}
}

// has reports whether the relay still has anything of site: queued, in flight
// or held, so a publish may still commit (review M3).
func (q *queue) has(site string) bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	_, pending := q.pending[site]
	return pending || q.inFlight[site] || q.held[site] != nil
}

func (q *queue) isClosed() bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.closed
}

// close stops accepting and releases every hold, so the drain forwards held
// sites' pushes at once rather than waiting out HoldTimeout (the race that the
// hold prevents is accepted at shutdown).
func (q *queue) close(now time.Time) []ended {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.closed = true
	var out []ended
	for site := range q.held {
		out = append(out, q.endHold(site, HeldReleased, "shutdown", now))
	}
	q.wake.Broadcast()
	return out
}

// abandon empties the queue and returns what was not forwarded: the sites in
// flight, then the waiting ones in arrival order (a site can be both).
func (q *queue) abandon() (inFlight, waiting []string) {
	q.mu.Lock()
	defer q.mu.Unlock()
	for site := range q.inFlight {
		inFlight = append(inFlight, site)
	}
	waiting, q.order = q.order, nil
	q.pending = map[string]job{}
	q.closed = true
	q.c.SetQueueDepth(0)
	q.wake.Broadcast()
	return inFlight, waiting
}

// idle reports that nothing is queued or being forwarded (holds may remain).
func (q *queue) idle() bool {
	q.mu.Lock()
	defer q.mu.Unlock()
	return len(q.pending) == 0 && len(q.inFlight) == 0
}

type Relay struct {
	Secret   []byte
	Upstream *url.URL
	Domain   string
	Forge    string
	Client   *http.Client
	Counters *Counters
	Log      *slog.Logger

	// Updates is git-pages' /metrics, for its webhook update counter (hold.go);
	// nil: a hold ends by its site's health or its deadline only.
	Updates *url.URL
	Hold    HoldConfig // zero fields: defaultHold

	BodyTimeout  time.Duration // 0: DefaultBodyTimeout
	SlotWait     time.Duration // 0: DefaultSlotWait
	CheckTimeout time.Duration // 0: SiteCheckTimeout

	once    sync.Once
	sem     chan struct{}
	q       *queue
	wg      sync.WaitGroup
	ctx     context.Context // forwards; canceled when a drain runs out of time
	cancel  context.CancelFunc
	pollWG  sync.WaitGroup
	pctx    context.Context // the hold poller; canceled when the drain starts
	pcancel context.CancelFunc
}

// start creates the queue, its workers and the hold poller on first use.
func (rl *Relay) start() {
	rl.once.Do(func() {
		rl.sem = make(chan struct{}, MaxInFlight)
		rl.q = newQueue(QueueMax, rl.Counters)
		rl.ctx, rl.cancel = context.WithCancel(context.Background())
		rl.pctx, rl.pcancel = context.WithCancel(context.Background())
		for i := 0; i < Workers; i++ {
			rl.wg.Add(1)
			go rl.work()
		}
		rl.pollWG.Add(1)
		go rl.poll()
	})
}

func (rl *Relay) idle() bool {
	rl.start()
	return rl.q.idle()
}

// work forwards sites one at a time. Before each forward it reads the site's
// health, the baseline a hold compares with, and maybe a ledger base; after a
// 202 the site is held instead of freed, and the worker moves on.
func (rl *Relay) work() {
	defer rl.wg.Done()
	for {
		j, ok := rl.q.take()
		if !ok {
			return
		}
		var base siteHealth
		if !rl.q.isClosed() { // a drain holds nothing, so it needs no baseline
			rl.maybeBase()
			base = rl.readHealth(rl.ctx, j.Target)
		}
		rl.q.launch()
		sent := time.Now()
		a := rl.forward(j)
		h, e := rl.q.finish(j, a, base, sent, time.Now(), rl.holdConfig())
		if h != nil {
			rl.Log.Info("hold", "site", j.Target.Site(), "until", h.deadline.UTC().Format(time.RFC3339),
				"baseline", h.base.String())
		}
		rl.logEnded(e)
	}
}

// Shutdown stops accepting and releases every hold, then waits until every
// accepted push is forwarded or ctx ends. It returns, and logs by site name,
// what was not forwarded: sites still in flight (whose forwards it then
// cancels; git-pages may still publish them) and sites still queued. Only
// public repos are ever queued, so the names may be logged (D9).
func (rl *Relay) Shutdown(ctx context.Context) []string {
	rl.start()
	rl.logEnded(rl.q.close(time.Now()))
	rl.pcancel()
	rl.pollWG.Wait()
	drained := make(chan struct{})
	go func() { rl.wg.Wait(); close(drained) }()
	select {
	case <-drained:
		rl.cancel()
		rl.Log.Info("shutdown: drained")
		return nil
	case <-ctx.Done():
	}
	inFlight, waiting := rl.q.abandon()
	rl.cancel()
	var left []string
	for _, s := range inFlight {
		// git-pages runs an accepted update under context.WithoutCancel
		// (src/pages.go:925): canceling the POST does not stop it (review M6).
		rl.Log.Warn("may not have been forwarded at shutdown", "site", s, "state", "in_flight")
		left = append(left, s)
	}
	for _, s := range waiting {
		rl.Log.Warn("not forwarded at shutdown", "site", s, "state", "queued")
		left = append(left, s)
	}
	rl.Log.Warn("shutdown: drain timed out", "not_forwarded", len(left))
	return left
}

// count records an outcome and logs it.
func (rl *Relay) count(o Outcome, attrs ...any) {
	rl.Counters.Inc(o)
	rl.Log.Info("event", append([]any{"outcome", string(o)}, attrs...)...)
}

func (rl *Relay) done(w http.ResponseWriter, code int, o Outcome, msg string, attrs ...any) {
	rl.count(o, attrs...)
	w.WriteHeader(code)
	io.WriteString(w, msg)
}

// readBody reads the whole body into ONE buffer sized by Content-Length (cap
// MaxBody), so a request costs about one body of memory. Without a length it
// falls back to a growing read.
func readBody(w http.ResponseWriter, r *http.Request) ([]byte, error) {
	lr := http.MaxBytesReader(w, r.Body, MaxBody)
	if r.ContentLength < 0 {
		return io.ReadAll(lr)
	}
	if r.ContentLength > MaxBody {
		return nil, &http.MaxBytesError{Limit: MaxBody}
	}
	buf := make([]byte, r.ContentLength)
	if _, err := io.ReadFull(lr, buf); err != nil {
		return nil, err
	}
	return buf, nil
}

func (rl *Relay) busy(w http.ResponseWriter, attrs ...any) {
	w.Header().Set("Retry-After", "5")
	rl.done(w, http.StatusServiceUnavailable, Busy, "busy\n", attrs...)
}

func (rl *Relay) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/hook" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	rl.start()
	bt := rl.BodyTimeout
	if bt == 0 {
		bt = DefaultBodyTimeout
	}
	sw := rl.SlotWait
	if sw == 0 {
		sw = DefaultSlotWait
	}
	// A body slot: wait for one rather than refuse a burst, but not past
	// Forgejo's 5 s delivery timeout, nor once the sender has gone (review M2).
	// The signature cannot be checked first: it covers the whole body, and the
	// slot is what bounds reading bodies. A waiter holds no body memory.
	select {
	case rl.sem <- struct{}{}:
	default:
		t := time.NewTimer(sw)
		select {
		case rl.sem <- struct{}{}:
			t.Stop()
		case <-t.C:
			rl.busy(w, "reason", "body_slots")
			return
		case <-r.Context().Done():
			t.Stop()
			rl.count(Busy, "reason", "client_gone") // nobody is left to answer
			return
		}
	}
	var releaseOnce sync.Once
	release := func() { releaseOnce.Do(func() { <-rl.sem }) }
	defer release()
	// Errors (unsupported by recorders) are ignored; the server enforces it.
	_ = http.NewResponseController(w).SetReadDeadline(time.Now().Add(bt))
	body, err := readBody(w, r)
	if err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			rl.done(w, http.StatusRequestEntityTooLarge, TooLarge, "too_large\n")
			return
		}
		rl.done(w, http.StatusBadRequest, BadRequest, "bad_request\n")
		return
	}
	if !Verify(body, rl.Secret, r.Header.Get(SignatureHeader)) {
		rl.done(w, http.StatusUnauthorized, BadSignature, "bad_signature\n")
		return
	}
	evName := r.Header.Get("X-Forgejo-Event")
	ev, err := ParseEvent(evName, body)
	if err != nil {
		rl.done(w, http.StatusBadRequest, BadRequest, "bad_request\n")
		return
	}
	release() // the body is not needed any more: free its slot
	target, outcome := Route(ev, rl.Domain, rl.Forge)
	// Names are logged only for public repos (hidden orgs' repos are private).
	publishable := ev.Repository.Publishable() && target.Host != ""
	var attrs []any
	if publishable {
		attrs = []any{"site", target.Site(), "host", target.Host, "path", target.Path}
	}
	switch outcome {
	case Forwarded: // Route's "forward this": accept and queue it
		switch rl.q.put(job{Target: target, CloneURL: ev.Repository.CloneURL}) {
		case enqQueued:
			rl.done(w, http.StatusAccepted, Queued, "queued\n", append(attrs, "after", ev.After)...)
		case enqCoalesced:
			rl.done(w, http.StatusAccepted, Coalesced, "coalesced\n", append(attrs, "after", ev.After)...)
		case enqFull:
			rl.busy(w, append(attrs, "reason", "queue_full")...)
		default:
			rl.busy(w, append(attrs, "reason", "shutting_down")...)
		}
	case UnpublishRequested:
		// git-pages logs every request's Host and path (src/pages.go:971-975), so
		// a non-public repo's names never go there (D9): count it, fail loud.
		// While the relay still has a publish of the site queued, in flight or
		// held, git-pages' 404 proves nothing: that update may yet commit (M3).
		switch {
		case !publishable:
		case rl.q.has(target.Site()):
			attrs = append(attrs, "check", "publish_in_progress")
		case !rl.siteExists(target, attrs):
			rl.done(w, http.StatusOK, IgnoredNoSite, "ignored: no site\n", attrs...)
			return
		}
		rl.done(w, http.StatusOK, outcome, "unpublish_requested: an Owner must take the site down\n", attrs...)
	case BadRequest:
		rl.done(w, http.StatusBadRequest, outcome, "bad_request\n", attrs...)
	default:
		rl.done(w, http.StatusOK, outcome, "ignored: "+string(outcome)+"\n", attrs...)
	}
}

// siteExists asks git-pages, in-cluster, for the site's health
// (`/<repo>/.git-pages/health`, or `/.git-pages/health` for a root site, with
// Host <owner>.<domain>; src/pages.go:236-240). It needs no authorization and
// serializes nothing, and a missing project falls through to the index site as
// a file lookup, so it 404s like manifest.json did (review M4). Only a 404
// means "no site"; anything else, or no answer, counts as a site, so a takedown
// request is never lost to a failed check.
func (rl *Relay) siteExists(t Target, attrs []any) bool {
	h := rl.readHealth(context.Background(), t)
	switch {
	case h.err != nil:
		rl.Log.Warn("site check failed", append(attrs, "error", h.err.Error())...)
		return true
	case h.status == http.StatusOK:
		return true
	case h.status == http.StatusNotFound:
		return false
	default:
		rl.Log.Warn("site check failed", append(attrs, "status", h.status)...)
		return true
	}
}

// forward POSTs a minimal push for the job's site to git-pages, with the same
// headers as Forgejo (git-pages wants X-Forgejo-Event: push and exactly
// application/json, src/pages.go:845-872), counts forwarded or upstream_error,
// and returns what git-pages' answer says of the update (classify). git-pages
// answers when the update ends or after 3 s (202, the update goes on).
func (rl *Relay) forward(j job) answer {
	t := j.Target
	attrs := []any{"site", t.Site(), "host", t.Host, "path", t.Path}
	u := *rl.Upstream
	u.Path = strings.TrimRight(rl.Upstream.Path, "/") + t.Path
	req, err := http.NewRequestWithContext(rl.ctx, http.MethodPost, u.String(), bytes.NewReader(forwardBody(j.CloneURL)))
	if err != nil {
		rl.count(UpstreamError, append(attrs, "error", err.Error())...)
		return ansNone
	}
	req.Host = t.Host
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forgejo-Event", "push")
	start := time.Now()
	resp, err := rl.Client.Do(req)
	if err != nil {
		if rl.ctx.Err() != nil {
			return ansUnknown // the drain ran out of time; Shutdown logged the site
		}
		rl.count(UpstreamError, append(attrs, "error", err.Error())...)
		return ansUnknown
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
	o := Forwarded
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		o = UpstreamError
	}
	rl.count(o, append(attrs, "status", resp.StatusCode, "answer", strings.TrimSpace(string(body)),
		"seconds", time.Since(start).Seconds())...)
	return classify(resp.StatusCode, body)
}
