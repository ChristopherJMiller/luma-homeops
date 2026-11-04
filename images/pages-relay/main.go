package main

import (
	"bytes"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"strings"
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
	mux := http.NewServeMux()
	mux.Handle("/hook", rl)
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		counters.WriteTo(w)
	})
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) { fmt.Fprintln(w, "ok") })
	srv := &http.Server{Addr: env("PAGES_RELAY_LISTEN", ":8080"), Handler: mux,
		ReadHeaderTimeout: 10 * time.Second, ReadTimeout: 30 * time.Second, WriteTimeout: 60 * time.Second}
	fatal(srv.ListenAndServe().Error())
}
