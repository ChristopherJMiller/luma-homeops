package main

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

// releaser is a gate a fake git-pages blocks on; opening it twice is fine.
type releaser struct {
	ch   chan struct{}
	once sync.Once
}

func newReleaser() *releaser { return &releaser{ch: make(chan struct{})} }

// gated is newHarness whose cleanup first opens gate, so a failed test never
// leaves a fake git-pages handler blocked (cleanups run last-in, first-out).
func gated(t *testing.T, gate *releaser, h http.HandlerFunc) *harness {
	t.Helper()
	hs := newHarness(t, h)
	t.Cleanup(gate.open)
	return hs
}

func (r *releaser) open() { r.once.Do(func() { close(r.ch) }) }

func (r *releaser) wait(req *http.Request) {
	select {
	case <-r.ch:
	case <-req.Context().Done():
	}
}

// pushTo signs and posts a real-shape push to the pages branch of owner/name.
func (h *harness) pushTo(t *testing.T, owner, name string) *httptest.ResponseRecorder {
	t.Helper()
	b := pushBody(repoJSON(owner, "public", name, false, false), 1)
	return h.post(t, b, "push", sign(b, h.relay.Secret))
}

// waitFor polls cond for up to 5 s.
func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(2 * time.Millisecond)
	}
}

func counted(m string, o Outcome, n int) bool {
	return strings.Contains(m, fmt.Sprintf(`pages_relay_events_total{outcome=%q} %d`+"\n", string(o), n))
}

// The live defect (Task 10 concern 1): forwards take 2-3 s, Forgejo never
// retries, and the third push in that window got 503 while the in-flight
// forward had cloned the older head. Five pushes to one site within 100 ms
// must all be accepted, never forward that site twice at once, and end with a
// forward that starts after the last push.
func TestBurstOneSiteCoalesces(t *testing.T) {
	gate := newReleaser()
	var mu sync.Mutex
	var starts []time.Time
	var bodies []string
	inFlight, maxInFlight := 0, 0
	h := gated(t, gate, func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		starts = append(starts, time.Now())
		bodies = append(bodies, string(b))
		inFlight++
		if inFlight > maxInFlight {
			maxInFlight = inFlight
		}
		mu.Unlock()
		gate.wait(r) // every forward is slow until the burst is over
		mu.Lock()
		inFlight--
		mu.Unlock()
	})
	codes := make(chan int, 5)
	var wg sync.WaitGroup
	var lastSent time.Time
	begin := time.Now()
	for i := 0; i < 5; i++ {
		if i == 1 {
			waitFor(t, "the first forward", func() bool { return h.hits.Load() >= 1 })
		}
		if i > 1 {
			time.Sleep(20 * time.Millisecond)
		}
		if i == 4 {
			lastSent = time.Now()
		}
		wg.Add(1)
		go func() { defer wg.Done(); codes <- h.pushTo(t, "org", "site").Code }()
	}
	burst := time.Since(begin)
	accepted := make(chan struct{})
	go func() { wg.Wait(); close(accepted) }()
	select {
	case <-accepted:
	case <-time.After(time.Second): // the old relay holds deliveries until git-pages answers
	}
	gate.open()
	<-accepted
	h.settle(t)
	close(codes)
	for c := range codes {
		if c != 202 {
			t.Errorf("delivery answered %d, want 202", c)
		}
	}
	if burst > 100*time.Millisecond {
		t.Logf("burst took %v", burst)
	}
	mu.Lock()
	defer mu.Unlock()
	if maxInFlight != 1 {
		t.Errorf("%d forwards of one site in flight at once", maxInFlight)
	}
	if len(starts) != 2 {
		t.Errorf("%d forwards, want 2 (the first push, then one for the four coalesced)", len(starts))
	}
	if n := len(starts); n == 0 || !starts[n-1].After(lastSent) {
		t.Errorf("the last forward did not start after the last push")
	}
	for _, b := range bodies {
		if b != minimalBody(forge+"org/site.git") {
			t.Errorf("forwarded %q", b)
		}
	}
	m := h.metrics()
	for o, n := range map[Outcome]int{Queued: 2, Coalesced: 3, Forwarded: 2, Busy: 0, UpstreamError: 0} {
		if !counted(m, o, n) {
			t.Errorf("want %s %d:\n%s", o, n, m)
		}
	}
}

