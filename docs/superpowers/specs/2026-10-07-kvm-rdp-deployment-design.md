# kvm-rdp on galaxy — deployment design

**Status:** draft for review · **Date:** 2026-10-07 · **Scope:** `mac.kvm.chrismiller.xyz`: rdpgw and the kvm-rdp bridge on galaxy, plus the edge and network changes they need.

**This is the companion spec** that kvm-rdp's design (`~/Repos/kvm-rdp/docs/superpowers/specs/2026-10-05-kvm-rdp-design.md`, rev 6.1) leaves to luma-homeops. That spec covers how the bridge works. This one covers where it runs, how traffic reaches it, and what it may reach. kvm-rdp §9.4 lists what it requires of this spec, and §11 of this document checks every item.

## 1. Intent

**Goal.** Chris's managed work laptop runs Microsoft Windows App (from the App Store). It should open the work Mac Studio's screen through the ES3 IP-KVM, **with no raw internet exposure of the Mac or the KVM**. The browser step is gated by Dex through the oauth2-proxy admin tier. The tunnel is gated by an rdpgw token and then NLA on the bridge.

**Settled by Chris (not reopened here):**

- The name is `mac.kvm.chrismiller.xyz`, which leaves room for more machines.
- rdpgw is pinned by digest, from master `16cdaaf` or a later security fix. It runs in **header-auth mode behind the oauth2-proxy admin tier**, which gates `/connect` and has one user.
- Websocket transport only. One replica.
- The KVM's own web UI is break-glass only (LAN or tailnet), and is **never on the edge**.
- The client is Windows App on Chris's Mac laptop.
- The bridge is the ES3's only automated client, and it **never logs out** of the KVM, because ES3 logout is global (kvm-rdp §3.2).

**Not in this spec:**

- The bridge's internals (kvm-rdp).
- Building the KVM VLAN. §6.3 only sketches the options; the build gets its own plan once Chris picks one.
- A second machine.
- A browser client path.

**Done when:**

1. FreeRDP connects through the gateway from off the LAN and drives the Mac.
2. Every check kvm-rdp §9.4 lists passes against the live deployment (§8, stage 3).
3. Chris has run the Windows App acceptance checklist (kvm-rdp §12), and its gateway-transport item passes.
4. The edge snapshot is unchanged for every existing host.

## 2. Starting state

Observed read-only on 2026-10-07.

| Thing | State |
|---|---|
| KVM | ES3 at `192.168.0.2` (`kvm` static mapping) on the flat LAN (`eth2`, `192.168.0.0/24`). WAN egress is dropped and logged by forward rule 120 (`NO-WAN-v4`). Reachable from: the whole LAN; **all three nodes at L2** (the router never sees node→KVM traffic, so it can't filter it); every pod (flannel SNATs pods to the node IP); every `hostNetwork` process; the tailnet (the router advertises a `/32` route). The pre-auth RCE port (`:8888`, CVE-2026-32297) is open to all of them. Its SPKI pin is in kvm-rdp `census.md` (`f948…614f`), taken before any factory reset. |
| CNI | **flannel. NetworkPolicy is not enforced.** The repo's position is that such a policy is "decorative" (`cluster/dex/grpc-tls.yaml`, `cluster/gramps/mcp.yaml`). Pod CIDR is `10.244.0.0/16`; Service CIDR is `10.96.0.0/12`. |
| Traefik | v3 (chart 41.x), 2 replicas, **both on `bottom` right now** (the anti-affinity is only preferred). The Service has `externalTrafficPolicy: Cluster`, so **every request in the last 30 min of access log (446 lines) came from one SNAT'd `10.244.3.x` address**. Traefik never sees a real client IP. `websecure` has `readTimeout: 0`. No `forwardedHeaders.trustedIPs`, so client-supplied `X-Forwarded-For` is discarded. |
| Edge cert | `traefik/wildcard-tls` has 9 SANs and no `*.kvm.chrismiller.xyz`. A request to `https://mac.kvm.chrismiller.xyz` gets a name mismatch today. |
| DNS | `mac.kvm.chrismiller.xyz` **already resolves to the WAN IP** through `*.chrismiller.xyz` (an authoritative answer from `jasmine.ns.cloudflare.com`, A only, no AAAA). Cloudflare still synthesises the wildcard below an empty non-terminal: `probe._domainkey` resolves, while `_domainkey` holds only children. It does **not** synthesise below a name that owns records: `probe._dmarc` is NXDOMAIN. |
| Admin tier | Cookie `_oauth2_proxy_admin` on `.chrismiller.xyz`, and `--whitelist-domain=.chrismiller.xyz`. Both are suffix matches, so a two-level host is covered. **No oauth2-gated two-level host exists yet** (`dav.music` is ungated), so the sign-in round trip on one is untested. |
| rdpgw | No release carries the security fixes. Upstream's `bolkedebruin/rdpgw:latest`/`:master` is rebuilt from a fresh `git clone` of master, with `go mod tidy` at build time (`dev/docker/Dockerfile`), so its digest doesn't identify a commit. Upstream commits no `go.sum`. |
| kvm-rdp | Plans A–B are done (rev 6.1). Plans C–E (bridge, CI, image) are being built. No image has been published yet. |
| Nodes | `middle` is at 88% of memory *requests*. Asterisk is `hostNetwork` on `top`. `top`'s NVMe swap (#2661) is pending. |

## 3. Architecture

### 3.1 Data path

```
 managed Mac laptop
   browser ── 1. GET https://mac.kvm.chrismiller.xyz/connect  (Dex sign-in via auth-admin; downloads the .rdp)
   Windows App ── 2. opens the .rdp: gateway mac.kvm…:443, target mac-bridge.kvm…:3389, PAA token
        │
        ▼  Cloudflare DNS, DNS-only:  *.chrismiller.xyz → apex → WAN IP
 VyOS eth0 :443 ── DNAT rule 300 ──▶ 192.168.0.7  (MetalLB; Traefik Service, externalTrafficPolicy Local — §5.3)
        ▼
 Traefik websecure — wildcard-tls (+ *.kvm.chrismiller.xyz, §5.1)
   ├─ Exact /connect                [admin-errors → admin-auth → strip-set-cookie] ─┐
   ├─ Exact /remoteDesktopGateway/  [strip-set-cookie]                              │
   └─ anything else on this host → 404                                              ▼
 pod rdpgw-mac (ns kvm)
   relay  nginx :8080 ── /connect: auth_request ─▶ oauth2-proxy-admin.oauth2-proxy:4180/oauth2/auth
     │                   stamps X-Forwarded-User, appends X-Forwarded-For, hides Set-Cookie
     └──▶ 127.0.0.1:8081 rdpgw (header + token mode, websocket transport, TLS off)
               │ TCP mac-bridge.kvm.chrismiller.xyz:3389 → hostAliases → bridge's fixed ClusterIP
               ▼
 pod kvm-rdp-mac (ns kvm)   :3389 RDP (own LE RSA cert + NLA)   :9464 /metrics /livez /readyz
               │ https 443 / 8881 / 8889, SPKI-pinned, leaves the node SNAT'd to the node IP
               ▼
 ES3 192.168.0.2 ── HDMI + USB ── Mac Studio
```

### 3.2 Who sees what

| Layer | Between | Certificate |
|---|---|---|
| Outer HTTPS (gateway) | laptop ↔ Traefik | `wildcard-tls` (LE) |
| In-cluster HTTP and websocket | Traefik → relay → rdpgw | None (pod network, then loopback). Once the tunnel is up, it carries only the inner TLS bytes |
| RDP TLS + CredSSP (NLA) | Windows App ↔ bridge, **end to end through rdpgw** | The bridge's own LE RSA cert (kvm-rdp §9.1; §4.5 below) |
| KVM TLS | bridge ↔ ES3 | The ES3's self-signed cert, SPKI-pinned |

Traefik, the relay and rdpgw only ever see the RDP session as ciphertext. Keystrokes and pasted text are plaintext only inside the bridge and on the (hostile) KVM. kvm-rdp §9.3 accepts the latter.

### 3.3 Approaches considered

| Question | Choice | Rejected, and why |
|---|---|---|
| How rdpgw learns who the user is | **Header mode. A pod-local nginx relay re-checks the admin session** against the central `oauth2-proxy-admin` (`auth_request`) and is the only process that can reach rdpgw (loopback). | **Trusting Traefik's pod address.** Flannel enforces no NetworkPolicy, so any pod could forge the header. **An oauth2-proxy admin sidecar** (kvm-rdp §9.4's own suggestion) needs the admin tier's cookie secret, Dex client secret and allowlist inside the pod: either copied into `kvm` or by running rdpgw in `oauth2-proxy`. The cookie secret can forge an admin session for every admin host. The relay holds no secrets, and "who is admin" keeps one definition, the same goal as `mcp-jwt-auth`. **rdpgw `openid` against Dex.** Dex authenticates any Google or Microsoft account and rdpgw has no allowlist (Dex's Google connector also lacks `preferred_username`). |
| Bridge's RDP certificate | **cert-manager + LE, RSA 2048, for a dedicated name** (§4.5) | **The edge wildcard:** kvm-rdp §9.1 forbids it, because a compromised bridge must not hold the key for every public host. **Self-signed:** Windows App prompts, and kvm-rdp §5.1 needs a prompt-free TLS + NLA within 10 s for takeovers. Whether the managed laptop lets Windows App trust it permanently is unknown (open question 3). **`edge-ca`:** the laptop doesn't trust it. |
| rdpgw signing and session keys | **Unset, so rdpgw generates them at start** (`crypto/rand`, 32 characters from a 63-character alphabet, about 190 bits) | **A SealedSecret** would persist a key for no benefit at one replica. Random keys also mean **any rdpgw restart revokes every outstanding token**, which makes it a free kill switch. |
| rdpgw image | **Build our own** in `images/rdpgw/` | The upstream `:master` digest names a build, not a commit (§2). |
| Where the bridge runs | **In the cluster, for v1** | A satellite host is the end-state candidate (§6.3, open question 6). |

