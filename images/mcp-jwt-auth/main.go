// mcp-jwt-auth is a Traefik forward-auth endpoint that answers one question:
// does this request carry a Dex-issued JWT belonging to somebody on the family
// allowlist?
//
// It exists because the MCP gateway authenticates but does not authorize — its
// only check is that a bearer token is signed by Dex — and Dex authenticates any
// Google or Microsoft account in the world. Without this, publishing the MCP
// endpoint published the family tree.
//
// Why not oauth2-proxy, which already does bearer-JWT validation plus an email
// allowlist: it insists the token's audience match a configured value, and MCP
// clients register dynamically, so every client gets a fresh random client id and
// the audience cannot be known in advance (oauth2-proxy#1032, still open).
//
// Audience is therefore deliberately NOT checked. The issuer is ours, the
// signature is verified against its JWKS, and the gate is the email claim — so
// "some other client of our own Dex" is not a meaningful attacker here, whereas
// "any Google account" was.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
)

type allowlist struct {
	mu      sync.RWMutex
	path    string
	entries map[string]struct{}
	loaded  time.Time
}

// load reads the allowlist file. An unreadable or empty file yields an empty set,
// which denies everyone: this fails closed, exactly like the oauth2-proxy tiers.
func (a *allowlist) load() error {
	data, err := os.ReadFile(a.path)
	if err != nil {
		return err
	}
	entries := map[string]struct{}{}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.ToLower(strings.TrimSpace(line))
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		entries[line] = struct{}{}
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.entries = entries
	a.loaded = time.Now()
	return nil
}

func (a *allowlist) permits(email string) bool {
	a.mu.RLock()
	defer a.mu.RUnlock()
	_, ok := a.entries[strings.ToLower(strings.TrimSpace(email))]
	return ok
}

func (a *allowlist) size() int {
	a.mu.RLock()
	defer a.mu.RUnlock()
	return len(a.entries)
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	log := slog.New(slog.NewTextHandler(os.Stdout, nil))

	issuer := os.Getenv("OIDC_ISSUER")
	if issuer == "" {
		log.Error("OIDC_ISSUER is required")
		os.Exit(1)
	}
	list := &allowlist{path: env("EMAILS_FILE", "/etc/mcp-jwt-auth/emails")}
	if err := list.load(); err != nil {
		// Do not exit: starting with an empty list denies everyone, which is the
		// safe direction, and the reload below can recover once the mount appears.
		log.Error("could not load allowlist; denying all until it loads", "err", err)
	}
	log.Info("allowlist loaded", "entries", list.size(), "path", list.path)

	ctx := context.Background()
	provider, err := oidc.NewProvider(ctx, strings.TrimSuffix(issuer, "/"))
	if err != nil {
		log.Error("OIDC discovery failed", "issuer", issuer, "err", err)
		os.Exit(1)
	}
	// SkipClientIDCheck is the deliberate part: see the package comment. Signature,
	// issuer and expiry are all still verified.
	verifier := provider.Verifier(&oidc.Config{SkipClientIDCheck: true})

	// Secret mounts update in place, so re-read periodically. Adding somebody to
	// the family list should not need a restart.
	go func() {
		for range time.Tick(30 * time.Second) {
			if err := list.load(); err != nil {
				log.Error("allowlist reload failed", "err", err)
			}
		}
	}()

	mux := http.NewServeMux()

	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	mux.HandleFunc("/auth", func(w http.ResponseWriter, r *http.Request) {
		email, err := authorize(r.Context(), verifier, list, r.Header.Get("Authorization"))
		if err != nil {
			// A flat 401 on purpose: the caller is an MCP client, not a browser, so
			// a redirect would be useless. The gateway's own WWW-Authenticate tells
			// the client where to get a token.
			log.Info("denied", "path", r.Header.Get("X-Forwarded-Uri"), "reason", err)
			w.Header().Set("WWW-Authenticate", `Bearer error="invalid_token"`)
			w.WriteHeader(http.StatusUnauthorized)
			_ = json.NewEncoder(w).Encode(map[string]string{"error": "unauthorized"})
			return
		}
		w.Header().Set("X-Auth-Request-Email", email)
		w.WriteHeader(http.StatusAccepted)
	})

	addr := env("ADDR", ":4181")
	log.Info("listening", "addr", addr, "issuer", issuer)
	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}
	if err := srv.ListenAndServe(); err != nil {
		log.Error("server stopped", "err", err)
		os.Exit(1)
	}
}

func authorize(ctx context.Context, v *oidc.IDTokenVerifier, list *allowlist, header string) (string, error) {
	raw := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(header), "Bearer"))
	if raw == "" {
		return "", errors.New("no bearer token")
	}
	token, err := v.Verify(ctx, raw)
	if err != nil {
		return "", fmt.Errorf("token verification failed: %w", err)
	}
	var claims struct {
		Email    string `json:"email"`
		Verified *bool  `json:"email_verified"`
	}
	if err := token.Claims(&claims); err != nil {
		return "", fmt.Errorf("could not read claims: %w", err)
	}
	if claims.Email == "" {
		return "", errors.New("token has no email claim")
	}
	// Dex passes the upstream provider's verification through. Treat an explicit
	// false as disqualifying; absent means the connector did not say, which both
	// Google and Microsoft do not do.
	if claims.Verified != nil && !*claims.Verified {
		return "", errors.New("email not verified")
	}
	if !list.permits(claims.Email) {
		return "", errors.New("email not on the allowlist")
	}
	return claims.Email, nil
}
