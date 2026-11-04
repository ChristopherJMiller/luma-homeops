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
// buffer). A delivery waits up to its body timeout for a slot; only then is it
// refused as busy.
const MaxInFlight = 2

// DefaultBodyTimeout bounds waiting for a body slot, then reading the body.
const DefaultBodyTimeout = 10 * time.Second

// Workers forward queued sites to git-pages, at most one forward per site.
const Workers = 2

// QueueMax bounds the sites waiting for a forward. A push to a site that is
// not already waiting is refused as busy (503) only when this many wait.
const QueueMax = 64

// DrainTimeout is how long SIGTERM waits for the queue to drain: 66 forwards
// (64 queued + 2 in flight) on 2 workers at git-pages' 3 s answer
// (src/pages.go:832) is about 100 s. terminationGracePeriodSeconds (120 in
// cluster/pages/pages-relay.yaml) must exceed it.
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

type Counters struct {
	mu    sync.Mutex
	n     map[Outcome]uint64
	depth int
}

func NewCounters() *Counters { return &Counters{n: map[Outcome]uint64{}} }

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

// WriteTo writes every outcome (from zero, so increase() sees the first event)
// and the queue depth in the Prometheus text format.
func (c *Counters) WriteTo(w io.Writer) (int64, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	var b strings.Builder
	b.WriteString("# TYPE pages_relay_events_total counter\n")
	for _, o := range allOutcomes {
		fmt.Fprintf(&b, "pages_relay_events_total{outcome=%q} %d\n", string(o), c.n[o])
	}
	b.WriteString("# TYPE pages_relay_queue_depth gauge\n")
	fmt.Fprintf(&b, "pages_relay_queue_depth %d\n", c.depth)
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
// order. take never hands out a site that is being forwarded: a push that lands
// during its site's forward waits as a pending ("dirty") entry until that
// forward ends, then goes again, so git-pages refetches the head after the last
// push. At most max sites wait; dirty entries count too.
type queue struct {
	mu       sync.Mutex
	wake     *sync.Cond
	max      int
	order    []string       // waiting sites, first arrival first
	pending  map[string]job // site -> latest job
	inFlight map[string]bool
	closed   bool
	depth    func(int)
}

func newQueue(max int, depth func(int)) *queue {
	q := &queue{max: max, pending: map[string]job{}, inFlight: map[string]bool{}, depth: depth}
	q.wake = sync.NewCond(&q.mu)
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
	if len(q.pending) >= q.max {
		return enqFull
	}
	q.pending[site] = j
	q.order = append(q.order, site)
	q.depth(len(q.pending))
	q.wake.Broadcast()
	return enqQueued
}

// take blocks until a waiting site is not being forwarded and hands it out, or
// returns false once the queue is closed and empty.
func (q *queue) take() (job, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	for {
		for i, site := range q.order {
			if q.inFlight[site] {
				continue
			}
			j := q.pending[site]
			delete(q.pending, site)
			q.order = append(q.order[:i:i], q.order[i+1:]...)
			q.inFlight[site] = true
			q.depth(len(q.pending))
			return j, true
		}
		if q.closed && len(q.pending) == 0 {
			return job{}, false
		}
		q.wake.Wait()
	}
}

// done ends a site's forward; its dirty entry, if any, may now go.
func (q *queue) done(site string) {
	q.mu.Lock()
	delete(q.inFlight, site)
	q.wake.Broadcast()
	q.mu.Unlock()
}

func (q *queue) close() {
	q.mu.Lock()
	q.closed = true
	q.wake.Broadcast()
	q.mu.Unlock()
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
	q.depth(0)
	q.wake.Broadcast()
	return inFlight, waiting
}

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

	BodyTimeout  time.Duration // 0: DefaultBodyTimeout
	CheckTimeout time.Duration // 0: SiteCheckTimeout

	once   sync.Once
	sem    chan struct{}
	q      *queue
	wg     sync.WaitGroup
	ctx    context.Context // forwards; canceled when a drain runs out of time
	cancel context.CancelFunc
}

