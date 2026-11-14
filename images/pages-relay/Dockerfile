# Forgejo -> git-pages webhook relay (images/pages-relay). Stdlib only.
#
# Builder: golang:1.26.9-alpine, tag pushed 2026-10-10 (past the 2-week rule;
# 1.26.10 is too new). Digest is the multi-arch index, read from the registry.
# Runtime: distroless static nonroot, same pin as forge-bot.
FROM golang:1.27.4-alpine@sha256:0a5f7fe796599ff487605987432e7619ae45beeb2fba4c2ce0b907879932aebf AS build
WORKDIR /src
COPY go.mod ./
COPY relay.go hold.go main.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o /pages-relay .

FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
COPY --from=build /pages-relay /pages-relay
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/pages-relay"]
