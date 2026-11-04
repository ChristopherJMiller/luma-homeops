package main

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const forge = "https://git.realliance.net/"
const domain = "realliance.page"

func pub(owner, name string) Repo {
	return Repo{Name: name, OwnerLogin: owner, OwnerVisibility: "public", CloneURL: forge + owner + "/" + name + ".git"}
}

func pushEv(r Repo) Event {
	return Event{Name: "push", Ref: "refs/heads/pages", After: strings.Repeat("a", 40), Repository: r}
}

func TestRouteLowercases(t *testing.T) {
	tg, o := Route(pushEv(pub("Test-Ab12", "My-Site")), domain, forge)
	if o != Forwarded || tg != (Target{"test-ab12.realliance.page", "/my-site/"}) {
		t.Fatalf("got %v %v", tg, o)
	}
	tg, o = Route(pushEv(pub("Test-Ab12", "Pages")), domain, forge)
	if o != Forwarded || tg.Path != "/" {
		t.Fatalf("got %v %v", tg, o)
	}
}

func TestRouteRejectsNames(t *testing.T) {
	for _, owner := range []string{"a.b", "a_b", strings.Repeat("a", 64), "www", "WWW", "-x", "x-"} {
		if _, o := Route(pushEv(pub(owner, "r")), domain, forge); o != IgnoredName {
			t.Errorf("owner %q: %v", owner, o)
		}
	}
	if _, o := Route(pushEv(pub("o", ".profile")), domain, forge); o != IgnoredName {
		t.Errorf(".profile: %v", o)
	}
	r := pub("o", "r")
	r.CloneURL = "https://evil.example/a/b.git"
	if _, o := Route(pushEv(r), domain, forge); o != BadRequest {
		t.Errorf("evil clone url: %v", o)
	}
	if _, o := Route(pushEv(pub(strings.Repeat("a", 63), "r")), domain, forge); o != Forwarded {
		t.Errorf("63-char owner should pass")
	}
}

func TestRouteIgnores(t *testing.T) {
	ev := pushEv(pub("o", "r"))
	ev.Ref = "refs/heads/main"
	if _, o := Route(ev, domain, forge); o != IgnoredRef {
		t.Errorf("main: %v", o)
	}
	ev = pushEv(pub("o", "r"))
	ev.Repository.Private = true
	if _, o := Route(ev, domain, forge); o != IgnoredPrivate {
		t.Errorf("private: %v", o)
	}
	ev = pushEv(pub("o", "r"))
	ev.Name = "issues"
	if _, o := Route(ev, domain, forge); o != IgnoredEvent {
		t.Errorf("issues: %v", o)
	}
	ev = Event{Name: "repository", Action: "created", Repository: pub("o", "r")}
	if _, o := Route(ev, domain, forge); o != IgnoredEvent {
		t.Errorf("created: %v", o)
	}
	ev = Event{Name: "delete", RefType: "branch", Ref: "feature", Repository: pub("o", "r")}
	if _, o := Route(ev, domain, forge); o != IgnoredRef {
		t.Errorf("delete other branch: %v", o)
	}
	ev = Event{Name: "delete", RefType: "tag", Ref: "pages", Repository: pub("o", "r")}
	if _, o := Route(ev, domain, forge); o != IgnoredRef {
		t.Errorf("delete tag: %v", o)
	}
}

func TestRouteUnpublish(t *testing.T) {
	want := Target{"o.realliance.page", "/r/"}
	for name, ev := range map[string]Event{
		"delete":  {Name: "delete", RefType: "branch", Ref: "refs/heads/pages", Repository: pub("O", "R")},
		"zeros40": {Name: "push", Ref: "refs/heads/pages", After: strings.Repeat("0", 40), Repository: pub("O", "R")},
		"zeros64": {Name: "push", Ref: "refs/heads/pages", After: strings.Repeat("0", 64), Repository: pub("O", "R")},
		"repo":    {Name: "repository", Action: "deleted", Repository: pub("O", "R")},
	} {
		tg, o := Route(ev, domain, forge)
		if o != UnpublishRequested || tg != want {
			t.Errorf("%s: %v %v", name, tg, o)
		}
	}
}