// start creates the queue and its workers on first use.
func (rl *Relay) start() {
	rl.once.Do(func() {
		rl.sem = make(chan struct{}, MaxInFlight)
		rl.q = newQueue(QueueMax, rl.Counters.SetQueueDepth)
		rl.ctx, rl.cancel = context.WithCancel(context.Background())
		for i := 0; i < Workers; i++ {
			rl.wg.Add(1)
			go rl.work()
		}
	})
}

func (rl *Relay) idle() bool {
	rl.start()
	return rl.q.idle()
}

func (rl *Relay) work() {
	defer rl.wg.Done()
	for {
		j, ok := rl.q.take()
		if !ok {
			return
		}
		rl.forward(j)
		rl.q.done(j.Target.Site())
	}
}

// Shutdown stops accepting, then waits until every accepted push is forwarded
// or ctx ends. It returns, and logs by site name, what was not forwarded:
// sites still in flight (whose forwards it then cancels) and sites still queued.
// Only public repos are ever queued, so the names may be logged (D9).
func (rl *Relay) Shutdown(ctx context.Context) []string {
	rl.start()
	rl.q.close()
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
		rl.Log.Warn("not forwarded at shutdown", "site", s, "state", "in_flight")
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
	// A body slot: wait for one rather than refuse a burst.
	select {
	case rl.sem <- struct{}{}:
	default:
		t := time.NewTimer(bt)
		select {
		case rl.sem <- struct{}{}:
			t.Stop()
		case <-t.C:
			rl.busy(w, "reason", "body_slots")
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
		if publishable && !rl.siteExists(target, attrs) {
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

// siteExists asks git-pages, in-cluster, for the site's manifest
// (`/<repo>/.git-pages/manifest.json`, or `/.git-pages/manifest.json` for a root
// site, with Host <owner>.<domain>; wildcard sites need no authorization,
// src/auth.go:307-339). Only a 404 means "no site"; anything else, or no answer,
// counts as a site, so a takedown request is never lost to a failed check.
func (rl *Relay) siteExists(t Target, attrs []any) bool {
	ct := rl.CheckTimeout
	if ct == 0 {
		ct = SiteCheckTimeout
	}
	ctx, cancel := context.WithTimeout(context.Background(), ct)
	defer cancel()
	u := *rl.Upstream
	u.Path = strings.TrimRight(rl.Upstream.Path, "/") + t.Path + ".git-pages/manifest.json"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return true
	}
	req.Host = t.Host
	req.Header.Set("Cache-Control", "no-cache") // git-pages bypasses its manifest cache
	resp, err := rl.Client.Do(req)
	if err != nil {
		rl.Log.Warn("site check failed", append(attrs, "error", err.Error())...)
		return true
	}
	io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
	resp.Body.Close()
	switch resp.StatusCode {
	case http.StatusOK:
		return true
	case http.StatusNotFound:
		return false
	default:
		rl.Log.Warn("site check failed", append(attrs, "status", resp.StatusCode)...)
		return true
	}
}

// forward POSTs a minimal push for the job's site to git-pages, with the same
// headers as Forgejo (git-pages wants X-Forgejo-Event: push and exactly
// application/json, src/pages.go:845-872), and counts forwarded or
// upstream_error. git-pages answers when the update ends or after 3 s
// (202, the update goes on).
func (rl *Relay) forward(j job) {
	t := j.Target
	attrs := []any{"site", t.Site(), "host", t.Host, "path", t.Path}
	u := *rl.Upstream
	u.Path = strings.TrimRight(rl.Upstream.Path, "/") + t.Path
	req, err := http.NewRequestWithContext(rl.ctx, http.MethodPost, u.String(), bytes.NewReader(forwardBody(j.CloneURL)))
	if err != nil {
		rl.count(UpstreamError, append(attrs, "error", err.Error())...)
		return
	}
	req.Host = t.Host
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forgejo-Event", "push")
	start := time.Now()
	resp, err := rl.Client.Do(req)
	if err != nil {
		if rl.ctx.Err() != nil {
			return // the drain ran out of time; Shutdown logged the site
		}
		rl.count(UpstreamError, append(attrs, "error", err.Error())...)
		return
	}
	defer resp.Body.Close()
	answer, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
	o := Forwarded
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		o = UpstreamError
	}
	rl.count(o, append(attrs, "status", resp.StatusCode, "answer", strings.TrimSpace(string(answer)),
		"seconds", time.Since(start).Seconds())...)
}
