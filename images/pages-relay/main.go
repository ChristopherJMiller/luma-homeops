package main

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func fatal(msg string) {
	fmt.Fprintln(os.Stderr, msg)
	os.Exit(1)
}

func main() {
	secretFile := env("PAGES_RELAY_SECRET_FILE", "/etc/pages-relay/webhook-secret")
	secret, err := os.ReadFile(secretFile)
	secret = bytes.TrimSpace(secret)
	if err != nil || len(secret) == 0 {
		fatal("PAGES_RELAY_SECRET_FILE (" + secretFile + ") is missing or empty")
	}
	up := os.Getenv("PAGES_RELAY_UPSTREAM")
	uu, err := url.Parse(up)
	if err != nil || up == "" || uu.Host == "" {
		fatal("PAGES_RELAY_UPSTREAM must be an absolute URL")
	}
	forge := env("PAGES_RELAY_FORGE", "https://git.realliance.net/")
	if !strings.HasSuffix(forge, "/") {
		forge += "/"
	}
	counters := NewCounters()
	rl := &Relay{Secret: secret, Upstream: uu, Domain: env("PAGES_RELAY_DOMAIN", "realliance.page"),
		Forge: forge, Client: NewClient(), Counters: counters,
		Log: slog.New(slog.NewJSONHandler(os.Stdout, nil))}
	// git-pages' /metrics, for its webhook update counter (hold.go). Unset, a
	// hold after a 202 ends by its site's health or at HoldTimeout only.
	if m := os.Getenv("PAGES_RELAY_UPSTREAM_METRICS"); m != "" {
		mu, err := url.Parse(m)
		if err != nil || mu.Host == "" {
			fatal("PAGES_RELAY_UPSTREAM_METRICS must be an absolute URL")
		}
		rl.Updates = mu
	}
	rl.start()
	mux := http.NewServeMux()
	mux.Handle("/hook", rl)
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		counters.WriteTo(w)
	})
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) { fmt.Fprintln(w, "ok") })
	srv := &http.Server{Addr: env("PAGES_RELAY_LISTEN", ":8080"), Handler: mux,
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 60 * time.Second}

	sig, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()
	served := make(chan error, 1)
	go func() { served <- srv.ListenAndServe() }()
	select {
	case err := <-served:
		fatal(err.Error())
	case <-sig.Done():
	}
	// SIGTERM (PreStopSleep after the pod began terminating; it served all the
	// while): stop accepting, let the deliveries being answered finish, release
	// every hold, then forward everything queued. Whatever is left at
	// DrainTimeout is logged by site name; a crash loses the queue (accepted:
	// redeliver from Forgejo).
	rl.Log.Info("shutdown: stop accepting", "drain_timeout", DrainTimeout.String())
	ctx, cancel := context.WithTimeout(context.Background(), DrainTimeout)
	defer cancel()
	srv.Shutdown(ctx)
	rl.Shutdown(ctx)
}