// R9: a site published while public must still raise a takedown request after
// the repo went private and was then deleted.
func TestRouteUnpublishWhenPrivate(t *testing.T) {
	priv := pub("o", "r")
	priv.Private = true
	for name, ev := range map[string]Event{
		"delete branch": {Name: "delete", RefType: "branch", Ref: "refs/heads/pages", Repository: priv},
		"repo deleted":  {Name: "repository", Action: "deleted", Repository: priv},
	} {
		tg, o := Route(ev, domain, forge)
		if o != UnpublishRequested || tg.Host != "o.realliance.page" {
			t.Errorf("%s: %v %v", name, tg, o)
		}
	}
	for name, ev := range map[string]Event{
		"push":       pushEv(priv),
		"zero push":  {Name: "push", Ref: "refs/heads/pages", After: strings.Repeat("0", 40), Repository: priv},
		"delete tag": {Name: "delete", RefType: "branch", Ref: "main", Repository: priv},
		"repo made":  {Name: "repository", Action: "created", Repository: priv},
	} {
		if _, o := Route(ev, domain, forge); o != IgnoredPrivate {
			t.Errorf("%s: %v", name, o)
		}
	}
}

func sign(body, secret []byte) string {
	m := hmac.New(sha256.New, secret)
	m.Write(body)
	return hex.EncodeToString(m.Sum(nil))
}

func TestVerify(t *testing.T) {
	secret := []byte("s3cret")
	body := []byte(`{"a":1}`)
	good := sign(body, secret)
	if !Verify(body, secret, good) {
		t.Error("valid rejected")
	}
	flipped := []byte(good)
	if flipped[0] == 'a' {
		flipped[0] = 'b'
	} else {
		flipped[0] = 'a'
	}
	for name, sig := range map[string]string{
		"flipped": string(flipped), "empty": "", "non-hex": strings.Repeat("z", 64),
		"short": good[:10], "prefixed": "sha256=" + good, "long": good + "00",
	} {
		if Verify(body, secret, sig) {
			t.Errorf("%s accepted", name)
		}
	}
	if Verify(body, []byte("other"), good) {
		t.Error("wrong secret accepted")
	}
	if Verify(body, nil, sign(body, nil)) {
		t.Error("empty secret must never verify")
	}
}

type harness struct {
	relay *Relay
	hits  *atomic.Int32
	log   *bytes.Buffer
	up    *httptest.Server
}

func newHarness(t *testing.T, h http.HandlerFunc) *harness {
	t.Helper()
	hits := new(atomic.Int32)
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		if h != nil {
			h(w, r)
		}
	}))
	t.Cleanup(up.Close)
	u, _ := url.Parse(up.URL)
	buf := new(bytes.Buffer)
	return &harness{
		relay: &Relay{Secret: []byte("s3cret"), Upstream: u, Domain: domain, Forge: forge,
			Client: up.Client(), Counters: NewCounters(), Log: slog.New(slog.NewJSONHandler(buf, nil))},
		hits: hits, log: buf, up: up,
	}
}

func (h *harness) post(t *testing.T, body []byte, event, sig string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/hook", bytes.NewReader(body))
	req.Header.Set("X-Forgejo-Event", event)
	if sig != "" {
		req.Header.Set("X-Forgejo-Signature", sig)
	}
	w := httptest.NewRecorder()
	h.relay.ServeHTTP(w, req)
	return w
}

