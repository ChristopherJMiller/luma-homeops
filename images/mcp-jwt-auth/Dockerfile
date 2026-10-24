# Forward-auth validator for the MCP endpoint (images/mcp-jwt-auth).
#
# Two stages so the shipped image carries no toolchain and no shell: this sits in
# the request path for family data, so its attack surface should be one static
# binary and nothing else.
FROM golang:1.27-alpine@sha256:b8c9b82affbbb82a020bbcf998dad2ba9516944ac7e883524642a481a9505e9c AS build
WORKDIR /src
# Dependencies first, so edits to main.go do not refetch them.
COPY go.mod go.sum ./
RUN go mod download
COPY main.go ./
# CGO off => a genuinely static binary, which is what distroless/static expects.
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /mcp-jwt-auth .

FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
COPY --from=build /mcp-jwt-auth /mcp-jwt-auth
USER nonroot:nonroot
EXPOSE 4181
ENTRYPOINT ["/mcp-jwt-auth"]
