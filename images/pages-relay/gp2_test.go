package main

// A harsher git-pages model than hold_test.go's (review of fix 2, M3), and the
// tests that need it: Last-Modified at 1 s resolution, an ETag that an unchanged
// re-publish keeps, and the update's tail after its commit.

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// gp2 is a harsher git-pages model than hold_test.go's fakePages:
//   - Last-Modified has the real format's 1 s resolution, scaled: res is one
//     "second", so the 202 timer (3 res) is the real 3 s;
//   - the ETag is the committed head, so an unchanged re-publish keeps its ETag
//     and only LM can move (as on B2, where the ETag is the content's MD5);
//   - an update past updTimeout dies without committing (update-timeout);
//   - after the commit the update still runs postCommit (CreateDomain, AddSite)
//     before the site appears in health for a first publish and before the
//     counter counts it (pages.go:922-924);
//   - stale counts commits of a head older than one already committed: the
//     user-visible symptom of I1.
type gp2 struct {
	timer, res, updTimeout time.Duration
	epoch                  time.Time

	mu         sync.Mutex
	head       map[string]int
	committed  map[string]int
	maxCommit  map[string]int
	exists     map[string]bool
	lmAt       map[string]time.Time
	running    map[string]int
	updates    map[string]int
	overlaps   int
	stale      int
	ended      int
	start      float64
	altStart   float64 // nonzero: every other /metrics read reports this start (two pods)
	reads      int
	accepted   int
	failNext   func(site string) bool
	duration   func(site string) time.Duration
	postCommit func() time.Duration
	bg         sync.WaitGroup
}

func newGP2(timer, res, updTimeout time.Duration) *gp2 {
	return &gp2{timer: timer, res: res, updTimeout: updTimeout, epoch: time.Now(),
		head: map[string]int{}, committed: map[string]int{}, maxCommit: map[string]int{}, exists: map[string]bool{},
		lmAt: map[string]time.Time{}, running: map[string]int{}, updates: map[string]int{},
		start:      float64(time.Now().Unix()),
		failNext:   func(string) bool { return false },
		duration:   func(string) time.Duration { return 0 },
		postCommit: func() time.Duration { return 0 }}
}

func (f *gp2) key(r *http.Request) string {
	return r.Host + strings.TrimSuffix(r.URL.Path, ".git-pages/health")
}

func (f *gp2) bump(owner string) {
	f.mu.Lock()
	f.head[owner+"."+domain+"/site/"]++
	f.mu.Unlock()
}

// lm formats t in synthetic seconds: one res of real time is one second.
func (f *gp2) lm(t time.Time) string {
	sec := int64(t.Sub(f.epoch) / f.res)
	return time.Unix(1_790_000_000+sec, 0).UTC().Format(http.TimeFormat)
}

func (f *gp2) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/metrics":
		f.mu.Lock()
		f.reads++
		start := f.start
		if f.altStart != 0 && f.reads%2 == 0 {
			start = f.altStart
		}
		if f.ended > 0 {
			fmt.Fprintf(w, "git_pages_site_updates{via=\"webhook\"} %d\n", f.ended)
		}
		fmt.Fprintf(w, "process_start_time_seconds %g\n", start)
		f.mu.Unlock()
	case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/.git-pages/health"):
		k := f.key(r)
		f.mu.Lock()
		ok, at, c := f.exists[k], f.lmAt[k], f.committed[k]
		f.mu.Unlock()
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			io.WriteString(w, "site not found\n")
			return
		}
		w.Header().Set("Last-Modified", f.lm(at))
		w.Header().Set("ETag", fmt.Sprintf(`"%d"`, c))
		io.WriteString(w, "ok\n")
	case r.Method == http.MethodPost:
		f.update(w, r)
	}
}

func (f *gp2) update(w http.ResponseWriter, r *http.Request) {
	io.Copy(io.Discard, r.Body)
	timer := time.NewTimer(f.timer)
	site := f.key(r)
	f.mu.Lock()
	f.running[site]++
	if f.running[site] > 1 {
		f.overlaps++
	}
	f.updates[site]++
	v := f.head[site] // the clone
	d, fail, pc := f.duration(site), f.failNext(site), f.postCommit()
	f.mu.Unlock()
	done := make(chan string, 1)
	f.bg.Add(1)
	go func() {
		defer f.bg.Done()
		res := "replaced\n"
		if d >= f.updTimeout {
			time.Sleep(f.updTimeout)
			res = "timeout"
		} else {
			time.Sleep(d)
			if fail {
				res = "fail"
			} else {
				f.mu.Lock()
				if v < f.maxCommit[site] {
					f.stale++
				}
				if v > f.maxCommit[site] {
					f.maxCommit[site] = v
				}
				f.committed[site] = v
				f.lmAt[site] = time.Now()
				existed := f.exists[site]
				f.running[site]-- // past the commit: the tail cannot reorder commits
				f.mu.Unlock()
				time.Sleep(pc) // CreateDomain, AddSite
				if !existed {
					f.mu.Lock()
					f.exists[site] = true
					f.mu.Unlock()
				}
			}
		}
		if res != "replaced\n" {
			f.mu.Lock()
			f.running[site]--
			f.mu.Unlock()
		}
		done <- res
		f.mu.Lock()
		f.ended++
		f.mu.Unlock()
	}()
	select {
	case res := <-done:
		switch res {
		case "fail":
			w.WriteHeader(http.StatusServiceUnavailable)
			io.WriteString(w, "update error: boom\n")
		case "timeout":
			w.WriteHeader(http.StatusGatewayTimeout)
			io.WriteString(w, "update timeout\n")
		default:
			io.WriteString(w, res)
		}
	case <-timer.C:
		f.mu.Lock()
		f.accepted++
		f.mu.Unlock()
		w.WriteHeader(http.StatusAccepted)
		io.WriteString(w, "updating (taking longer than 3s)")
	}
}

