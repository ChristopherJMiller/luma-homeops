# Forward-auth validator for the MCP endpoint (images/mcp-jwt-auth).
#
# Two stages so the shipped image carries no toolchain and no shell: this sits in
# the request path for family data, so its attack surface should be one static
# binary and nothing else.
FROM golang:1.27-alpine@sha256:8a5910f31396cd4d89662f56c68b3ae31d374308270a1c3bd96672ee5ed43414 AS build
WORKDIR /src
# Dependencies first, so edits to main.go do not refetch them.
COPY go.mod go.sum ./
RUN go mod download
COPY main.go ./
# CGO off => a genuinely static binary, which is what distroless/static expects.
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /mcp-jwt-auth .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /mcp-jwt-auth /mcp-jwt-auth
USER nonroot:nonroot
EXPOSE 4181
ENTRYPOINT ["/mcp-jwt-auth"]