func payload(t *testing.T, ev Event) []byte {
	t.Helper()
	b, err := json.Marshal(map[string]any{
		"ref": ev.Ref, "after": ev.After, "ref_type": ev.RefType, "action": ev.Action,
		"repository": map[string]any{"name": ev.Repository.Name, "private": ev.Repository.Private, "internal": ev.Repository.Internal,
			"clone_url": ev.Repository.CloneURL, "owner": map[string]any{"login": ev.Repository.OwnerLogin, "visibility": ev.Repository.OwnerVisibility}},
	})
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestServeForwards(t *testing.T) {
	var gotHost, gotPath, gotCT, gotEv string
	var gotBody []byte
	h := newHarness(t, func(w http.ResponseWriter, r *http.Request) {
		gotHost, gotPath, gotCT, gotEv = r.Host, r.URL.Path, r.Header.Get("Content-Type"), r.Header.Get("X-Forgejo-Event")
		gotBody, _ = io.ReadAll(r.Body)
		w.WriteHeader(202)
		io.WriteString(w, "updating")
	})
	body := payload(t, pushEv(pub("Org", "Site")))
	w := h.post(t, body, "push", sign(body, h.relay.Secret))
	if w.Code != 202 || w.Body.String() != "updating" {
		t.Fatalf("%d %q", w.Code, w.Body.String())
	}
	if gotHost != "org.realliance.page" || gotPath != "/site/" || gotCT != "application/json" || gotEv != "push" || !bytes.Equal(gotBody, body) {
		t.Fatalf("upstream saw %q %q %q %q", gotHost, gotPath, gotCT, gotEv)
	}
	var m bytes.Buffer
	h.relay.Counters.WriteTo(&m)
	if !strings.Contains(m.String(), `pages_relay_events_total{outcome="forwarded"} 1`) {
		t.Error(m.String())
	}
}

func TestServeUpstreamErrors(t *testing.T) {
	h := newHarness(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(503) })
	body := payload(t, pushEv(pub("o", "r")))
	w := h.post(t, body, "push", sign(body, h.relay.Secret))
	if w.Code != 503 {
		t.Errorf("status %d", w.Code)
	}
	var m bytes.Buffer
	h.relay.Counters.WriteTo(&m)
	if !strings.Contains(m.String(), `outcome="upstream_error"} 1`) {
		t.Error(m.String())
	}
	h.up.Close()
	w = h.post(t, body, "push", sign(body, h.relay.Secret))
	if w.Code != 502 {
		t.Errorf("transport error status %d", w.Code)
	}
}

func TestServeNoRedirects(t *testing.T) {
	h := newHarness(t, func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/elsewhere", 302)
	})
	h.relay.Client = NewClient()
	body := payload(t, pushEv(pub("o", "r")))
	w := h.post(t, body, "push", sign(body, h.relay.Secret))
	if h.hits.Load() != 1 {
		t.Errorf("hits %d", h.hits.Load())
	}
	if w.Code < 300 || w.Code >= 400 {
		// 302 passes through (non-2xx) and is counted as an upstream error.
		t.Errorf("status %d", w.Code)
	}
}

func TestServeUnpublishNotForwarded(t *testing.T) {
	h := newHarness(t, nil)
	body := payload(t, Event{Name: "repository", Action: "deleted", Repository: pub("o", "r")})
	w := h.post(t, body, "repository", sign(body, h.relay.Secret))
	if w.Code != 200 || h.hits.Load() != 0 {
		t.Fatalf("%d hits %d", w.Code, h.hits.Load())
	}
	var m bytes.Buffer
	h.relay.Counters.WriteTo(&m)
	if !strings.Contains(m.String(), `outcome="unpublish_requested"} 1`) {
		t.Error(m.String())
	}
}

func TestServeBadSignature(t *testing.T) {
	h := newHarness(t, nil)
	body := payload(t, pushEv(pub("o", "r")))
	for _, sig := range []string{"", "zz", strings.Repeat("0", 64), "sha256=" + sign(body, h.relay.Secret)} {
		if w := h.post(t, body, "push", sig); w.Code != 401 {
			t.Errorf("sig %q: %d", sig, w.Code)
		}
	}
	if h.hits.Load() != 0 {
		t.Error("upstream called")
	}
	var m bytes.Buffer
	h.relay.Counters.WriteTo(&m)
	if !strings.Contains(m.String(), `outcome="bad_signature"} 4`) {
		t.Error(m.String())
	}
}