// Busy only when 64 distinct sites already wait and the site is not one of
// them. Retry-After 5 as before.
func TestQueueFullBusy(t *testing.T) {
	gate := newReleaser()
	h := gated(t, gate, func(w http.ResponseWriter, r *http.Request) { gate.wait(r) })
	first := make(chan int, Workers)
	for i := 0; i < Workers; i++ {
		go func() { first <- h.pushTo(t, fmt.Sprintf("busy%d", i), "site").Code }()
	}
	waitFor(t, "both workers busy", func() bool { return h.hits.Load() == Workers })
	for i := Workers; i < Workers+QueueMax; i++ {
		if w := h.pushTo(t, fmt.Sprintf("busy%d", i), "site"); w.Code != 202 {
			t.Fatalf("site %d of %d: %d %q", i-Workers+1, QueueMax, w.Code, w.Body.String())
		}
	}
	if m := h.metrics(); !strings.Contains(m, fmt.Sprintf("\npages_relay_queue_depth %d\n", QueueMax)) {
		t.Errorf("depth gauge:\n%s", m)
	}
	// A site that already waits coalesces even when the queue is full.
	if w := h.pushTo(t, fmt.Sprintf("busy%d", Workers+5), "site"); w.Code != 202 || w.Body.String() != "coalesced\n" {
		t.Errorf("queued site: %d %q", w.Code, w.Body.String())
	}
	// A new site does not fit.
	w := h.pushTo(t, "overflow", "site")
	if w.Code != 503 || w.Header().Get("Retry-After") != "5" || !counted(h.metrics(), Busy, 1) {
		t.Errorf("65th site: %d %q\n%s", w.Code, w.Header().Get("Retry-After"), h.metrics())
	}
	// Neither does a second forward of a site being forwarded: it needs a slot.
	if w := h.pushTo(t, "busy0", "site"); w.Code != 503 || !counted(h.metrics(), Busy, 2) {
		t.Errorf("in-flight site: %d", w.Code)
	}
	gate.open()
	for i := 0; i < Workers; i++ {
		if c := <-first; c != 202 {
			t.Errorf("first deliveries: %d", c)
		}
	}
	h.settle(t)
	m := h.metrics()
	if !counted(m, Forwarded, Workers+QueueMax) || !strings.Contains(m, "\npages_relay_queue_depth 0\n") {
		t.Errorf("after drain:\n%s", m)
	}
}

// Two workers, many pushes to three sites: never one site twice at once, never
// more than two forwards, and each site's last forward starts after its last
// push was sent.
func TestPerSiteExclusivity(t *testing.T) {
	var mu sync.Mutex
	active := map[string]int{}
	lastStart := map[string]time.Time{}
	lastSent := map[string]time.Time{}
	global, maxGlobal, overlaps := 0, 0, 0
	h := newHarness(t, func(w http.ResponseWriter, r *http.Request) {
		site := r.Host + r.URL.Path
		mu.Lock()
		active[site]++
		if active[site] > 1 {
			overlaps++
		}
		global++
		if global > maxGlobal {
			maxGlobal = global
		}
		lastStart[site] = time.Now()
		mu.Unlock()
		time.Sleep(3 * time.Millisecond)
		mu.Lock()
		active[site]--
		global--
		mu.Unlock()
	})
	owners := []string{"a", "b", "c"}
	var wg sync.WaitGroup
	var bad atomic.Int32
	for g := 0; g < 6; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 30; i++ {
				o := owners[(g+i)%len(owners)]
				mu.Lock()
				lastSent[o+".realliance.page/site/"] = time.Now()
				mu.Unlock()
				if c := h.pushTo(t, o, "site").Code; c != 202 {
					bad.Add(1)
				}
				time.Sleep(time.Millisecond)
			}
		}()
	}
	wg.Wait()
	h.settle(t)
	mu.Lock()
	defer mu.Unlock()
	if bad.Load() != 0 {
		t.Errorf("%d deliveries not accepted", bad.Load())
	}
	if overlaps != 0 {
		t.Errorf("%d concurrent forwards of one site", overlaps)
	}
	if maxGlobal > Workers {
		t.Errorf("%d forwards at once, want <= %d", maxGlobal, Workers)
	}
	for site, sent := range lastSent {
		if !lastStart[site].After(sent) {
			t.Errorf("%s: last forward %v is not after the last push %v", site, lastStart[site], sent)
		}
	}
}