func gp2Harness(t *testing.T, f *gp2, cfg HoldConfig, counter bool) *harness {
	t.Helper()
	srv := httptest.NewServer(f)
	t.Cleanup(func() { srv.Close(); f.bg.Wait() })
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
	return &harness{relay: rl, log: buf, up: srv}
}

// Scale: 1 ms of test time is 1 s of production time.
func scaledHold() HoldConfig {
	return HoldConfig{Timeout: 300 * time.Millisecond, PollGap: time.Millisecond, PollJitter: time.Millisecond,
		HealthFirst: 2 * time.Millisecond, HealthEvery: 10 * time.Millisecond, CounterEvery: 3 * time.Millisecond,
		LedgerQuiet: 30 * time.Millisecond, LedgerWarmup: 20 * time.Millisecond}
}

func settleHeld(t *testing.T, h *harness, d time.Duration) {
	t.Helper()
	deadline := time.Now().Add(d)
	for !h.relay.idle() || h.held() != "0" {
		if time.Now().After(deadline) {
			t.Fatalf("never settled:\n%s", h.metrics())
		}
		time.Sleep(2 * time.Millisecond)
	}
}

// The stress test with holds, at production ratios: 1 s LM resolution, a 3 s
// 202 timer, updates of 0-20 s and some of 20-200 s, 5 min update-timeout,
// unchanged re-publishes (same ETag), and optionally failures, timeouts and
func TestLedgerLateIncrement(t *testing.T) {
	f := newGP2(3*time.Millisecond, time.Millisecond, 2*time.Second)
	var n atomic.Int32
	f.duration = func(site string) time.Duration {
		switch n.Add(1) {
		case 1:
			return 0 // site a exists already (answered 200)
		case 2:
			return 10 * time.Millisecond // site a: 202, commits at 10 ms
		case 3:
			return 600 * time.Millisecond // site b: a long update
		default:
			return time.Millisecond // b's quick fix
		}
	}
	var pc atomic.Int32
	f.postCommit = func() time.Duration {
		if pc.Add(1) == 2 {
			return 200 * time.Millisecond // a's CreateDomain is slow: B2 is having a bad minute
		}
		return 0
	}
	cfg := scaledHold()
	cfg.Timeout = 2 * time.Second
	cfg.LedgerWarmup = 90 * time.Millisecond // no base before b's forward (the relay just started)
	h := gp2Harness(t, f, cfg, true)
	f.bump("a")
	h.pushTo(t, "a", "site")
	settleHeld(t, h, time.Second)
	time.Sleep(40 * time.Millisecond)
	f.bump("a")
	h.pushTo(t, "a", "site")
	waitFor(t, "a's hold to end on health", func() bool { return h.holds("completed") == "1" })
	time.Sleep(50 * time.Millisecond) // > LedgerQuiet; a's update is still in its tail
	f.bump("b")
	h.pushTo(t, "b", "site")
	waitFor(t, "b held", func() bool { return h.held() == "1" })
	f.bump("b")
	h.pushTo(t, "b", "site") // the quick fix waits behind b's hold
	time.Sleep(900 * time.Millisecond)
	settleHeld(t, h, 5*time.Second)
	f.bg.Wait()
	f.mu.Lock()
	defer f.mu.Unlock()
	t.Logf("log:\n%s", grepLog(h.log.String(), "hold ended", "updates counter"))
	if f.overlaps != 0 || f.stale != 0 {
		t.Errorf("REPRODUCED: %d overlapping updates, %d stale commits (b committed %d, head %d)",
			f.overlaps, f.stale, f.committed["b.realliance.page/site/"], f.head["b.realliance.page/site/"])
	}
}

// Concern 3: during a git-pages rollout two processes answer behind the
// Service. A counter read from the new pod ends holds whose updates still run
func grepLog(l string, subs ...string) string {
	var out []string
	for _, line := range strings.Split(l, "\n") {
		for _, s := range subs {
			if strings.Contains(line, s) {
				out = append(out, line)
				break
			}
		}
	}
	return strings.Join(out, "\n")
}