func TestServeBodyCap(t *testing.T) {
	h := newHarness(t, nil)
	body := bytes.Repeat([]byte("x"), MaxBody+1)
	w := h.post(t, body, "push", sign(body, h.relay.Secret))
	if w.Code != 413 || h.hits.Load() != 0 {
		t.Fatalf("%d hits %d", w.Code, h.hits.Load())
	}
	var m bytes.Buffer
	h.relay.Counters.WriteTo(&m)
	if !strings.Contains(m.String(), `outcome="too_large"} 1`) {
		t.Error(m.String())
	}
	if MaxBody != 16<<20 {
		t.Error("cap must be 16 MiB")
	}
}

func TestServeMethodAndPath(t *testing.T) {
	h := newHarness(t, nil)
	for _, tc := range []struct {
		method, path string
		want         int
	}{
		{"GET", "/hook", 405}, {"PUT", "/hook", 405}, {"DELETE", "/hook", 405},
		{"POST", "/", 404}, {"POST", "/hook/x", 404}, {"POST", "/other", 404},
	} {
		w := httptest.NewRecorder()
		h.relay.ServeHTTP(w, httptest.NewRequest(tc.method, tc.path, nil))
		if w.Code != tc.want {
			t.Errorf("%s %s: %d", tc.method, tc.path, w.Code)
		}
	}
}

func TestServeMalformedJSON(t *testing.T) {
	h := newHarness(t, nil)
	body := []byte("{not json")
	if w := h.post(t, body, "push", sign(body, h.relay.Secret)); w.Code != 400 {
		t.Errorf("%d", w.Code)
	}
}

func TestPrivateLogsNoNames(t *testing.T) {
	h := newHarness(t, nil)
	priv := pub("secret-org", "hidden")
	priv.Private = true
	for _, ev := range []Event{
		pushEv(priv),
		{Name: "repository", Action: "deleted", Repository: priv},
		{Name: "delete", RefType: "branch", Ref: "refs/heads/pages", Repository: priv},
	} {
		body := payload(t, ev)
		h.post(t, body, ev.Name, sign(body, h.relay.Secret))
	}
	l := h.log.String()
	if l == "" || strings.Contains(l, "secret-org") || strings.Contains(l, "hidden") || strings.Contains(l, "s3cret") {
		t.Fatalf("log: %q", l)
	}
}

func TestPublicLogsHostPath(t *testing.T) {
	h := newHarness(t, nil)
	body := payload(t, pushEv(pub("org", "site")))
	h.post(t, body, "push", sign(body, h.relay.Secret))
	if l := h.log.String(); !strings.Contains(l, "org.realliance.page") || !strings.Contains(l, "/site/") || strings.Contains(l, "s3cret") {
		t.Fatalf("log: %q", l)
	}
}

func TestMetricsAllOutcomesStartAtZero(t *testing.T) {
	var b bytes.Buffer
	NewCounters().WriteTo(&b)
	lines := strings.Split(strings.TrimSpace(b.String()), "\n")
	if len(lines) != 11 {
		t.Fatalf("%d lines: %q", len(lines), b.String())
	}
	for _, l := range lines {
		if !strings.HasPrefix(l, `pages_relay_events_total{outcome="`) || !strings.HasSuffix(l, " 0") {
			t.Errorf("bad line %q", l)
		}
	}
}

func TestMainRefusesWithoutSecret(t *testing.T) {
	if os.Getenv("PAGES_RELAY_CHILD") == "1" {
		main()
		return
	}
	dir := t.TempDir()
	empty := dir + "/empty"
	os.WriteFile(empty, nil, 0o600)
	for _, f := range []string{dir + "/missing", empty} {
		cmd := exec.Command(os.Args[0], "-test.run=TestMainRefusesWithoutSecret")
		cmd.Env = append(os.Environ(), "PAGES_RELAY_CHILD=1", "PAGES_RELAY_SECRET_FILE="+f, "PAGES_RELAY_UPSTREAM=http://127.0.0.1:1")
		out, err := cmd.CombinedOutput()
		if err == nil || !strings.Contains(string(out), "PAGES_RELAY_SECRET_FILE") {
			t.Errorf("%s: err=%v out=%q", f, err, out)
		}
	}
}