// A push that lands while its site is being forwarded waits (dirty) and is
// forwarded once that forward ends; the idle worker takes other sites meanwhile.
func TestDirtyRequeue(t *testing.T) {
	gate := newReleaser()
	var mu sync.Mutex
	var order []string
	h := gated(t, gate, func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		order = append(order, r.Host)
		n := len(order)
		mu.Unlock()
		if n == 1 {
			gate.wait(r)
		}
	})
	go h.pushTo(t, "a", "site")
	waitFor(t, "a in flight", func() bool { return h.hits.Load() == 1 })
	if w := h.pushTo(t, "a", "site"); w.Code != 202 || w.Body.String() != "queued\n" {
		t.Fatalf("dirty push: %d %q", w.Code, w.Body.String())
	}
	if w := h.pushTo(t, "b", "site"); w.Code != 202 {
		t.Fatalf("b: %d", w.Code)
	}
	waitFor(t, "b forwarded by the other worker", func() bool { return h.hits.Load() == 2 })
	time.Sleep(30 * time.Millisecond)
	mu.Lock()
	got := strings.Join(order, ",")
	mu.Unlock()
	if got != "a.realliance.page,b.realliance.page" {
		t.Fatalf("while a is in flight: %s", got)
	}
	if m := h.metrics(); !strings.Contains(m, "\npages_relay_queue_depth 1\n") {
		t.Errorf("a's second push should wait in the queue:\n%s", m)
	}
	gate.open()
	h.settle(t)
	mu.Lock()
	got = strings.Join(order, ",")
	mu.Unlock()
	if got != "a.realliance.page,b.realliance.page,a.realliance.page" {
		t.Errorf("order %s", got)
	}
	if !counted(h.metrics(), Forwarded, 3) {
		t.Error(h.metrics())
	}
}