## 4. Objects: `cluster/kvm/`

### 4.1 Layout

```
cluster/applications/kvm.yaml        Argo Application (path cluster/kvm, prune + self-heal, like galaxy-site)
cluster/kvm/
  kustomization.yaml                 configMapGenerator for the three configs below (hash rolls the pods)
  namespace.yaml                     ns kvm, pod-security.kubernetes.io/enforce: restricted
  bridge-mac.yaml                    Deployment + Service kvm-rdp-mac
  bridge-mac-cert.yaml               Certificate kvm-rdp-mac-tls
  kvm-rdp-mac.secret.yaml            git-crypt cleartext (§4.5)  → ./sign.sh →  kvm-rdp-mac.yaml (SealedSecret)
  rdpgw-mac.yaml                     Deployment + Service rdpgw-mac (relay + rdpgw)
  ingress-mac.yaml                   Ingresses mac-connect, mac-tunnel
  middlewares.yaml                   Middleware strip-set-cookie
  servicemonitor.yaml                release: prometheus
  prometheusrule.yaml                namespace prometheus, labels release: prometheus (+ legacy prometheus: kube-prometheus, role: alert-rules)
  config/bridge-mac.toml  config/rdpgw-mac.yaml  config/defaults.rdp  config/relay.conf
images/rdpgw/                        Dockerfile + committed go.sum; .github/workflows/rdpgw-image.yml
```

The `-mac` suffix leaves room for a second machine: copy the four `*-mac` files and add one more host. Nothing else is shared state.

### 4.2 Bridge: `kvm-rdp-mac`