// Fixtures below follow Forgejo v15.0 (codeberg.org/forgejo/forgejo, branch
// v15.0/forgejo):
//   - push: modules/structs/hook.go:258 PushPayload {ref, before, after,
//     commits[], head_commit, repository, pusher, sender}; X-Forgejo-Event: push.
//   - branch delete: services/webhook/notifier.go:803-809 DeleteRef sends
//     DeletePayload (modules/structs/hook.go:161: ref, ref_type, pusher_type,
//     repository, sender) with Ref = refFullName.String() = "refs/heads/<b>"
//     and RefType "branch"; X-Forgejo-Event: delete. No push event is sent.
//   - repository delete: notifier.go:121-124 DeleteRepository sends
//     RepositoryPayload{action:"deleted"}; X-Forgejo-Event: repository.
//   - repository object: services/convert/repository.go:257
//     Internal = !IsPrivate && Owner.Visibility == private; owner JSON is
//     modules/structs/user.go:55 (login, visibility).
func repoJSON(owner, vis, name string, private, internal bool) string {
	return fmt.Sprintf(`{"name":%q,"private":%t,"internal":%t,"clone_url":"https://git.realliance.net/%s/%s.git",`+
		`"owner":{"login":%q,"visibility":%q}}`, name, private, internal, owner, name, owner, vis)
}

func pushBody(repo string, files int) []byte {
	var added strings.Builder
	for i := 0; i < files; i++ {
		if i > 0 {
			added.WriteString(",")
		}
		fmt.Fprintf(&added, `"docs/generated/api/module_%06d/index.html"`, i)
	}
	c := fmt.Sprintf(`{"id":"%s","message":"deploy","added":[%s],"removed":[],"modified":[]}`, strings.Repeat("b", 40), added.String())
	return []byte(fmt.Sprintf(`{"ref":"refs/heads/pages","before":"%s","after":"%s","commits":[%s],"total_commits":1,"head_commit":%s,"repository":%s,"pusher":{},"sender":{}}`,
		strings.Repeat("c", 40), strings.Repeat("b", 40), c, c, repo))
}

func deleteBranchBody(repo string) []byte {
	return []byte(`{"ref":"refs/heads/pages","ref_type":"branch","pusher_type":"user","repository":` + repo + `,"sender":{}}`)
}

func repoDeletedBody(repo string) []byte {
	return []byte(`{"action":"deleted","repository":` + repo + `,"organization":null,"sender":{}}`)
}

func (h *harness) metrics() string {
	var m bytes.Buffer
	h.relay.Counters.WriteTo(&m)
	return m.String()
}

// C1: only private=false, internal=false and owner.visibility=public publish.
func TestNotFullyPublicNeverForwardedNorLogged(t *testing.T) {
	cases := map[string]string{
		"internal":      repoJSON("hidden-org", "private", "secret-repo", false, true),
		"owner private": repoJSON("hidden-org", "private", "secret-repo", false, false),
		"owner limited": repoJSON("hidden-org", "limited", "secret-repo", false, false),
		"no visibility": repoJSON("hidden-org", "", "secret-repo", false, false),
		"private":       repoJSON("hidden-org", "public", "secret-repo", true, false),
	}
	for name, repo := range cases {
		h := newHarness(t, nil)
		body := pushBody(repo, 3)
		w := h.post(t, body, "push", sign(body, h.relay.Secret))
		if w.Code != 200 || h.hits.Load() != 0 || !strings.Contains(h.metrics(), `outcome="ignored_private"} 1`) {
			t.Errorf("%s: %d hits %d", name, w.Code, h.hits.Load())
		}
		for _, b := range [][]byte{deleteBranchBody(repo), repoDeletedBody(repo)} {
			ev := "delete"
			if bytes.Contains(b, []byte(`"action"`)) {
				ev = "repository"
			}
			h.post(t, b, ev, sign(b, h.relay.Secret))
		}
		if !strings.Contains(h.metrics(), `outcome="unpublish_requested"} 2`) || h.hits.Load() != 0 {
			t.Errorf("%s: unpublish not counted: %s", name, h.metrics())
		}
		if l := h.log.String(); l == "" || strings.Contains(l, "hidden-org") || strings.Contains(l, "secret-repo") {
			t.Errorf("%s: log leaks names: %q", name, l)
		}
	}
}