// Shutdown stops accepting and forwards everything that was accepted.
func TestShutdownDrains(t *testing.T) {
	var forwarded atomic.Int32
	h := newHarness(t, func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(20 * time.Millisecond)
		forwarded.Add(1)
	})
	for i := 0; i < 6; i++ {
		if w := h.pushTo(t, fmt.Sprintf("s%d", i), "site"); w.Code != 202 {
			t.Fatalf("push %d: %d", i, w.Code)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if left := h.relay.Shutdown(ctx); len(left) != 0 {
		t.Errorf("left %v", left)
	}
	if forwarded.Load() != 6 || !counted(h.metrics(), Forwarded, 6) {
		t.Errorf("forwarded %d\n%s", forwarded.Load(), h.metrics())
	}
	if w := h.pushTo(t, "late", "site"); w.Code != 503 {
		t.Errorf("after shutdown: %d", w.Code)
	}
}

// Past the drain deadline Shutdown returns and logs, by site name, what it
// could not forward (in flight and still queued).
func TestShutdownDeadlineLogsPending(t *testing.T) {
	gate := newReleaser()
	h := gated(t, gate, func(w http.ResponseWriter, r *http.Request) { gate.wait(r) })
	go h.pushTo(t, "a", "site")
	go h.pushTo(t, "b", "pages")
	waitFor(t, "two forwards in flight", func() bool { return h.hits.Load() == 2 })
	for _, o := range []string{"c", "d"} {
		if w := h.pushTo(t, o, "site"); w.Code != 202 {
			t.Fatalf("%s: %d", o, w.Code)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	start := time.Now()
	left := h.relay.Shutdown(ctx)
	if time.Since(start) > 2*time.Second {
		t.Errorf("Shutdown took %v past its deadline", time.Since(start))
	}
	sort.Strings(left)
	want := []string{"a.realliance.page/site", "b.realliance.page/.index", "c.realliance.page/site", "d.realliance.page/site"}
	if strings.Join(left, ",") != strings.Join(want, ",") {
		t.Errorf("left %v, want %v", left, want)
	}
	l := h.log.String()
	for _, s := range want {
		if !strings.Contains(l, `"site":"`+s+`"`) {
			t.Errorf("log does not name %s: %s", s, l)
		}
	}
	if !strings.Contains(l, "not forwarded at shutdown") {
		t.Errorf("log: %s", l)
	}
}

func freePort(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	return strconv.Itoa(l.Addr().(*net.TCPAddr).Port)
}

// The binary itself: SIGTERM stops accepting, drains, logs and exits 0.
func TestMainDrainsOnSIGTERM(t *testing.T) {
	if os.Getenv("PAGES_RELAY_CHILD") == "sigterm" {
		main()
		return
	}
	var got atomic.Int32
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(300 * time.Millisecond)
		got.Add(1)
	}))
	defer up.Close()
	dir := t.TempDir()
	secret := []byte("s3cret")
	if err := os.WriteFile(dir+"/secret", secret, 0o600); err != nil {
		t.Fatal(err)
	}
	port := freePort(t)
	cmd := exec.Command(os.Args[0], "-test.run=^TestMainDrainsOnSIGTERM$")
	cmd.Env = append(os.Environ(), "PAGES_RELAY_CHILD=sigterm", "PAGES_RELAY_SECRET_FILE="+dir+"/secret",
		"PAGES_RELAY_UPSTREAM="+up.URL, "PAGES_RELAY_LISTEN=127.0.0.1:"+port)
	out := new(syncBuffer)
	cmd.Stdout, cmd.Stderr = out, out
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	exited := make(chan error, 1)
	go func() { exited <- cmd.Wait() }()
	defer func() {
		cmd.Process.Kill()
		select {
		case <-exited:
		case <-time.After(5 * time.Second):
		}
	}()
	base := "http://127.0.0.1:" + port
	waitFor(t, "the relay to listen", func() bool {
		resp, err := http.Get(base + "/healthz")
		if err == nil {
			resp.Body.Close()
		}
		return err == nil && resp.StatusCode == 200
	})
	for i := 0; i < 3; i++ {
		b := pushBody(repoJSON(fmt.Sprintf("s%d", i), "public", "site", false, false), 1)
		req, _ := http.NewRequest(http.MethodPost, base+"/hook", bytes.NewReader(b))
		req.Header.Set("X-Forgejo-Event", "push")
		req.Header.Set(SignatureHeader, sign(b, secret))
		resp, err := (&http.Client{Timeout: 5 * time.Second}).Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != 202 {
			t.Fatalf("push %d: %d", i, resp.StatusCode)
		}
	}
	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-exited:
		if err != nil {
			t.Fatalf("exit: %v\n%s", err, out.String())
		}
	case <-time.After(10 * time.Second):
		t.Fatalf("no exit 10 s after SIGTERM\n%s", out.String())
	}
	if got.Load() != 3 {
		t.Errorf("git-pages got %d forwards, want 3\n%s", got.Load(), out.String())
	}
	if l := out.String(); !strings.Contains(l, `"msg":"shutdown: drained"`) {
		t.Errorf("log: %s", l)
	}
}