- **Deployment:**
  - `replicas: 1` and `strategy: Recreate`, so there is never a second automated KVM client, even briefly. No PDB: a PDB on a single replica blocks drains (S3).
  - `automountServiceAccountToken: false` and `enableServiceLinks: false`.
  - Pod `securityContext` as the restricted profile requires: non-root, `seccompProfile: RuntimeDefault`. Container: `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, drop `ALL`. The bridge writes nothing.
  - `args: [--config, /etc/kvm-rdp/config.toml]`.
- **Mounts:** the generated ConfigMap at `/etc/kvm-rdp`; Secret `kvm-rdp-mac` at `/run/secrets/kvm-rdp`; Secret `kvm-rdp-mac-tls` at `/run/secrets/kvm-rdp-tls`. **No `subPath`.** A `subPath` mount never receives Secret updates, and the bridge restarts itself when the certificate file changes (kvm-rdp §4.4).
- **Config** (`config/bridge-mac.toml`):
  - `rdp.listen 0.0.0.0:3389`, `rdp.username kvm`, `admin.listen 0.0.0.0:9464`.
  - `kvm.host 192.168.0.2`, `kvm.spki_sha256` set to the pin, scheme `https`.
  - The four `*_file` keys point at the mounts.
  - Every value kvm-rdp §4.4 marks *acceptance* stays at its default until Chris's Windows App run retunes it here, through git.
- **Probes:** `httpGet /livez` (liveness) and `/readyz` (readiness) on 9464. **Never a TCP probe on 3389:** it would spend `security.accept_rate` (30 per 60 s) and look like connection attempts. `terminationGracePeriodSeconds: 15`; the bridge exits within 5 s of SIGTERM (kvm-rdp §5.2). **No preStop hook**, because the bridge never logs out.
- **Resources:**
  - Requests `cpu: 50m`, `memory: 48Mi`. The target is under 5% of a core at 1080p30.
  - Limit `memory: 128Mi` until Plan E's soak measures steady RSS. Then: RSS + `backlog_limit` (4 MiB) + 16 MiB (kvm-rdp §10.1 stalled-client bound) + 50%.
  - An OOM kill from an abusive, already-authenticated clipboard is accepted (kvm-rdp §8).
  - `MALLOC_ARENA_MAX=2` if the image is glibc-based. That is the condition the soak ran under, so the limit derived from it only holds with the same setting.
- **Service `kvm-rdp-mac`:**
  - ClusterIP with a **fixed `spec.clusterIP`**: a free address in `10.96.0.0/24`, the static band of the `/12`, checked free at implementation.
  - Ports `rdp` 3389/TCP and `metrics` 9464.
  - The address is fixed because rdpgw's pod maps the bridge's public certificate name to it (§4.3).
  - **No LoadBalancer, ever.** Nothing outside the cluster dials 3389.

### 4.3 Gateway: `rdpgw-mac`

**One pod, two containers, `replicas: 1`, `strategy: Recreate`.** Recreate means a `/connect` and its tunnel can't land on two processes holding different random keys. Same security context as the bridge, and `automountServiceAccountToken: false`.

- **`relay`:** the nginx image the repo already runs (`public.ecr.aws/docker/library/nginx:1.29-alpine@sha256:…`, the galaxy-site pattern: uid 101, port 8080, `emptyDir` for `/var/cache/nginx` and `/var/run`). The official build includes `auth_request`; confirm with `nginx -V` at implementation. Port `http` 8080 is Traefik's backend. Port `metrics` 9090 exists only for Prometheus; no Ingress references it.
- **`rdpgw`:** `images/rdpgw` (§4.6), listening on **`127.0.0.1:8081` only**.
- **`hostAliases`:** `mac-bridge.kvm.chrismiller.xyz` → the bridge's fixed ClusterIP. rdpgw dials exactly the string it writes into `full address`, and in-cluster DNS would resolve the public name to the WAN IP. Go's resolver reads `/etc/hosts` first when there is no `nsswitch.conf`; stage 3 verifies it.
- **Probes:** `relay` gets liveness on `:8080/healthz` (a local `return 200`). `rdpgw` gets liveness on `:9090/metrics`, which reaches rdpgw through the relay. Kubelet probes the pod IP and restarts the container the probe is attached to, which is the one being tested.

`config/rdpgw-mac.yaml`. Every line is load-bearing:

```yaml
Server:
  Tls: disable                         # TLS ends at Traefik; relay → rdpgw is loopback
  GatewayAddress: mac.kvm.chrismiller.xyz   # → gatewayhostname in the .rdp
  BindAddress: 127.0.0.1               # nothing outside the pod can reach rdpgw
  Port: 8081
  Authentication: [header]
  HostSelection: roundrobin            # one curated host; never `any` (§9.4)
  Hosts: [mac-bridge.kvm.chrismiller.xyz:3389]
  TrustedProxies: [127.0.0.1/32]       # take the client IP from X-Forwarded-For (leftmost entry)
  SessionStore: cookie                 # SessionKey/SessionEncryptionKey unset → random per process
Header:
  UserHeader: X-Forwarded-User
  TrustedProxies: [127.0.0.1/32]       # only the relay may stamp the user
Security:
  VerifyClientIp: true                 # PAA tokens pinned to the fetching address (§5.3)
                                       # PAATokenSigningKey/EncryptionKey unset → random per process
Caps:
  TokenAuth: true
  EnableClipboard: true                # paste-as-typing (kvm-rdp §8); rdpgw's default disables CLIPRDR
  IdleTimeout: 0                       # the bridge owns session liveness (kvm-rdp §6.9)
Client:
  Defaults: /etc/rdpgw/defaults.rdp    # one line: username:s:kvm  (== rdp.username)
  NoUsername: true                     # don't overwrite it with the admin identity
  # RdpOverridableKeys: (none) — /connect?… can never rewrite the .rdp