// I1: real Forgejo 15 shapes.
func TestForgejoShapes(t *testing.T) {
	repo := repoJSON("Org", "public", "Site", false, false)
	h := newHarness(t, nil)
	b := pushBody(repo, 3)
	if w := h.post(t, b, "push", sign(b, h.relay.Secret)); w.Code != 200 || h.hits.Load() != 1 {
		t.Fatalf("push: %d hits %d", w.Code, h.hits.Load())
	}
	b = deleteBranchBody(repo)
	h.post(t, b, "delete", sign(b, h.relay.Secret))
	b = repoDeletedBody(repo)
	h.post(t, b, "repository", sign(b, h.relay.Secret))
	if !strings.Contains(h.metrics(), `outcome="unpublish_requested"} 2`) || h.hits.Load() != 1 {
		t.Errorf("metrics %s hits %d", h.metrics(), h.hits.Load())
	}
	if l := h.log.String(); !strings.Contains(l, "org.realliance.page") {
		t.Errorf("public unpublish should log host: %q", l)
	}
	// Short ref (Gitea-era) still honoured; a non-pages branch is not.
	if _, o := Route(Event{Name: "delete", RefType: "branch", Ref: "pages", Repository: pub("o", "r")}, domain, forge); o != UnpublishRequested {
		t.Errorf("short ref: %v", o)
	}
	if _, o := Route(Event{Name: "delete", RefType: "branch", Ref: "refs/heads/main", Repository: pub("o", "r")}, domain, forge); o != IgnoredRef {
		t.Errorf("main: %v", o)
	}
}

// I2: a ~1.03 MiB real-shape push (11,500 files) is forwarded.
func TestLargeRealPushForwarded(t *testing.T) {
	h := newHarness(t, nil)
	b := pushBody(repoJSON("org", "public", "site", false, false), 11500)
	if len(b) <= 1<<20 {
		t.Fatalf("fixture too small: %d", len(b))
	}
	if w := h.post(t, b, "push", sign(b, h.relay.Secret)); w.Code != 200 || h.hits.Load() != 1 {
		t.Fatalf("%d hits %d", w.Code, h.hits.Load())
	}
}

func TestBusyWhenFiveInFlight(t *testing.T) {
	release := make(chan struct{})
	h := newHarness(t, func(w http.ResponseWriter, r *http.Request) { <-release })
	b := pushBody(repoJSON("org", "public", "site", false, false), 1)
	sig := sign(b, h.relay.Secret)
	done := make(chan int, 4)
	for i := 0; i < 4; i++ {
		go func() { done <- h.post(t, b, "push", sig).Code }()
	}
	for h.hits.Load() < 4 {
		time.Sleep(5 * time.Millisecond)
	}
	if w := h.post(t, b, "push", sig); w.Code != 503 || !strings.Contains(h.metrics(), `outcome="busy"} 1`) {
		t.Errorf("5th: %d %s", w.Code, h.metrics())
	}
	close(release)
	for i := 0; i < 4; i++ {
		<-done
	}
}

// Minors M1-M3.
func TestRouteHardening(t *testing.T) {
	r := pub("o", "r")
	r.CloneURL = forge + "other/r.git"
	if _, o := Route(pushEv(r), domain, forge); o != BadRequest {
		t.Errorf("clone/owner mismatch: %v", o)
	}
	for _, after := range []string{"", strings.Repeat("a", 39), strings.Repeat("g", 40)} {
		ev := pushEv(pub("o", "r"))
		ev.After = after
		if _, o := Route(ev, domain, forge); o != BadRequest {
			t.Errorf("after %q: %v", after, o)
		}
	}
	if _, o := Route(pushEv(pub("Kate", "r")), domain, forge); o != IgnoredName {
		t.Errorf("kelvin sign: %v", o)
	}
	for _, name := range []string{"", "a/b", "x@1", "a b"} {
		k := pub("o", name)
		k.CloneURL = forge + "o/" + name + ".git"
		if _, o := Route(pushEv(k), domain, forge); o != IgnoredName {
			t.Errorf("repo %q: %v", name, o)
		}
	}
}