// Takedown noise (Task 10 concern 2): a deleted pages branch or repo is a
// takedown request only if git-pages has a site for it.
func TestUnpublishSiteCheck(t *testing.T) {
	type seen struct{ method, host, path, cache string }
	for _, tc := range []struct {
		status int
		want   Outcome
	}{
		{200, UnpublishRequested},
		{404, IgnoredNoSite},
		{401, UnpublishRequested}, // not a "no": fail loud
		{500, UnpublishRequested},
	} {
		var mu sync.Mutex
		var got []seen
		h := newHarness(t, func(w http.ResponseWriter, r *http.Request) {
			mu.Lock()
			got = append(got, seen{r.Method, r.Host, r.URL.Path, r.Header.Get("Cache-Control")})
			mu.Unlock()
			w.WriteHeader(tc.status)
		})
		repo := repoJSON("Org", "public", "Site", false, false)
		root := repoJSON("Org", "public", "pages", false, false)
		events := []struct {
			name string
			body []byte
		}{
			{"delete", deleteBranchBody(repo)},
			{"repository", repoDeletedBody(repo)},
			{"delete", deleteBranchBody(root)},
		}
		for _, e := range events {
			if w := h.post(t, e.body, e.name, sign(e.body, h.relay.Secret)); w.Code != 200 {
				t.Errorf("%d: %s answered %d", tc.status, e.name, w.Code)
			}
		}
		if m := h.metrics(); !counted(m, tc.want, 3) {
			t.Errorf("git-pages %d: want %s 3:\n%s", tc.status, tc.want, m)
		}
		mu.Lock()
		want := []seen{
			{"GET", "org.realliance.page", "/site/.git-pages/manifest.json", "no-cache"},
			{"GET", "org.realliance.page", "/site/.git-pages/manifest.json", "no-cache"},
			{"GET", "org.realliance.page", "/.git-pages/manifest.json", "no-cache"},
		}
		if fmt.Sprint(got) != fmt.Sprint(want) {
			t.Errorf("git-pages %d saw %v", tc.status, got)
		}
		mu.Unlock()
	}
}

// If the check cannot answer, count the takedown request (fail loud).
func TestUnpublishCheckFailsLoud(t *testing.T) {
	body := deleteBranchBody(repoJSON("org", "public", "site", false, false))

	h := newHarness(t, nil)
	h.up.Close() // transport error
	if w := h.post(t, body, "delete", sign(body, h.relay.Secret)); w.Code != 200 || !counted(h.metrics(), UnpublishRequested, 1) {
		t.Errorf("unreachable: %d\n%s", w.Code, h.metrics())
	}

	gate := newReleaser()
	h = gated(t, gate, func(w http.ResponseWriter, r *http.Request) { gate.wait(r) }) // hangs
	h.relay.CheckTimeout = 100 * time.Millisecond
	start := time.Now()
	if w := h.post(t, body, "delete", sign(body, h.relay.Secret)); w.Code != 200 || !counted(h.metrics(), UnpublishRequested, 1) {
		t.Errorf("hung: %d\n%s", w.Code, h.metrics())
	}
	if d := time.Since(start); d > time.Second {
		t.Errorf("check took %v", d)
	}
	if SiteCheckTimeout > 3*time.Second {
		t.Errorf("SiteCheckTimeout %v must leave room in Forgejo's 5 s delivery timeout", SiteCheckTimeout)
	}
}

