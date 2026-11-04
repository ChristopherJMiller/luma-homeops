package main

// Tests added in fix round 1 of relay fix 2: the aborted hold (M1), the body
// deadline (M2), the ledger's quiet rule and Last-Modified at 1 s resolution (M3).

import (
	"bufio"
	"fmt"
	"net"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// M1: a restart kills the update and nothing stores the push. With no newer
// push waiting, the relay forwards it once more, and counts the first hold
// aborted, not completed. A second restart under the retry is not retried
// again: it is counted and logged with the site, for redelivery.
func TestHoldAbortedRequeuesOnce(t *testing.T) {
	fp := newFakePages(20 * time.Millisecond)
	never := make(chan struct{})
	fp.plan = func(site string, n int) (time.Duration, bool, <-chan struct{}) {
		if n <= 2 {
			return 0, false, never
		}
		return 0, false, nil
	}
	cfg := fastHold()
	cfg.Timeout = time.Minute
	h := pagesHarness(t, fp, cfg, true)
	t.Cleanup(func() { close(never) })
	fp.bump("a")
	h.pushTo(t, "a", "site")
	waitFor(t, "a held", func() bool { return h.held() == "1" })
	time.Sleep(1100 * time.Millisecond) // process_start_time_seconds has 1 s resolution
	fp.restart()
	waitFor(t, "the push to be forwarded again and held", func() bool {
		fp.mu.Lock()
		defer fp.mu.Unlock()
		return fp.updates["a.realliance.page/site/"] == 2 && h.held() == "1"
	})
	if h.holds("aborted") != "1" || h.holds("aborted_lost") != "0" || h.holds("completed") != "0" {
		t.Errorf("after the first restart:\n%s", h.metrics())
	}
	time.Sleep(1100 * time.Millisecond)
	fp.restart()
	waitFor(t, "the second hold to abort", func() bool { return h.holds("aborted_lost") == "1" })
	time.Sleep(100 * time.Millisecond)
	fp.mu.Lock()
	n := fp.updates["a.realliance.page/site/"]
	fp.mu.Unlock()
	if n != 2 || h.held() != "0" {
		t.Errorf("the retry was retried: %d updates, held %s", n, h.held())
	}
	l := h.log.String()
	for _, want := range []string{`"level":"WARN"`, `"outcome":"aborted"`, `"outcome":"aborted_lost"`, `"site":"a.realliance.page/site"`, "re-queued", "already retried"} {
		if !strings.Contains(l, want) {
			t.Errorf("log lacks %q:\n%s", want, grepLog(l, "hold ended"))
		}
	}
}

// M1 at the queue: an abort re-queues unless a newer push waits or the job was
// already retried; the re-queued job carries its clone URL.
func TestAbortRequeueRules(t *testing.T) {
	q := newQueue(64, NewCounters())
	cfg := HoldConfig{Timeout: time.Minute, HealthFirst: time.Second}
	now := time.Now()
	mk := func(owner string, retried bool) job {
		return job{Target: Target{Host: owner + ".realliance.page", Path: "site"}, CloneURL: "https://forge/" + owner + "/site.git", retried: retried}
	}
	for _, j := range []job{mk("a", false), mk("b", false), mk("c", true)} {
		q.launch()
		q.inFlight[j.Target.Site()] = true
		q.finish(j, ansRunning, siteHealth{}, now, now, cfg)
	}
	q.put(mk("b", false)) // a newer push of b waits
	q.mu.Lock()
	defer q.mu.Unlock()
	for _, site := range []string{"a.realliance.page/site", "b.realliance.page/site", "c.realliance.page/site"} {
		for s, h := range q.held {
			if s == site {
				q.abort(s, h, now)
			}
		}
	}
	if q.c.holds[HeldAborted] != 2 || q.c.holds[HeldLost] != 1 {
		t.Errorf("outcomes: aborted %d (want a and b), aborted_lost %d (want c)", q.c.holds[HeldAborted], q.c.holds[HeldLost])
	}
	if len(q.held) != 0 {
		t.Fatalf("holds left: %d", len(q.held))
	}
	if j, ok := q.pending["a.realliance.page/site"]; !ok || !j.retried || j.CloneURL != "https://forge/a/site.git" {
		t.Errorf("a not re-queued once: %+v", j)
	}
	if j := q.pending["b.realliance.page/site"]; j.retried {
		t.Errorf("b's newer push was replaced: %+v", j)
	}
	if _, ok := q.pending["c.realliance.page/site"]; ok {
		t.Errorf("c, already retried, was re-queued again")
	}
}

// M2: a body that trickles holds a slot for DefaultBodyTimeout at most, which
// is under Forgejo's 5 s delivery timeout; the sender gets 400 then.
func TestBodyDeadlineUnderForgejoTimeout(t *testing.T) {
	if DefaultBodyTimeout >= 5*time.Second {
		t.Fatalf("DefaultBodyTimeout %v is not under Forgejo's 5 s delivery timeout", DefaultBodyTimeout)
	}
	t.Parallel()
	h := newHarness(t, nil)
	h.relay.start()
	srv := httptest.NewServer(h.relay)
	defer srv.Close()
	c, err := net.Dial("tcp", strings.TrimPrefix(srv.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	start := time.Now()
	fmt.Fprintf(c, "POST /hook HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\nX-Forgejo-Event: push\r\n\r\nabc")
	c.SetReadDeadline(time.Now().Add(DefaultBodyTimeout + 3*time.Second))
	line, err := bufio.NewReader(c).ReadString('\n')
	d := time.Since(start)
	if err != nil && line == "" {
		t.Logf("connection closed without an answer after %v: %v", d, err) // the server may just close
	}
	if d < DefaultBodyTimeout-500*time.Millisecond || d > DefaultBodyTimeout+time.Second {
		t.Errorf("the trickling body ended after %v, want about %v (%q)", d, DefaultBodyTimeout, line)
	}
	if len(h.relay.sem) != 0 {
		t.Errorf("slot still held")
	}
}

// M3, the ledger's quiet rule: a hold that ends on health leaves the update
// running to its deadline, so no ledger base is sound before then plus
// LedgerQuiet; one that ends on the counter is quiet after LedgerQuiet.
func TestBaseableWaitsForDeadlineAfterHealthEnd(t *testing.T) {
	cfg := HoldConfig{Timeout: 5 * time.Minute, LedgerQuiet: 30 * time.Second, LedgerWarmup: time.Nanosecond, HealthFirst: time.Second}
	q := newQueue(64, NewCounters())
	t0 := time.Now().Add(-time.Hour)
	q.led.startedAt = t0
	j := job{Target: Target{Host: "a.realliance.page", Path: "site"}}
	q.launch()
	q.inFlight[j.Target.Site()] = true
	q.finish(j, ansRunning, siteHealth{status: 404, known: true}, t0, t0, cfg)
	q.mu.Lock()
	defer q.mu.Unlock()
	q.healthSeen(j.Target.Site(), siteHealth{status: 200, ok: true, known: true, lm: "x", etag: "1"}, t0.Add(10*time.Second), time.Second)
	q.inFlight["b.realliance.page/site"] = true
	for _, tc := range []struct {
		at time.Duration
		ok bool
	}{{20 * time.Second, false}, {45 * time.Second, false}, {5*time.Minute + 10*time.Second, false}, {5*time.Minute + 31*time.Second, true}} {
		if _, ok := q.baseable(t0.Add(tc.at), cfg); ok != tc.ok {
			t.Errorf("baseable %v after the 202: %v, want %v", tc.at, ok, tc.ok)
		}
	}
}

// M3, Last-Modified at 1 s resolution: an unchanged re-publish keeps its ETag
// and only Last-Modified moves, and it does not within the first second. The 202
// comes 3 s after the forward, so the commit that ends the update is at least
// that far after the baseline and the hold ends on health alone.
func TestHoldUnchangedRepublishEndsOnLastModified(t *testing.T) {
	f := newGP2(3*time.Millisecond, time.Millisecond, 2*time.Second)
	f.duration = func(string) time.Duration { return 8 * time.Millisecond }
	cfg := scaledHold()
	h := gp2Harness(t, f, cfg, false) // no counter: the health alone
	f.bump("a")
	h.pushTo(t, "a", "site")
	waitFor(t, "first publish to end its hold", func() bool { return h.holds("completed") == "1" })
	// the head is unchanged: the ETag stays 1, the commit only moves Last-Modified
	h.pushTo(t, "a", "site")
	waitFor(t, "the unchanged re-publish to end its hold", func() bool { return h.holds("completed") == "2" })
	if h.holds("timeout") != "0" {
		t.Errorf("a hold timed out:\n%s", h.metrics())
	}
}