```

`config/relay.conf`, in outline. `map $http_upgrade $connection_upgrade` sits at http level.

```nginx
server {                                   # :8080 — Traefik's backend
  listen 8080;
  location / { return 404; }               # rdpgw's /, /api/v1/*, /metrics, /tokeninfo: never via this port
  location = /healthz { access_log off; return 200; }

  location = /connect {                    # second, pod-local check of the admin tier
    auth_request /_admin;                  # 202 → continue; 401 → 401 (the edge already handled browsers)
    auth_request_set $admin $upstream_http_x_auth_request_email;
    proxy_set_header X-Forwarded-User $admin;           # replaces anything the caller sent
    proxy_set_header X-Forwarded-For  $proxy_add_x_forwarded_for;
    proxy_hide_header Set-Cookie;                       # RDPGWSESSION never leaves the pod
    proxy_pass http://127.0.0.1:8081;
  }
  location = /remoteDesktopGateway/ {      # MS-TSGU over websocket; the PAA token is the credential
    proxy_set_header X-Forwarded-User "";
    proxy_set_header X-Forwarded-For  $proxy_add_x_forwarded_for;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 1h; proxy_send_timeout 1h;       # default 60 s would cut a stalled stream
    proxy_hide_header Set-Cookie;
    proxy_pass http://127.0.0.1:8081;
  }
  location = /_admin {
    internal;
    proxy_pass http://oauth2-proxy-admin.oauth2-proxy.svc.cluster.local:4180/oauth2/auth;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
  }
}
server { listen 9090; location = /metrics { proxy_pass http://127.0.0.1:8081; } location / { return 404; } }
```

- Don't test `$admin` in an `if`. `if` runs before `auth_request`, so it always sees the variable empty. rdpgw already refuses an empty user header with 401 `No authenticated user from proxy`.
- The tunnel location lets every method through. That keeps acceptance item 2's diagnostic intact: if Windows App falls back to the legacy transport, `Opening RDGOUT` still shows in rdpgw's log.

**Service `rdpgw-mac`:** ports `http` 80 → 8080 and `metrics` 9090. No sticky annotations (§5.3).

### 4.4 Ingresses and middlewares

There are two Ingresses for the one host, because the middleware annotation applies per Ingress. This is the same split as `cluster/media/ombi-ingress.yaml`. Neither has `spec.tls` and neither needs a DNS record: one Ingress per host is still the whole edge procedure.

| Ingress | Path (`pathType: Exact`) | `router.middlewares` (in this order) | Why |
|---|---|---|---|
| `mac-connect` | `/connect` | `oauth2-proxy-oauth2-admin-errors`, `oauth2-proxy-oauth2-admin-auth`, `kvm-strip-set-cookie` (all `@kubernetescrd`) | The standard admin pair, `errors` first. `strip` goes **last**, so it wraps only rdpgw's response and never the sign-in redirect, whose CSRF cookie must survive. `/connect` is a document download, so the Ombi "backend 401" caveat doesn't apply. |
| `mac-tunnel` | `/remoteDesktopGateway/` | `kvm-strip-set-cookie` | **Deliberately not gated at the edge.** Windows App carries no browser cookie; the PAA token is checked inside rdpgw. It is the only path exempt from forward-auth (kvm-rdp §9.4). |

- Every other path on `mac.kvm.chrismiller.xyz` has no router, so Traefik answers 404. Traefik v3's path sanitising turns `/remoteDesktopGateway/../connect` into `/connect`, which is gated.
- `Middleware strip-set-cookie` (ns `kvm`) is `headers.customResponseHeaders: {Set-Cookie: ""}`. It is the edge half of the cookie strip kvm-rdp §9.4 asks for. The relay's `proxy_hide_header` is the pod half, which also covers in-cluster callers. The relay half alone would close the bypass, because rdpgw is unreachable except through it. The edge half is what the edge snapshot can check.
- Add `mac.kvm` to `.claude/skills/edge-ingress/edge-snapshot.sh`. Extend its path loop for this host to `/connect`, `/remoteDesktopGateway/` and `/metrics`, and add `set-cookie: rdpgwsession` to its header grep.

### 4.5 Secrets: what each is, and where it comes from

| Material | Where it lives | Source |
|---|---|---|
| KVM password (`kvm-password`) | `kvm-rdp-mac.secret.yaml` (git-crypt) → `./sign.sh` → SealedSecret | Set fresh, 20+ random characters, in the ES3 UI at the pre-deployment factory reset (§8, stage 2). The ES3 has one password, so break-glass and `kvm-probe` use the same one. |
| NLA password (`nla-password`) | Same Secret: one Secret document, two keys (`sign.sh` reads only the first document in a file) | `openssl rand -base64 24`, 192 bits (≥ 128 required by kvm-rdp §9.1). Stored recoverably because NTLM needs it. Saved once in Windows App on the laptop. Username `kvm` is config, not secret. |
| Bridge TLS cert + key | Secret `kvm-rdp-mac-tls`, **written by cert-manager**, not in git | `Certificate kvm-rdp-mac-tls`: `letsencrypt-prod` (DNS-01, same ClusterIssuer), `dnsNames: [mac-bridge.kvm.chrismiller.xyz]`, `privateKey: {algorithm: RSA, size: 2048, rotationPolicy: Always}`. |
| KVM SPKI pin | `config/bridge-mac.toml` (ConfigMap). **Not a secret:** it hashes a public key and is already public in kvm-rdp `census.md` | `kvm-probe fingerprint` after the factory reset. The reset may regenerate the ES3's self-signed cert. |
| rdpgw PAA signing/encryption and session keys | Process memory only | Generated by rdpgw at start (§3.3). |
| Admin-tier secrets (cookie secret, Dex client secret, allowlist) | **Nowhere new** | They stay in `oauth2-proxy`. The relay asks the central instance. |

**Why a dedicated LE certificate is allowed here.** The edge-ingress rule "never a per-host Certificate" is about hostnames Traefik serves. Traefik never serves this name; only the bridge presents it, inside the tunnel. The name exists so Windows App can validate the bridge with no prompt.

- Its DNS-01 TXT (`_acme-challenge.mac-bridge.kvm…`) only creates empty non-terminals, which Cloudflare keeps synthesising past (§2). So no public name stops resolving during a challenge.
- The cost is one CT-log entry, plus a bridge restart at each renewal. That restart ends a live session, roughly every 60 days at LE's current lifetimes and more often as they shrink (§10.9).

### 4.6 Images

- **rdpgw** (`images/rdpgw/`):
  - Multi-stage build. The golang builder is pinned by digest. It clones `bolkedebruin/rdpgw` at the **full commit SHA**, copies in a `go.sum` we generate once at that commit and review, runs `go mod verify`, then `CGO_ENABLED=0 go build -trimpath -mod=readonly ./cmd/rdpgw`.
  - The final stage is distroless `static:nonroot`, pinned by digest. Templates aren't needed: rdpgw falls back to an embedded page, and `/` isn't routed anyway.
  - Built by `.github/workflows/rdpgw-image.yml` like the other `images/*`, and pinned in the Deployment as `ghcr.io/christopherjmiller/rdpgw:sha-<commit>@sha256:…`. Same two-commit pattern as CLAUDE.md sharp edge 6.
  - **Bumping the rdpgw commit is manual**, and it re-runs the stage-3 checks: kvm-rdp §9.4 says any pin other than `16cdaaf` is re-checked against the Leg C findings.
- **kvm-rdp:** recommended source is kvm-rdp's own CI (Plan E). kvm-rdp §9.1 already makes "the published image rejects any non-Hybrid mode" a CI check on the published artifact. It would be pinned here as `ghcr.io/christopherjmiller/kvm-rdp:sha-<commit>@sha256:…`, with the same two-commit rule (open question 1). The image must run non-root on a read-only root filesystem under the restricted profile, and must be `amd64`, plus `arm64` if open question 6 lands on a Pi.
- **nginx:** the existing pinned image. Renovate already tracks it.

## 5. Edge changes

### 5.1 Wildcard cert: add `*.kvm.chrismiller.xyz`

`*.chrismiller.xyz` matches one label only, which is the same reason `*.music.chrismiller.xyz` exists. The change is one line in `cluster/traefik/wildcard-cert.yaml` `dnsNames` (still one certificate). Also update the SAN list in the edge-ingress SKILL.

**Blast radius:** every public host on all four zones.

- cert-manager re-issues for the new SAN set. Expect a DNS-01 order covering up to 10 names, in the same Cloudflare zone and with the same token.
- `rotationPolicy: Always` means **every host gets a new key** when it lands. Traefik hot-reloads the default TLSStore. Nothing in the repo pins the edge key.
- **If issuance fails, the old Secret keeps serving.** cert-manager writes it only on success. The only effect is that the new host keeps its name mismatch.
- LE limits aren't a concern: a new name set isn't a duplicate certificate, and this is far under 50 certificates a week per domain.
- CT logs will publish `*.kvm.chrismiller.xyz`, which reveals the sub-zone but no names in it.
- **Procedure:** run the edge snapshot before and after; `kubectl -n traefik get certificate wildcard-tls` must show Ready; `openssl s_client -servername mac.kvm.chrismiller.xyz` must show the SAN. The snapshot diff must be empty (it already ignores `cert=`).
- **Rollback:** revert the line, which costs one more harmless re-issue.

### 5.2 DNS: no change

The wildcard already answers for `mac.kvm.chrismiller.xyz`, and Cloudflare keeps synthesising it while `_acme-challenge.kvm…` exists (§2). `./cloudflare/dns/tf.sh plan` stays empty. Two rules follow from the measurements:

1. **Never put a record *at* `kvm.chrismiller.xyz`.** Once that name owns a record, Cloudflare stops synthesising for names below it, just as `probe._dmarc` is NXDOMAIN. If one is ever needed, add an explicit `*.kvm` CNAME in the same change.
2. **Never make `mac.kvm` proxied.** Cloudflare would terminate the outer TLS, and every client would arrive as a Cloudflare address. That makes token pinning meaningless.

### 5.3 Traefik: websockets, timeouts, stickiness, cookies, client address

- **Websockets** are native, and nothing in the chart is needed for them.
  - `websecure` already has `readTimeout: 0` (deliberate, for attic and WebDAV), so the upgrade request isn't cut at 60 s.
  - `writeTimeout` defaults to 0.
  - `idleTimeout` (180 s) applies between keep-alive requests only. Go clears connection deadlines when a connection is hijacked, so an upgraded websocket has no Traefik-side deadline.
  - `serversTransport` sets no response or read deadline (`responseHeaderTimeout: 0`).
  - **No Traefik timeout change.** Stage 4's 60-minute soak, which includes an idle-input stretch, is the proof. The ES3 sends 30 fps even on a static screen (`census.md`), so a live tunnel is never idle in practice.
- **Stickiness:** none. There is one rdpgw replica, and websocket transport is one TCP stream through whichever Traefik pod accepted it. The legacy two-half-channel transport would need sticky routing at more than one replica, but `16cdaaf` refuses it in token mode anyway.
- **What does cut a session:** a Traefik pod restart (chart bump, drain), an rdpgw, relay or bridge restart, a bridge cert renewal, or MetalLB moving `.7`. Windows App then reconnects with the same token if it's under about 6 min old. Otherwise fetch `/connect` again.
- **Cookies:** `strip-set-cookie` (§4.4).
- **Client address: a prerequisite, not a nicety.** With `externalTrafficPolicy: Cluster`, kube-proxy SNATs every external connection. So Traefik writes the SNAT address into `X-Forwarded-For` for every client (§2), and `VerifyClientIp` would pin every token to the same `10.244.3.x`, which pins nothing. The change:
  - `service.spec.externalTrafficPolicy: Local` in `cluster/applications/traefik-release.yaml`, which follows the `wg-gateway` and CephNFS precedent.
  - Make Traefik's pod anti-affinity **required** (2 replicas, 3 nodes). With `Local`, MetalLB L2 announces `.7` only from a node that has a ready Traefik pod, and today both pods sit on `bottom`.
  - Keep `forwardedHeaders` untrusted. Never `insecure`, never `0.0.0.0/0`. Traefik then discards any `X-Forwarded-For` a client sends and writes the real peer address. The relay appends Traefik's pod IP, and rdpgw takes the leftmost entry from its trusted loopback peer.
  - **Blast radius:** edge-wide. Backends start seeing real client addresses. Home Assistant's `trusted_proxies: 10.0.0.0/8` stays correct, because the peer is still Traefik's pod. LAN clients arrive as `192.168.0.1` through reflection rule 110, the same for `/connect` and the tunnel. Edge snapshot before and after. Chris signs off on the change (open question 4).
  - **Without it,** ship `VerifyClientIp: true` anyway (harmless) and record that tokens are effectively unpinned bearer credentials for about 6 min.

## 6. Network and isolation

### 6.1 Phase 1, now: what reaches what

- **Bridge → KVM:** any node can reach `.2` at L2, and the pod's traffic leaves SNAT'd to its node's IP. The bridge dials only `kvm.host` on 443, 8881 and 8889, but **by construction only: nothing at the pod or router level enforces it.** It needs no DNS and no other egress.
- **→ bridge `:3389`:** any pod. **NLA is the only in-cluster control** (kvm-rdp §9.3: an in-cluster caller can also occupy the 10 s Preempt candidate slot).
- **→ rdpgw:** only through the relay. rdpgw's port is loopback-only.
- **→ KVM:** unchanged from §2. **Phase 1 makes the KVM no more exposed than it is today**, and it doesn't make it less exposed either. kvm-rdp §9.4's isolation requirement is **not met in Phase 1** (§11).

### 6.2 NetworkPolicy and node affinity

- **No NetworkPolicy objects.** Under flannel they are decorative (the repo's precedent). kvm-rdp §9.1 counts a policy as a layer only if enforcement is shown. If the CNI ever enforces policy, these are the policies to add:
  - `kvm` default-deny.
  - Bridge ingress on 3389 from `rdpgw-mac` only, and on 9464 from Prometheus and the node CIDR (kubelet probes).
  - Bridge egress to the KVM's three TLS ports only.
  - Relay egress to `oauth2-proxy-admin:4180` and CoreDNS.
  - A cluster-wide deny to the KVM for everything else.
- **No node affinity in Phase 1.** Every node reaches `.2` equally, so pinning would only shorten availability. Pinning becomes required in Phase 2 option A, where the router admits one node's IP.

### 6.3 Phase 2: isolation (own plan, router change under commit-confirm)

The common part, whatever Chris picks:

- The KVM moves to its own VLAN, built like voice VLAN 20: VyOS `eth2` vif N, TRENDnet port untagged PVID N, uplink tagged N.
- A `KVM-OUT` chain that drops every flow the KVM starts (LAN, WAN, router). DHCP keeps working through Kea's raw sockets.
- An explicit WAN → KVM drop. The forward filter has no default deny.
- An address-group swap in `NO-WAN-v4`, with rule 120 kept as the WAN guard. Only an allowlist may start flows into the VLAN.
- On its own, this stops a compromised KVM attacking the LAN at L2, which today it can.
- The router also starts seeing KVM-originated flows, so the "alert on KVM-originated LAN flows" in kvm-rdp §9.3 becomes possible: a logged drop rule.

Then the bridge identity. kvm-rdp §9.4 requires one that can be told apart from everything else:

| Option | Bridge identity | Meets §9.4? | Cost |
|---|---|---|---|
| **A. Node-pinned pod** | The pinned node's IP. The router admits it only, and only to `tcp 443,8881,8889`. | **No.** It narrows to *anything on that node*: every pod and every `hostNetwork` process. The router does block the plaintext ports (`:8888` among them) even from that node. | Ansible plus one `nodeSelector`. A drain of that node takes the bridge down. Pick `top`, not `middle` (memory requests), and revisit after #2661. |
| **B. Multus / macvlan** | The bridge pod gets a second interface on VLAN N, which can be L2-only with no router interface at all. | Yes | A new cluster-wide CNI component on a cluster where every node is control plane. Talos machine-config VLAN links (S4: confirm first). Node switch ports tagged N. A compromised bridge then spans the pod network and the KVM VLAN. |
| **C. Satellite** | A small NixOS box on VLAN N runs the bridge under systemd: kvm-rdp §9.4's dedicated-host variant, with `MemoryMax`, a `/livez` timer, and a host firewall. rdpgw stays in the cluster; the router admits galaxy node IPs to `:3389` and Prometheus to `:9464`. | Yes | Hardware, packaging kvm-rdp in Nix, comin, and the box's own WAN allowance for comin and the Nix cache. The bridge leaves Argo. |

**Recommendation: A soon after acceptance, C as the end state** (open question 6). The deciding fact is galaxy's own: **there is no worker pool, and every node is etcd plus API server (S5).** The bridge is the one workload whose input is attacker-controlled by design. In-cluster, the only thing between a KVM-borne exploit and a control-plane node is container isolation, and under flannel a compromised bridge pod can reach every ClusterIP Service. C removes both problems; B removes neither.

Ordering: isolation happens **after** Plan C's L4 hardware checks, which run from the dev host before isolation (kvm-rdp §11.6), and **after** the Windows App run. Afterwards, factory-reset the KVM again (kvm-rdp §9.3).

### 6.4 Break-glass and Tailscale

Chris's call is that the vendor UI stays break-glass on the LAN and tailnet. kvm-rdp §9.4 says to remove the KVM's tailnet `/32` route once the bridge works, and the KVM remote-access notes say direct tailnet or LAN reachability isn't acceptable break-glass while `:8888` is a pre-auth RCE. Those conflict, so this is open question 5.

**Proposal:** keep the route through acceptance, since it's the only view of the Mac when the bridge misbehaves. At Phase 2, replace it with an *authenticated hop* rather than raw reachability:

- Option A: a short-lived `socat` debug pod on the allowlisted node, plus `kubectl port-forward`. That is gated by kube RBAC, from anywhere the kubeconfig works.
- Option C: `ssh -L` through the bridge host.

If the route stays anyway, restrict it with a tailnet ACL to Chris's own devices. That lives in the admin console, not the repo. Under the default ACLs, every tailnet device gets every approved route.

Logging out of the vendor UI ends the bridge's KVM token too (logout is global). The bridge recovers by re-logging in once (kvm-rdp §6.9). That costs nothing, but break-glass users should know.

## 7. Observability

- **ServiceMonitor `kvm`** (`release: prometheus`, or it is silently never scraped): selects both Services by a shared label (`app.kubernetes.io/part-of: kvm`) with endpoint port `metrics`. That is the bridge's `:9464` and the relay's `:9090` → rdpgw `/metrics`. `targetLabels: [app.kubernetes.io/name]`. Confirm the targets are `up` with the Prometheus targets query in `docs/auth.md`, using namespace `kvm`.
- **Neither metrics port is reachable from the edge.** Traefik routes only the two Exact paths to port `http`. The bridge's admin listener is cluster-internal (kvm-rdp §4.5 keeps secrets out of it).
- **PrometheusRule `kvm-alerts`** (ns `prometheus`, labels `release: prometheus` plus the legacy `prometheus: kube-prometheus, role: alert-rules`). Since 0b Task 5 (2026-10-21) the ruleSelector is `release: prometheus`. Alert names avoid Volume, Storage and Ceph so they route to homelab-default.
  - `KvmBridgeDown`: the bridge target is down for 10 min.
  - `KvmBridgeUpstreamFatal`: any fatal upstream disconnect cause (`kvm_*`, `stream_*`) in 15 min. Plan C names the series.
- **Acceptance reads:** `rdpgw_websocket_connections` / `rdpgw_legacy_connections` (item 2), and the bridge's §10.4 series.
- **Not covered:** alerting on hits on rule 120 (kvm-rdp §9.3). The router has no path that exports firewall counters to Prometheus. Check them by hand with `show firewall ipv4 forward filter rule 120`, and treat automation as a follow-up.

## 8. Rollout

Each stage ends at a check, and nothing moves on until it passes.

**Stage 0: prerequisites (no runtime change).**
- kvm-rdp Plans C–E are done, and the bridge passes a kvm-sim dry run.
- The kvm-rdp image is published. `images/rdpgw` is built.
- Chris has answered open questions 1–4.

**Stage 1: edge preparation.**
- §5.1 SAN change, with a snapshot before and after: the diff is empty, and the SAN is present.
- §5.3 Traefik `Local` plus required anti-affinity, with a snapshot before and after: the diff is empty. The Traefik pods are on two nodes. The access log shows a phone-on-LTE address for a test request, not `10.244.x`.
- Neither change depends on the bridge, and each can be reverted alone.

**Stage 2: KVM preparation and the bridge (no Ingress yet).**
- **KVM preparation:**
  - Factory-reset the ES3 (kvm-rdp §9.3).
  - Set a fresh password.
  - Pin the preset to 1920×1080 at 30 fps (kvm-rdp §6.1).
  - Keep "Broadcasting IP" (UDP 33333) off.
  - `kvm-probe fingerprint`. If the pin changed, it goes in the TOML.
- Commit the Secret, Certificate, bridge Deployment and Service.
- **Checks:**
  - `Certificate kvm-rdp-mac-tls` is Ready.
  - The pod is Ready on `/readyz`.
  - The bridge log shows **no KVM traffic before an authenticated connection** (kvm-rdp §5.1).
  - The Prometheus target is `up`.
- **Direct smoke test:** run `kubectl -n kvm port-forward svc/kvm-rdp-mac 3389` from rowlett, with `/etc/hosts` mapping `mac-bridge.kvm.chrismiller.xyz` to `127.0.0.1`, then `xfreerdp /v:mac-bridge.kvm.chrismiller.xyz /u:kvm /gfx:AVC420` **without `/cert:ignore`**.
  - The certificate validates.
  - NLA completes.
  - The Mac's picture appears within N (kvm-rdp §10.3).
  - Typing works.
  - Stop the forward afterwards.

**Stage 3: gateway and Ingress. Negative tests first.**
- From outside the cluster:
  - `/connect` with no cookie → 302 to `auth-admin…/oauth2/sign_in`.
  - `/`, `/metrics`, `/api/v1/hosts`, `/tokeninfo` and `/static/app.js` → 404.
- From a throwaway in-cluster pod:
  - `rdpgw-mac/connect` with a forged `X-Forwarded-User` → 401 (the relay's check fails).
  - The pod IP on `:8081` → connection refused.
  - A direct `/connect` carrying an `RDPGWSESSION` cookie → 401.
  - These are kvm-rdp §9.4's checks, adapted: rdpgw itself is unreachable.
- **Signed-in browser:**
  - The Dex round trip lands back on `/connect`, proving the two-level host works with the admin cookie and the whitelist.
  - The `.rdp` has `gatewayhostname:s:mac.kvm.chrismiller.xyz`, `full address:s:mac-bridge.kvm.chrismiller.xyz:3389` and `username:s:kvm`.
  - The response carries **no `Set-Cookie`** (`curl -i` with the session cookie; don't paste the cookie anywhere).
  - The 101 response on the tunnel path carries no `Set-Cookie` either (a raw upgrade request with `curl -i`).
  - Decoding the token's middle segment (HS256, unencrypted) shows `clientIp` equal to the fetching network's public address.
- rdpgw's log shows the dial to the bridge's ClusterIP, which confirms `hostAliases`.

**Stage 4: FreeRDP through the gateway, from off the LAN (phone hotspot).**
- The `.rdp` fetched over the hotspot, plus `xfreerdp … /gfx:AVC420` and the NLA password: the session works.
- `rdpgw_websocket_connections 1`, `rdpgw_legacy_connections 0`.
- The paste chord types into the Mac. That proves `EnableClipboard`.
- **Pinning:** a `.rdp` fetched on the LAN and used over the hotspot is refused (`E_PROXY_RAP_ACCESSDENIED`, and rdpgw logs the IP mismatch).
- A 60-minute soak that includes 15 min with no input: no disconnect.
- Edge snapshot with the new host added. The only new lines are the intended ones.

**Stage 5: Windows App acceptance.** Chris runs the kvm-rdp §12 checklist, through the gateway and direct. The direct leg uses a temporary `kubectl port-forward --address <rowlett LAN IP>`, expects a name-mismatch prompt, and stops the forward afterwards.
- **Item 2 (websocket transport) is this deployment's go/no-go.** If Windows App uses legacy HTTP, stop: rdpgw needs a patch or a different pin (kvm-rdp §9.4).
- Results go in kvm-rdp's `census.md`. *Acceptance* values are retuned in `config/bridge-mac.toml` through git.
- Also record:
  - Whether the corporate network splits `/connect` and the tunnel across egress addresses (open question 7).
  - Whether Windows App warns about an unsigned `.rdp`. rdpgw's `Client.SigningCert` is the remedy if it nags.
  - Which username/domain form it sends (kvm-rdp item 9).

**Stage 6: isolation (Phase 2).** Its own plan, written after open question 6 is answered. It goes through `router/ansible/` only, under commit-confirm (S4, S8).
- **Exit:** TCP connects to every KVM service port, plus a full port scan, all **fail** from a non-bridge pod, a node shell (or, for option A, a node other than the pinned one) and the tailnet. kvm-rdp §9.4's matrix.
- Factory-reset the KVM again afterwards.
- The tailnet route is handled per open question 5.

**Stage 7: write-up.**
- `docs/kvm.md` runbook.
- A CLAUDE.md topology row and a sharp-edge line: rdpgw pinned to a master commit, random keys, cert renewal restarts the bridge.
- The edge-ingress SKILL SAN list.

## 9. Rollback

| Stage | Undo |
|---|---|
| 1 | Revert the SAN line or the Traefik values, with a snapshot afterwards. |
| 2–4 | `git revert`, and Argo prunes. **Kill switch:** a commit setting `rdpgw-mac` to `replicas: 0`. A manual `kubectl scale` is reverted by self-heal within minutes, so it isn't one. Stopping rdpgw ends any tunnel and, with per-process keys, voids every outstanding token. The physical kill switch is KVM power. Removing Chris from the admin allowlist does **not** end an existing session cookie (168 h); rotating the admin cookie secret does (`docs/auth.md`). |
| 5 | Config-only retunes: revert the TOML. |
| 6 | commit-confirm auto-reverts; after confirming, revert the Ansible commit. |

## 10. Risks

| # | Risk | Mitigation | Residual |
|---|---|---|---|
| 1 | **PAA token reuse:** a bearer credential for about 6 min (`exp` + 60 s), usable for several tunnels (Leg C) | Bound to `mac-bridge…:3389`; `VerifyClientIp` with real client addresses (§5.3); NLA behind it; random keys die with the pod | Same-address replay inside 6 min, for example a colleague behind the same corporate NAT holding a leaked `.rdp`, still needs the NLA password. **`.rdp` files land in Downloads on a managed laptop, which DLP or OneDrive folder sync may copy.** Delete them after use. |
| 2 | **Corporate egress splits** the browser's `/connect` (through a secure web gateway) from Windows App's tunnel (direct), or inspects TLS | Found at stage 5 | Pinning refuses legitimate connections. The fallback is `VerifyClientIp: false`, accepting unpinned 6-min tokens (open question 7). If TLS inspection breaks the websocket upgrade, kvm-rdp's §14 fallbacks come back into play. |
| 3 | **A compromised KVM attacks the bridge pod** (hostile FLV, H.264, TLS, websocket) | kvm-rdp's hardened, fuzzed parsers; restricted pod: non-root, read-only root filesystem, no capabilities, seccomp, no service-account token, memory limit; the pod holds only its own secrets | A bridge RCE plus a container escape lands on a **control-plane node**. Before escape, flannel gives the pod every ClusterIP Service and, through SNAT, the LAN. That's why C is the recommended end state (§6.3). |
| 4 | **RDPGWSESSION bypass** (Leg C) | rdpgw on loopback behind the relay's auth check; cookie hidden at the relay and stripped at the edge; verified in stage 3 | None known in this topology |
| 5 | **Forged identity or address from inside the cluster** | The relay re-checks the admin session on `/connect` | An in-cluster caller *holding a token* can forge `X-Forwarded-For` on the tunnel path and defeat pinning, then still faces NLA (kvm-rdp §9.3: "in-cluster attacker: NLA only"). `pods/portforward` on ns `kvm` reaches rdpgw's loopback; that is admin-equivalent. |
| 6 | **Wildcard cert change** | A failure keeps the old cert; snapshot before and after | Every edge host gets a new key at once; CT publishes `*.kvm`. |
| 7 | **`externalTrafficPolicy: Local`** | Required anti-affinity first; snapshot | Edge-wide behaviour change (real client IPs reach backends). `.7` fails over by MetalLB re-announcement when a Traefik node dies. |
| 8 | **The admin tier is one Google account** | Dex plus the allowlist; NLA behind it | Compromising that account opens the gate, but not NLA. |
| 9 | **Bridge restart on cert renewal** | Planned and rare (kvm-rdp §4.4 restart-on-rotation) | Ends a live session at an arbitrary time, about every 60 days. Optional kvm-rdp change: defer the restart until no client is connected. |
| 10 | **rdpgw pinned to an unreleased master commit** | Our own build at a fixed SHA; stage-3 checks re-run on every bump | Security fixes need manual watching: there are no releases for Renovate to follow. |
| 11 | **Single replicas** (rdpgw, bridge) | Stateless; Recreate | A drain or restart drops the session. Accepted for a single-user tool. |

## 11. kvm-rdp §9.4: requirement by requirement

| Requirement | Met by | Status |
|---|---|---|
| Isolation with a distinguishable bridge identity | §6.3, option B or C | **Open.** Not met in Phase 1; option A only narrows. |
| TCP-connect and full-scan matrix from a non-bridge pod, a node shell and the tailnet | Stage 6 exit | With Phase 2 |
| Remove the KVM's Tailscale `/32` route | §6.4 | Open question 5 |
| Does the CNI enforce NetworkPolicy? If so, add policies | §6.2 | No (flannel), so none; the network layer of §9.1 doesn't count |
| Header trust local to the pod | §4.3 relay, rdpgw on `127.0.0.1`, `Header.TrustedProxies 127.0.0.1/32` | Met (relay instead of an oauth2-proxy sidecar, §3.3) |
| `tokenauth`; host fixed; only `/remoteDesktopGateway/` exempt; path-level routing | §4.3 config, §4.4 Exact Ingresses, everything else 404 | Met |
| Strip `Set-Cookie: RDPGWSESSION` at the edge | §4.4 middleware and relay | Met |
| Pin tokens: `VerifyClientIp`, `TrustedProxies` = the edge, the edge stamps XFF, same address for `/connect` and the tunnel | §4.3, §5.3 | Met **once Traefik is `Local`** (open question 4); stage 5 checks the same-address part |
| Websocket transport only | rdpgw `16cdaaf` | Acceptance item 2 (stage 5) |
| `Caps.EnableClipboard: true` | §4.3 | Met |
| `Client.Defaults` + `NoUsername`; `full address` resolves to the bridge from the rdpgw pod | §4.3 `defaults.rdp`, `hostAliases` → fixed ClusterIP | Met |
| Forged header, cookie and other-address checks | Stage 3, stage 4 | Planned |
| A certificate for that name, restart on rotation | §4.5 Certificate; the bridge's mtime poll; no `subPath` | Met |
| Memory limit | §4.2 | Met; resized after Plan E's soak |
| ServiceMonitor with `release=prometheus` | §7 | Met |
| Probes on `/livez` and `/readyz` | §4.2 | Met |

**What this spec asks of kvm-rdp in return:**

1. The certificate mtime poll must **follow symlinks** (`metadata`, not `symlink_metadata`). Kubernetes swaps a `..data` symlink when a Secret updates, and an `lstat` would never see the change.
2. Publish the image as `sha-<commit>` with a digest. It must run non-root on a read-only root filesystem, with no writable paths.
3. `/livez` and `/readyz` stay unauthenticated on the admin port.
4. Optionally, defer the restart on rotation until idle (risk 9).

## 12. Operations

- **Reconnect:** Windows App reconnects with the same `.rdp` within about 6 min. After that, open `/connect` again.
- **Rotate NLA:** edit `kvm-rdp-mac.secret.yaml`, then `rm kvm-rdp-mac.yaml && ./sign.sh`, commit, and update the password saved in Windows App.
- **Rotate the KVM password:** change it in the ES3 UI, then reseal the same way. Expect `kvm_auth_failed` (ErrorInfo 0x9) in between.
- **KVM factory reset:** the pin may change. The bridge then refuses with `kvm_cert_mismatch` (0x9, no login sent) until `kvm.spki_sha256` is updated in git.
- **rdpgw bump:** change the SHA in `images/rdpgw`, regenerate `go.sum`, re-pin the Deployment, re-run stage 3.
- **Power:** kvm-rdp §9.3 suggests powering the KVM off when it isn't in use. A Home Assistant-controlled plug on a working-hours schedule would do it. The bridge treats an absent KVM as `kvm_unreachable`. This is a follow-up, not in scope.

## 13. Decisions (2026-10-07)

Chris's answers, and the controller's rulings on the questions the repo's conventions settle:

1. **Bridge image source** (ruling): published by kvm-rdp's CI to ghcr — kvm-rdp's Plan E builds and signs the image; this repo pins it by digest like the other external images.
2. **Names** (ruling): namespace `kvm`, `-mac`-suffixed objects, `mac-bridge.kvm.chrismiller.xyz` as the bridge's RDP identity.
3. **Bridge certificate** (ruling): Let's Encrypt via cert-manager for the dedicated name, RSA 2048 (kvm-rdp §9.1 forbids the edge wildcard; self-signed would prompt in Windows App).
4. **Traefik `externalTrafficPolicy: Local` plus required anti-affinity** (Chris: yes, edge-wide): rdpgw pins each token to the client address that fetched it (`VerifyClientIp: true`, `Server.TrustedProxies` = Traefik's pod CIDR). Snapshot before; its own step in the rollout under the usual Argo sync.
5. **Tailnet `/32` to the KVM** (Chris: remove once live): drop the route after the Windows App acceptance run passes (§8); break-glass is then LAN only. The router change goes through `router/ansible/` under commit-confirm.
6. **Isolation end state** (Chris: defer): phase 1 ships on the flat LAN; §6.3's options stay documented for a later plan. §11's unmet §9.4 isolation items are accepted for v1.
7. **If corporate egress splits `/connect` from the tunnel** (follows from 4): find out at acceptance; if pinning fails there, set `VerifyClientIp: false` and record it — tokens stay ~6 min, single-use in practice, behind the admin tier and NLA.
8. **Cert-rotation restart** (ruling): kvm-rdp defers its restart until no client is connected (a small kvm-rdp change, carried to its Plan C/E).

## 14. Out of scope / later

- A second machine (copy the `-mac` set).
- A browser client path.
- The VNC or Pomerium fallback (kvm-rdp §14).
- RD Web feeds.
- Signed `.rdp` files.
- Router counter export and alerting.
- A KVM-side full-range colour setting (kvm-rdp §6.8).
- Moving rdpgw off a single replica: it would need shared keys and sticky routing.