// gitPagesIntake mirrors git-pages 0.10.1's webhook intake for the
// cluster/pages/config.toml [[wildcard]] (clone-url
// https://git.realliance.net/<user>/<project>.git, index-repo "pages"):
//   - postPage, src/pages.go:845-892: event header order, "push" only,
//     Content-Type exactly application/json, 16 MiB, the same decode struct, ref
//     must be refs/heads/<branch>;
//   - GetSite :95 + names.ParseProjectURL (src/names/project.go:87): "/" is the
//     index project, "/<p>/" a project, no leading dot;
//   - authorizeWildcardMatchSite + ApplyTemplate (src/auth.go:216,
//     src/wildcard.go:68) and AuthorizeRepository (src/auth.go:450): the clone URL
//     equals the template, case-insensitively.
//
// It answers 200 where git-pages would start the update.
func gitPagesIntake(r *http.Request) (int, string) {
	eventName := ""
	for _, header := range []string{"X-Forgejo-Event", "X-GitHub-Event", "X-Gitea-Event", "X-Gogs-Event"} {
		if eventName = r.Header.Get(header); eventName != "" {
			break
		}
	}
	if eventName == "" {
		return 400, "event expected"
	}
	if eventName != "push" {
		return 400, "only push events are allowed"
	}
	if r.Header.Get("Content-Type") != "application/json" {
		return 400, "only JSON payload is allowed"
	}
	body, err := io.ReadAll(http.MaxBytesReader(nil, r.Body, 16*1048576))
	if err != nil {
		return 413, "body read"
	}
	var event struct {
		Ref        string `json:"ref"`
		Repository struct {
			CloneURL string `json:"clone_url"`
		} `json:"repository"`
	}
	if err := json.NewDecoder(bytes.NewReader(body)).Decode(&event); err != nil {
		return 400, "invalid request body"
	}
	user, ok := strings.CutSuffix(strings.ToLower(r.Host), ".realliance.page")
	if !ok || user == "" || strings.Contains(user, ".") {
		return 401, "domain does not match wildcard"
	}
	p := strings.TrimPrefix(strings.TrimSuffix(r.URL.Path, "/"), "/")
	project := p
	switch {
	case strings.Contains(p, "/"):
		return 400, "must contain zero or one path segments"
	case p == "":
		project = "pages" // index-repo
	case strings.HasPrefix(p, "."):
		return 400, "must not start with ."
	}
	if event.Ref != "refs/heads/pages" {
		return 401, "ref not in allowlist"
	}
	want := "https://git.realliance.net/" + user + "/" + project + ".git"
	if strings.ToLower(event.Repository.CloneURL) != want {
		return 401, "repository not allowed"
	}
	return 200, "accepted"
}

// The minimal forward passes git-pages' own intake, for a project and a root
// site, and carries nothing else.
func TestForwardMinimalPayloadAccepted(t *testing.T) {
	var mu sync.Mutex
	answers := map[string]string{}
	h := newHarness(t, func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		r.Body = io.NopCloser(bytes.NewReader(b))
		code, msg := gitPagesIntake(r)
		var keys map[string]json.RawMessage
		json.Unmarshal(b, &keys)
		mu.Lock()
		answers[r.Host+r.URL.Path] = fmt.Sprintf("%d %s keys=%d dry-run=%q", code, msg, len(keys), r.Header.Get("Dry-Run"))
		mu.Unlock()
		w.WriteHeader(code)
	})
	h.pushTo(t, "Org", "My-Site")
	h.pushTo(t, "Org", "pages")
	h.settle(t)
	want := map[string]string{
		"org.realliance.page/my-site/": `200 accepted keys=2 dry-run=""`,
		"org.realliance.page/":         `200 accepted keys=2 dry-run=""`,
	}
	mu.Lock()
	if fmt.Sprint(answers) != fmt.Sprint(want) {
		t.Errorf("git-pages intake answered %v", answers)
	}
	mu.Unlock()
	if !counted(h.metrics(), Forwarded, 2) {
		t.Error(h.metrics())
	}
	// The mirror is not a rubber stamp.
	for name, req := range map[string]*http.Request{
		"wrong repo": httptest.NewRequest("POST", "/my-site/", strings.NewReader(minimalBody(forge+"Org/other.git"))),
		"wrong ref":  httptest.NewRequest("POST", "/my-site/", strings.NewReader(`{"ref":"refs/heads/main","repository":{"clone_url":"`+forge+`Org/My-Site.git"}}`)),
		"no type":    httptest.NewRequest("POST", "/my-site/", strings.NewReader(minimalBody(forge+"Org/My-Site.git"))),
	} {
		req.Host = "org.realliance.page"
		req.Header.Set("X-Forgejo-Event", "push")
		if name != "no type" {
			req.Header.Set("Content-Type", "application/json")
		}
		if code, _ := gitPagesIntake(req); code == 200 {
			t.Errorf("%s: mirror accepted it", name)
		}
	}
}

