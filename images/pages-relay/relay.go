// Command pages-relay sits between Forgejo's system webhook and git-pages.
//
// git-pages picks the site from the request's Host and path (GetSite in
// src/pages.go) and takes only `ref` and `repository.clone_url` from the body,
// while one Forgejo system webhook has a single fixed URL. The relay verifies
// Forgejo's signature (git-pages verifies none), maps owner/repo to a site and
// forwards the push in-cluster with the right Host.
package main

import (
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

// MaxInFlight bounds concurrent requests (and so buffered bodies); the rest
// get 503 and are counted busy.
const MaxInFlight = 4

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

type Outcome string

const (
	Forwarded          Outcome = "forwarded"
	IgnoredEvent       Outcome = "ignored_event"
	IgnoredRef         Outcome = "ignored_ref"
	IgnoredPrivate     Outcome = "ignored_private"
	IgnoredName        Outcome = "ignored_name"
	UnpublishRequested Outcome = "unpublish_requested"
	BadSignature       Outcome = "bad_signature"
	BadRequest         Outcome = "bad_request"
	UpstreamError      Outcome = "upstream_error"
	TooLarge           Outcome = "too_large"
	Busy               Outcome = "busy"
)

var allOutcomes = []Outcome{Forwarded, IgnoredEvent, IgnoredRef, IgnoredPrivate, IgnoredName,
	UnpublishRequested, BadSignature, BadRequest, UpstreamError, TooLarge, Busy}

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
	if len(ev.Repository.CloneURL) < len(forge) || !strings.EqualFold(ev.Repository.CloneURL[:len(forge)], forge) {
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
	mu sync.Mutex
	n  map[Outcome]uint64
}

func NewCounters() *Counters { return &Counters{n: map[Outcome]uint64{}} }

func (c *Counters) Inc(o Outcome) {
	c.mu.Lock()
	c.n[o]++
	c.mu.Unlock()
}

func (c *Counters) WriteTo(w io.Writer) (int64, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	var total int64
	for _, o := range allOutcomes {
		n, err := fmt.Fprintf(w, "pages_relay_events_total{outcome=%q} %d\n", string(o), c.n[o])
		total += int64(n)
		if err != nil {
			return total, err
		}
	}
	return total, nil
}

// NewClient returns the upstream client: bounded timeout, no redirects.
func NewClient() *http.Client {
	return &http.Client{
		Timeout:       30 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

type Relay struct {
	Secret   []byte
	Upstream *url.URL
	Domain   string
	Forge    string
	Client   *http.Client
	Counters *Counters
	Log      *slog.Logger

	once sync.Once
	sem  chan struct{}
}

func (rl *Relay) done(w http.ResponseWriter, code int, o Outcome, msg string, attrs ...any) {
	rl.Counters.Inc(o)
	rl.Log.Info("event", append([]any{"outcome", string(o)}, attrs...)...)
	w.WriteHeader(code)
	io.WriteString(w, msg)
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
	rl.once.Do(func() { rl.sem = make(chan struct{}, MaxInFlight) })
	select {
	case rl.sem <- struct{}{}:
		defer func() { <-rl.sem }()
	default:
		rl.done(w, http.StatusServiceUnavailable, Busy, "busy\n")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, MaxBody))
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
	target, outcome := Route(ev, rl.Domain, rl.Forge)
	// Names are logged only for public repos (hidden orgs' repos are private).
	var attrs []any
	if ev.Repository.Publishable() && target.Host != "" {
		attrs = []any{"host", target.Host, "path", target.Path}
	}
	switch outcome {
	case Forwarded:
		rl.forward(w, r, target, evName, body, attrs)
	case UnpublishRequested:
		rl.done(w, http.StatusOK, outcome, "unpublish_requested: an Owner must take the site down\n", attrs...)
	case BadRequest:
		rl.done(w, http.StatusBadRequest, outcome, "bad_request\n", attrs...)
	default:
		rl.done(w, http.StatusOK, outcome, "ignored: "+string(outcome)+"\n", attrs...)
	}
}

func (rl *Relay) forward(w http.ResponseWriter, r *http.Request, t Target, evName string, body []byte, attrs []any) {
	u := *rl.Upstream
	u.Path = strings.TrimRight(rl.Upstream.Path, "/") + t.Path
	req, err := http.NewRequestWithContext(context.WithoutCancel(r.Context()), http.MethodPost, u.String(), strings.NewReader(string(body)))
	if err != nil {
		rl.done(w, http.StatusBadGateway, UpstreamError, "upstream_error\n", attrs...)
		return
	}
	req.Host = t.Host
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forgejo-Event", evName)
	resp, err := rl.Client.Do(req)
	if err != nil {
		rl.done(w, http.StatusBadGateway, UpstreamError, "upstream_error\n", attrs...)
		return
	}
	defer resp.Body.Close()
	snippet, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	o := Forwarded
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		o = UpstreamError
	}
	rl.done(w, resp.StatusCode, o, string(snippet), append(attrs, "status", resp.StatusCode)...)
}