// dryRunTransport adds git-pages' Dry-Run header to POSTs, so a real git-pages
// authorizes the forward and stops before cloning (src/pages.go:910, :528),
// and records what it answered.
type dryRunTransport struct {
	mu   sync.Mutex
	seen map[string]string
}

func (d *dryRunTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if r.Method == http.MethodPost {
		r = r.Clone(r.Context())
		r.Header.Set("Dry-Run", "1")
	}
	resp, err := http.DefaultTransport.RoundTrip(r)
	if err != nil || r.Method != http.MethodPost {
		return resp, err
	}
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	resp.Body = io.NopCloser(bytes.NewReader(b))
	d.mu.Lock()
	d.seen[r.Host+r.URL.Path] = fmt.Sprintf("%d %s", resp.StatusCode, strings.TrimSpace(string(b)))
	d.mu.Unlock()
	return resp, nil
}

func siteTar(t *testing.T, path string) {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	content := []byte("<p>hello</p>\n")
	tw.WriteHeader(&tar.Header{Name: "index.html", Mode: 0o644, Size: int64(len(content))})
	tw.Write(content)
	tw.Close()
	if err := os.WriteFile(path, buf.Bytes(), 0o644); err != nil {
		t.Fatal(err)
	}
}

// Contract test against a real git-pages binary (build the pinned tag, v0.10.1,
// and set PAGES_RELAY_GIT_PAGES to it). CI skips it; the report records a run.
func TestContractGitPages(t *testing.T) {
	bin := os.Getenv("PAGES_RELAY_GIT_PAGES")
	if bin == "" {
		t.Skip("PAGES_RELAY_GIT_PAGES is not set")
	}
	dir := t.TempDir()
	port := freePort(t)
	cfg := filepath.Join(dir, "config.toml")
	// The [[wildcard]] and allowed prefixes of cluster/pages/config.toml, on an fs backend.
	os.WriteFile(cfg, []byte(fmt.Sprintf(`log-format = "text"
features = []

[server]
pages = "tcp/127.0.0.1:%s"
proxy = "-"
metrics = "-"

[[wildcard]]
domain = "realliance.page"
clone-url = "https://git.realliance.net/<user>/<project>.git"
index-repo = "pages"
index-repo-branch = "pages"

[storage]
type = "fs"

[storage.fs]
root = %q

[secrets]
type = "fs"

[secrets.fs]
root = %q

[limits]
allowed-repository-url-prefixes = ["https://git.realliance.net/"]
`, port, filepath.Join(dir, "data"), filepath.Join(dir, "secrets"))), 0o600)
	archive := filepath.Join(dir, "site.tar")
	siteTar(t, archive)
	for _, site := range []string{"org.realliance.page/site", "org.realliance.page/.index"} {
		cmd := exec.Command(bin, "-config", cfg, "-update-site", site, archive)
		cmd.Dir = dir
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("-update-site %s: %v\n%s", site, err, out)
		}
	}
	srv := exec.Command(bin, "-config", cfg)
	srv.Dir = dir
	srvLog := new(syncBuffer)
	srv.Stdout, srv.Stderr = srvLog, srvLog
	if err := srv.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { srv.Process.Kill(); srv.Wait() })
	base := "http://127.0.0.1:" + port
	waitFor(t, "git-pages to listen", func() bool {
		c, err := net.Dial("tcp", "127.0.0.1:"+port)
		if err == nil {
			c.Close()
		}
		return err == nil
	})
	u, _ := url.Parse(base)
	tr := &dryRunTransport{seen: map[string]string{}}
	buf := new(syncBuffer)
	rl := &Relay{Secret: []byte("s3cret"), Upstream: u, Domain: domain, Forge: forge,
		Client: &http.Client{Transport: tr, Timeout: 10 * time.Second}, Counters: NewCounters(),
		Log: slog.New(slog.NewJSONHandler(buf, nil))}
	h := &harness{relay: rl, hits: new(atomic.Int32), gets: new(atomic.Int32), log: buf}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		rl.Shutdown(ctx)
	})

	// Forwards: an existing project site, an existing root site, a first publish.
	for _, r := range [][2]string{{"Org", "Site"}, {"Org", "pages"}, {"New-Owner", "fresh"}} {
		if w := h.pushTo(t, r[0], r[1]); w.Code != 202 {
			t.Fatalf("%s/%s: %d", r[0], r[1], w.Code)
		}
	}
	h.settle(t)
	want := map[string]string{
		"org.realliance.page/site/":        "200 dry-run ok",
		"org.realliance.page/":             "200 dry-run ok",
		"new-owner.realliance.page/fresh/": "200 dry-run ok",
	}
	tr.mu.Lock()
	if fmt.Sprint(tr.seen) != fmt.Sprint(want) {
		t.Errorf("git-pages answered %v", tr.seen)
	}
	tr.mu.Unlock()
	if m := h.metrics(); !counted(m, Forwarded, 3) || !counted(m, UpstreamError, 0) {
		t.Errorf("metrics:\n%s", m)
	}

	// Site checks: two sites exist, two do not.
	for _, tc := range []struct {
		owner, name string
		want        Outcome
	}{
		{"Org", "Site", UnpublishRequested},
		{"Org", "pages", UnpublishRequested},
		{"New-Owner", "fresh", IgnoredNoSite},
		{"New-Owner", "pages", IgnoredNoSite},
	} {
		before := h.metrics()
		b := deleteBranchBody(repoJSON(tc.owner, "public", tc.name, false, false))
		h.post(t, b, "delete", sign(b, rl.Secret))
		after := h.metrics()
		if outcomeDelta(before, after, tc.want) != 1 {
			t.Errorf("%s/%s: want %s\nbefore %s\nafter %s", tc.owner, tc.name, tc.want, before, after)
		}
	}

	// git-pages itself refuses what the relay never sends.
	for name, tc := range map[string]struct {
		body, ctype string
		want        int
	}{
		"wrong repo": {minimalBody(forge + "Org/other.git"), "application/json", 401},
		"wrong ref":  {`{"ref":"refs/heads/main","repository":{"clone_url":"` + forge + `Org/Site.git"}}`, "application/json", 401},
		"no type":    {minimalBody(forge + "Org/Site.git"), "", 400},
	} {
		req, _ := http.NewRequest(http.MethodPost, base+"/site/", strings.NewReader(tc.body))
		req.Host = "org.realliance.page"
		req.Header.Set("X-Forgejo-Event", "push")
		req.Header.Set("Dry-Run", "1")
		if tc.ctype != "" {
			req.Header.Set("Content-Type", tc.ctype)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != tc.want {
			t.Errorf("%s: git-pages answered %d, want %d", name, resp.StatusCode, tc.want)
		}
	}
	t.Logf("git-pages log:\n%s", srvLog.String())
}

// outcomeDelta is how much outcome o grew between two /metrics snapshots.
func outcomeDelta(before, after string, o Outcome) int {
	val := func(m string) int {
		prefix := fmt.Sprintf("pages_relay_events_total{outcome=%q} ", string(o))
		for _, l := range strings.Split(m, "\n") {
			if v, ok := strings.CutPrefix(l, prefix); ok {
				n, _ := strconv.Atoi(v)
				return n
			}
		}
		return -1
	}
	return val(after) - val(before)
}
