# Luma: a self-hosted household agent platform — design

**Status:** approved design (Chris, 2026-10-18), v2. It has been through a
builder check, four adversarial reviews (security, ops/capacity,
cost/caching, scope) and a consistency pass · **Date:** 2026-10-18 ·
**Repos:**
`github.com/ChristopherJMiller/luma` (product monorepo, public, Apache-2.0),
`github.com/ChristopherJMiller/ha-luma` (Home Assistant integration, public,
Apache-2.0), and this repo (site config and cluster wiring). Both new repos
exist and are cloned to `~/Repos/luma` and `~/Repos/ha-luma`.

This is the umbrella design. Each phase in §14 gets its own implementation
plan.

## 1. Intent

**What Chris asked for:**
- An in-house agentic assistant, **Luma** (named after the homelab). The aim
  is for Chris to control where his family's AI data lives and which tools
  can touch it, and to learn what it takes to build this experience at home.
- Build it **in layers**, as a full agent stack. Inference starts on the
  Anthropic API. Later, local inference boxes take over some of the work, so
  everything must stay generic and work with ≤20B-class models.
- **Cost efficiency is a first-class goal.** That means good caching, good
  model selection, and leaning on cheap models.
- **Per-user memory**, with Dex deciding who the user is.
- **Local MCP.** The homelab is opt-in. Home Assistant works both ways: HA's
  Assist is answered by Luma, and Luma can control HA.
- **A Nix sandbox** where the agent can set up a dev environment, with
  careful resource limits.
- **Artifacts** published and hosted on the fly, and shareable.
- **A generic way to expose an agent** (a "GPT" or subagent). The first is a
  Gramps agent for the family that answers questions and makes edits.
- **A good mobile experience**, plus **computer use** for web tasks.
- **Luma Core**: an AgentCore-style service is what gets exposed. It takes
  parameters, and it handles caching and sessions separately from any chat
  endpoint.

**North star.** "Claude.ai plus Claude Code on the web, for this household."
The data stays in galaxy. Every tool has a declared owner and scope. The model
behind any agent can be swapped.

**Anti-goals** (each would be a failure even if the system technically
works):
- A harness that wastes money silently, such as a cache-breaking prompt or
  hidden helper calls on expensive models.
- One shared "god agent" credential.
- Breaking the existing household: memory headroom, Ceph SMR, etcd nodes,
  logs leaving the house, existing apps.
- A Claude-only design.
- An unauthenticated control surface, or tokens in URLs or in browser
  storage.

**Done when** (detail per phase in §14):
1. Chris, then household members, then extended-family relatives use Luma
   from Android phones and laptops.
2. Dollars per person per day and cache-read share are on Grafana, and the
   SLO in §12.1 is met.
3. A relative can ask the Gramps agent a question and make an edit **as
   themselves**.
4. An HA Assist request that needs the model reaches Luma as the right
   person.
5. Any agent can be moved to a local model by changing site config
   (`models.yaml`, §11) without editing the agent's definition.

## 2. Starting state (2026-10-18)

**Cluster:**
- 3 Talos v1.10.9 / k8s v1.33.13 nodes, all control-plane, Ryzen 5700G,
  90.3 GiB allocatable in total.
- **Memory requests are 81.9 GiB (89–91% per node), with about 8.4 GiB of
  headroom.** Real use is 49–68%.
- **About 9.8 GiB of real use is BestEffort, with no request at all:**
  nfs-ganesha 4.7 GiB, both MDS, HA, Grafana, trivy and MetalLB. If memory
  runs out, the OOM killer picks these first.
- Rook accounts for 53.3 GiB of requests (mons request 4 Gi each and use
  about 0.5 Gi).
- **A single drain leaves about 21.7 GiB of requests Pending.** Two nodes
  can't host three nodes' requests.
- **GPUs.**
  - The 5700G iGPUs are **dead**. They fail VBIOS discovery ("Unable to
    locate a BIOS ROM"), a known ASRock Rack AM4 issue with no fix.
  - **`middle` has a discrete Polaris card** (`1002:67df`, RX 470/570/580
    class, 8 GiB VRAM). The driver is bound, `renderD128` exists, and
    `amd.com/gpu: 1` is allocatable and unclaimed. It has no ROCm (gfx803),
    but **Vulkan works** via Mesa RADV in the container.
  - Its init failed intermittently on earlier boots (July, Talos 1.9.6), so
    it may vanish after a reboot.
  - Plex's `mega-media-release.yaml` "revert when amd.com/gpu is back"
    comment is now actionable. That is separate work.
  - `middle` is also the memtest suspect (§2, Storage), so anything placed on
    it waits for that verdict.
- flannel **does not enforce NetworkPolicy**.

**Storage:**
- `rook-ceph-block` is SMR HDD and collapses when sustained writes exceed
  25 GiB.
- `rook-ceph-block-ssd` has **142 GiB MAX AVAIL**, shared with every Postgres
  cluster and the CephFS metadata pool. Every write lands on each node's
  single SATA SSD (3× replication).
- All StorageClasses are Retain.
- The system NVMes are worn (#2661).
- **`middle` is the memtest suspect.** There have been three bad replicas on
  middle's two HDDs since 10-11 (PGs 1.16, 1.b and 1.c), all non-primary,
  with SMART clean. That points at RAM, DMA or the SATA controller.
  - PG 1.c repair started 2026-10-18 with Chris's approval. Verify it ends
    clean.
  - Memtest86+ on middle is pending, in a maintenance window.
  - Anything placed on middle (the Polaris card, for example) waits for the
    verdict.

**Observability and logs:**
- Prometheus loads only rules in namespace `prometheus` that carry
  `prometheus=kube-prometheus, role=alert-rules`. Only 3 rule groups load,
  and no stock alerts are active.
- ServiceMonitors need `release=prometheus`.
- **fluent-bit ships every container's logs to Azure Log Analytics** and to
  router syslog. There is no Loki.

**Identity:**
- Dex v2.44: Google, plus Microsoft with `tenant: common`.
- The Microsoft connector takes email from **`userPrincipalName`**, not the
  settable `mail` attribute. So email is a sound cross-connector key **as long
  as we never use the generic OIDC connector against Entra.**
- No groups. `skipApprovalScreen: true`. No `expiry` block, so tokens live
  24 h.
- oauth2-proxy admin and family tiers, with cookies on `.chrismiller.xyz`,
  SameSite=Lax, HttpOnly.

**MCP:**
- hyprmcp/mcp-gateway 0.4.0 (archived upstream) plus `mcp-jwt-auth`, with
  open DCR via Dex gRPC. Only `/gramps/mcp` exists. **Chris is its only user.**

**Gramps:**
- Our patched `grampsweb`. `gramps-mcp` uses one shared editor account and
  **does no auth itself**.

**Home Assistant:**
- 2026.9.4, with `mcp_server` on (HA OAuth, Chris's account).
- No LLM agent, no STT/TTS. TTS is `google_translate` only.
- Only Chris has an HA user.

**Other:**
- Attic: 50 Gi HDD PVC; `allowed-hosts` lists only the public name.
- B2: the cluster keys can't delete; lifecycle rules can.
- Anthropic: an **Organization** with $200/month of API credit, **all of it
  Luma's**.
- Users: Chris only for now. Nobody under 18.

**Live security problems** (phase 0a fixes them first):
1. **One-click token theft.** Anyone can register a client through open DCR
   with their own `redirect_uri`. Dex then skips consent, so a crafted
   authorize link signs the victim in silently and hands their code to the
   attacker. `mcp-jwt-auth` doesn't check audience, so the stolen token opens
   the tree.
2. Dex tokens last 24 h and can't be revoked.
3. `gramps-mcp` does no auth itself, so any pod can read and write the tree.
4. The 401 has no `resource_metadata`, and the protected-resource metadata
   and the AS issuer disagree.

## 3. Decisions

"Chris" means Chris decided it in the 2026-10-18 brainstorm. "My call" means
the designer settled it and it is open to Chris's review. Decisions changed
by the reviews are marked *(rev)*.

| # | Decision | Who |
|---|---|---|
| D1 | Rollout goes Chris → household → extended family. Mostly Android. Reachable anywhere behind SSO, at `luma.chrismiller.xyz`. | Chris |
| D2 | First slice is everyday chat with memory and tools. | Chris |
| D3 | Anthropic API, $200/mo, Organization, **all of it Luma's**. Nothing is off-limits to Anthropic. Generic for local models. | Chris |
| D4 | **Luma Core**, an AgentCore-style session API, is built. Every chat surface is a client of it. | Chris |
| D5 | Go core. TypeScript runtime (the conductor) wrapping the Claude Agent SDK. | Chris |
| D6 | The **Claude Agent SDK** is the harness for Claude models. | Chris |
| D7 | Our own PWA, built on **assistant-ui**. No LibreChat or Open WebUI. | Chris |
| D8 | Memory is automatic and user-editable, per person. Incognito skips it. | Chris |
| D9 | Default MCP servers act **as the user**. Personal MCP comes from a vetted catalog with the user's own OAuth or credentials. Fastmail is first. | Chris |
| D10 | Family tools are Gramps, sandbox + artifacts, and web. HA and homelab tools in the app are admin-only. Homelab is read + act, with every action approved. | Chris |
| D11 | Family control the house **through HA Assist only**, as their own HA user, limited to exposed entities. *(rev)* Household members only (D29). | Chris |
| D12 | Agents are defined by admins in git. | Chris |
| D13 | Sandbox = plain k8s pods with Nix, run by an existing operator (agent-sandbox). No gVisor or Kata. NetworkPolicies everywhere. Profiles `slow` (HDD) and `fast` (SSD) with ephemeral volumes. *(rev)* **`slow` ships only once a write throttle is proven.** | Chris |
| D14 | Sandbox egress is open web, **never home**. | Chris |
| D15 | Workspaces can be restored for 7 days. | Chris |
| D16 | Computer use: web tasks, watch and take over. *(rev)* Built on demand. | Chris |
| D17 | Artifacts are private, family or public, at **`share.luma.chrismiller.xyz`**. This was kept after review, with mandatory rules (§12.3). | Chris |
| D18 | HA Assist conversations are answered by Luma itself, mapped to the person. | Chris |
| D19 | Proactive and scheduled agents come later; the door stays open. | Chris |
| D20 | Approvals: in-app, plus HA Companion push. Unanswered means denied. *(rev)* HA push for household members; Web Push for everyone. The approver's identity is verified (§10). | Chris |
| D21 | Relatives may edit the tree, and delete with approval, through the agent. | Chris |
| D22 | One global budget, no per-person caps. | Chris |
| D23 | The GPU box arrives within about 6 months, probably as a Talos worker; it may be a satellite if it's a unified-memory box. | Chris |
| D24 | Live auth fixes come first. The Gramps endpoint stays online (Chris uses it) and is patched. Reconnects are fine. | Chris |
| D25 | NetworkPolicy enforcement goes through GitOps, with the dormant policies validated first. | Chris |
| D26 | Headroom comes from right-sizing existing requests. | Chris |
| D27 | Repos: `luma` + `ha-luma`, public. | Chris |
| D28 | **Lean phase order** (§14): 0a (0b in parallel) → lean chat → Gramps (household, then first relatives) → HA Assist → sandbox + artifacts → mail → local. Homelab tools and computer use are built on demand. | Chris |
| D29 | **Two family tiers.** The registry flags `household: true`. Household memory, HA Assist control and HA push are household-only. Extended family get chat, Gramps and sandbox. | Chris |
| D30 | Evals and cache tests run **in-cluster only**, through the gateway, on a `luma-ci` workspace with a hard limit. No Anthropic key in GitHub. | Chris |
| D31 | Voice: Chris asked to use the nodes' GPUs. *(rev, research)* The iGPUs are dead, so voice starts **CPU-only**: Parakeet via `rhasspy/wyoming-whisper --stt-library sherpa` plus streaming Piper, on `bottom`. **middle's Polaris card** (whisper.cpp or parakeet.cpp on Vulkan) is the step up if CPU misses the latency target. | Chris + research |
| D32 | Core's API is shaped like Anthropic's **Managed Agents** API. | My call |
| D33 | The inference gateway is **agentgateway** (standalone, pinned), not LiteLLM. | My call |
| D34 | The MCP edge is **`mcp-edge`**, our Go service grown from `mcp-jwt-auth`. ToolHive is the point to reassess. | My call |
| D35 | Dex stays, upgraded to v2.46. A token-exchange module (**luma-auth**) lives **inside Core** *(rev)*, alongside a git **people registry**. | My call |
| D36 | *(rev)* Core and the conductor talk over **an internal protocol** of AG-UI-shaped events, not ACP. An ACP adapter comes in phase 6 for the second harness. | My call |
| D37 | The second harness for local models is the **OpenHands agent-server**, with Goose as fallback, settled by a bake-off. | My call |
| D38 | *(rev)* Each session's tool set is fixed when it is created. Adding a workspace to a conversation means forking it with a summary. | My call |
| D39 | *(rev)* The PWA authenticates through a **backend-for-frontend in Core**: Core is a confidential Dex client and sets an HttpOnly session cookie. No tokens in the browser. | My call |
| D40 | Claude agents use the SDK's WebSearch/WebFetch, with per-turn caps. SearXNG is added if search fees pass $15/mo, or with local models. | My call |

## 4. Architecture

```
  Luma PWA (assistant-ui) ── HttpOnly cookie (BFF) ─┐        ha-luma (HA conversation) ── HA pod SA token ─┐
                                                    ▼                                                     ▼
              ┌──────────────────────────────── Luma Core (Go) ───────────────────────────────────┐
              │ BFF/OIDC client · sessions · append-only event log (replay-from-id) · conv. tree   │
              │ memory · approvals · connections vault (enc., SealedSecret key) · artifacts index  │
              │ agents + models.yaml (from git) · model pinning · cost ledger · maintenance mode   │
              │ luma-auth module: /auth/token (RFC 8693) /auth/jwks · registry · TokenReview        │
              └───────────────┬──────────────────────────────────────────────┬────────────────────┘
                              │ internal protocol (AG-UI-shaped events, WS)   │ per-call capabilities
                              ▼                                              ▼
              ┌──── conductor (TS) ─────────────────────────────┐
              │ Agent SDK host per open conversation             │
              │ phase 1–3: Claude Code CLI as a local subprocess  │──► MCP proxies (no stored creds;
              │ phase 4+: spawnClaudeCodeProcess → sandbox pods   │     ask Core per call)
              └───────┬───────────────────────────────┬──────────┘
                      │ ANTHROPIC_BASE_URL             │ remote spawn (phase 4+)
                      ▼                                ▼
          agentgateway (2 replicas) ──► Anthropic    per-person sandbox (agent-sandbox, fast|slow)
          (real keys per workspace live only here)    /work/c/<conv> on ephemeral RBD volume
                      └──► local box (phase 6)

  MCP servers (each validates its own audience): gramps-mcp (as user) · HA /api/mcp(/assist) · Fastmail (remote)
  on demand: homelab-read · homelab-act      Egress: smokescreen (+ Blocky lists, phase 4) · kube-network-policies (phase 4)
```

| AgentCore part | Luma piece | Adopt / build |
|---|---|---|
| Runtime | conductor subprocesses (phases 1–3), agent-sandbox pods (phase 4+); a second harness in phase 6 | adopt + thin build |
| Session/control plane, identity exchange | Luma Core (with the luma-auth module) | **build** |
| Inference | agentgateway | adopt |
| Tool gateway | mcp-edge + MCP servers | build small + adopt |
| Memory | Core memory store + tools | build small |
| Code interpreter / browser | sandbox pod / browser pod (on demand) | adopt + images |
| Observability | agentgateway metrics, Core metrics and cost ledger | adopt + build small |

## 5. Luma Core (Go, `luma/core/`)

### 5.1 API (shaped like Managed Agents)

| Resource | Operations |
|---|---|
| `agents` | List and get. Read-only; loaded from git (§11). |
| `sessions` | Create (`agent`, `params`, `source`), get, list (titles; full-text search later), fork (`at_message`), archive, delete. |
| `sessions/{id}/events` | **Send:** `user.message` (text plus attachment ids), `user.approval`, `user.interrupt`. **Stream (WS):** text delta, tool start/end (name and a redacted summary), approval requested/resolved/cancelled, artifact published, model switched, status (`running`, `idle`, `requires_action`, `error`), usage. **Every event has a monotonically increasing id.** Clients reconnect with `since=<id>` and Core replays. **A turn runs to completion server-side whether or not a client is connected.** |
| `attachments` | Upload an image, PDF or text file: size cap 20 MB, images downscaled to ≤1568 px on the long edge, stored in `acid-luma`. They are passed to the model as content blocks. |
| `memory` | Get, put and delete the person's profile (phase 1), plus household notes (phase 2, household only). |
| `connections` | Per-user OAuth "connect" flows for catalog servers (start, callback, list, revoke). |
| `artifacts` | List mine, set visibility (**human UI only**), revoke, rotate the public link. |
| *(deferred)* | Adapters: `/v1/chat/completions` (OpenAI-compatible) and `/mcp` (agents as tools). They are built when a named consumer exists; the API shape keeps them thin. |

**`params`** are declared by the agent and validated by Core:
- `tier`: `fast`, `smart` or `deep`. These map through `models.yaml` (§11).
  `deep` is admin only.
- `effort`
- `incognito`
- `workspace`: `none`, `fast` or `slow`. It is fixed for the session's
  lifetime (D38).

**Auth.**
- **PWA (BFF):** Core runs the Dex auth-code flow as a confidential client.
  It keeps the Dex tokens server-side and sets a **host-only, HttpOnly,
  Secure, SameSite=Lax** session cookie on `luma.chrismiller.xyz`. The WS
  checks the cookie and an `Origin` allowlist. This keeps tokens out of the
  browser and gives Core the user's Dex identity for delegation (§8).
- **ha-luma:** HA's projected ServiceAccount token, checked by TokenReview.
- Every caller is mapped to a **person id** through the registry.

### 5.2 Rules

1. **Model pinning.** A session's model is chosen once, from `tier` through
   `models.yaml`.
   - Escalation, cheapest first:
     1. a higher per-message effort on the same session (verify that it is
        cache-preserving through the gateway);
     2. the `deep-think` subagent, which has a fresh prefix and returns one
        result;
     3. an explicit "continue in Smart" fork. The UI shows its cache-write
        cost.
   - **Classifier fallback.** Claude Code can move a session to another model
     after a flagged request. Core records the model per message, emits
     `model switched`, and accepts it (my call), with no silent retry.
2. **Append-only.** An edit or regenerate becomes a fork (`forkSession` /
   `resumeSessionAt`). Core never edits a stored turn.
3. **Transcripts.** Use the SDK `SessionStore` on Postgres. Pin
   `CLAUDE_CODE_PROJECT_DIR_NAME` per conversation, because the store keys by
   `cwd`. Core keeps the conversation tree and a title list. Never use
   `listSessions()`.
4. **Credentials stay in Core.**
   - The conductor holds no user tokens.
   - For each credentialed tool call it asks Core for a **single-call
     capability** bound to {session, tool, person}. Core checks that the
     session belongs to that person, then mints the downstream token.
   - Compromising the conductor therefore gives only the currently open
     sessions' tool calls, not standing access as every user.
5. **Cost ledger.**
   - Dollars come from the SDK result messages' `modelUsage` /
     `total_cost_usd`. These include web-search fees, which the gateway
     can't see.
   - A nightly Usage API job (`group_by=api_key_id`) reconciles them.
   - Warn in the UI and on Grafana when the **global** cap reaches 80%.
6. **Maintenance mode.** Core stops new sessions and claims, snapshots and
   releases sandboxes, and closes CLIs. **Luma is expected to be down during
   every node drain.** The talos-upgrade and node-maintenance procedures call
   this first.
7. **Retention** (disclosed in the UI):
   - **Deleting** a conversation removes it from live storage immediately.
     Copies remain in backups until they age out, about 31 days at most:
     - nightly dumps are kept 30 days;
     - WAL-G base backups and WAL are kept 30 days or less;
     - workspace tarballs last about 8 days (hidden at 7, deleted a day
       later).
   - **Incognito** reads and writes no memory, and its transcript is deleted
     24 h after the session goes idle (the same backup caveat applies).
8. **Storage: `acid-luma`.** A Zalando cluster on `rook-ceph-block-ssd`,
   created in phase 1.
   - **`synchronous_commit=on`**: this is the SSD pool, and approvals and
     memory writes must not be lost.
   - **WAL-G archiving to B2 for point-in-time recovery**, in addition to the
     nightly logical dump.
   - A **restore drill** (into `acid-drill`) is in phase 1's exit criteria.
   - The connections-vault key and Core's signing keys are SealedSecrets in
     git.
   - Consumed approval `jti`s are stored here too.
9. **Metrics.** Labels are `person`, `agent`, `tier` and `source`. Session
   ids **never** become labels (unbounded series, and Prometheus sits on
   SMR). There is no OTel backend in the cluster, so traces are deferred.

## 6. Conductor (TypeScript, `luma/conductor/`)

**What it does.** The conductor hosts the Agent SDK: one SDK host per **open**
conversation.
- **Phases 1–3:** the Claude Code CLI runs as a **local subprocess** in the
  conductor pod. These agents have **no shell or file tools**, so nothing
  model-chosen executes there.
- **Phase 4+:** sessions with a workspace use `spawnClaudeCodeProcess` to run
  the CLI inside the person's sandbox pod.

**Pinned SDK configuration.** Everything is set explicitly:
- **Permissions:**
  - `permissionMode: "default"` (an omitted value can start in auto mode);
  - `settingSources: []`;
  - `strictMcpConfig: true`;
  - an explicit `tools` list (`allowedTools` only auto-approves).
- **System prompt:** a **fully static** `systemPrompt` per agent version.
  The per-person memory snapshot is **not** in the system prompt, because
  `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` is ignored behind a gateway.
  - **Primary:** the snapshot is fenced context delivered through a
    SessionStart hook's `additionalContext` (§12.2). It re-fires after
    compaction; verify this on the pinned SDK.
  - **Fallback:** a fenced block opening the first user turn, which Core
    re-injects after compaction.
- **Environment:** `env` replaces the subprocess env and is built
  explicitly:
  - `CLAUDE_CONFIG_DIR` per person;
  - `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`;
  - `DISABLE_AUTOUPDATER`, `DISABLE_TELEMETRY`,
    `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`;
  - `CLAUDE_AGENT_SDK_CLIENT_APP=luma`;
  - `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1`, which adds
    `x-claude-code-request-class` for metrics.
- **Helper models:** `ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-5-5`.
  Without it, WebFetch extraction and other helper calls run on the **main**
  model when `ANTHROPIC_AUTH_TOKEN` is set.
- **Cache TTL:** set `CLAUDE_CODE_PROMPT_CACHE_TTL` per agent. Use **1h** for
  interactive chat; **5m** for Assist bursts, sandbox loops, subagents and
  compaction. Verify that the gateway forwards the `extended-cache-ttl` beta,
  using `usage.cache_creation.ephemeral_1h_input_tokens`.
- **Compaction:** `CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000` and
  `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE≈85` on Haiku-tier chat, which keeps
  requests under the 5× price step above 100K. Set `precomputeCompaction`
  off. Sandbox agents may run past 100K, because mid-task compaction can cost
  more than the price step.
- **Effort:** `low` for chat and Assist. With low effort, the prompt includes
  today's date (as appended context) and guidance on when to search, because
  Haiku under-searches at low effort. `medium` for Gramps. Explicit effort on
  subagents (Sonnet 5.5 defaults to high).
- **Web tools:** a PreToolUse counter caps WebSearch at **3 per turn** and
  **15 per session** (the CLI allows `max_uses: 8` per call). WebFetch goes
  through smokescreen (`HTTPS_PROXY`). Its domain check calls
  api.anthropic.com, which the proxy allows.
- **CLI pinning:** CLI ≥ 2.1.268, because older flag-less sessions rebuild
  the prompt per request. The SDK and CLI are pinned exactly. Renovate groups
  bumps **weekly** with a 3-day minimum release age, behind the eval gate
  (§13.4). The CLI goes in its own small final image layer, to limit churn on
  the worn NVMes.

**SDK issues to design around** (re-check on each pin):
- **#348:** keep the input iterable open for the whole session.
- **#391:** the libc probe. Use one long-lived query per conversation, and
  `pathToClaudeCodeExecutable`.
- **#268:** never call `listSessions`.
- **#370:** PreToolUse `defer` cannot resume in-process `createSdkMcpServer`
  tools. So our MCP proxies run as a **local HTTP MCP server in the conductor
  pod**, not in-process.
- **#289:** resume drops tool sequences in large sessions. The eval gate
  includes a large-session resume test.
- **#432:** never put `cache_control` in streamed user messages.
- **#378:** a stream can outlive its bearer token, and `apiKeyHelper` is
  only consulted between requests. **Gateway-audience tokens get their own
  60-minute TTL**, longer than the longest turn. luma-auth's 5–10 minute TTL
  applies to tool and downstream tokens.

**MCP:** hold the first prompt until init reports every MCP server
connected. A late connection changes `tools`, which breaks the cache and
thinking.

**Process lifecycle:**
- Evict a CLI that has been **idle for 10 minutes**. A resume from the
  SessionStore costs nothing extra while the 1h cache is still warm at
  Anthropic.
- Cap concurrent CLIs per conductor. The phase 1 cap is **4** (Chris only),
  6 in phase 2, raised as headroom allows. Size the pod request per CLI:
  about 400 MiB each, measured in phase 1.
- If the client stream drops, the turn finishes and then the CLI idles out.
  An **orphaned** CLI, one whose Core link is lost, is killed so it can't keep
  spending.
- There is one conductor replica. If it restarts, open turns fail with a
  visible error and resume from the store. This is accepted.

**Protocol to Core (D36).** A small internal protocol over WS carrying the
§5.1 event shapes, plus control messages (start, resume, fork, interrupt,
approval resolution, capability request). Phase 6 adds an ACP adapter for the
second harness. ACP's remote transport is still a draft (Transports WG,
2026-04).

## 7. Sandboxes (phase 4)

### 7.1 Execution targets

| Target | Used by | Tools | Lifecycle |
|---|---|---|---|
| Conductor-local CLI | Chat, the Gramps agent, the house agent | MCP (via proxies), web tools, memory tools. **No shell or files.** | One CLI per open conversation, idle-evicted. |
| **Per-person sandbox** | Sessions created with `workspace: fast\|slow` | Bash, files, Nix, MCP | Claimed from a warm pool on first use, one per person. One CLI per conversation, `cwd /work/c/<conv>`. Released after 15 minutes idle. **One active CLI per sandbox by default.** |

### 7.2 Operator

- **kubernetes-sigs/agent-sandbox**, pinned. v1.0.7 or later is required for
  sandboxd bearer auth.
  - Use SandboxTemplate, SandboxWarmPool and SandboxClaim, with sandboxd as
    the image entrypoint.
  - **Do not deploy** its router (allow-all authorizer) or its MCP server
    (no auth, and the model chooses namespace and labels).
  - Core is the only client. `networkPolicyManagement: Unmanaged`.
  - Smoke-test on kind 1.33. Add agent-sandbox to the talos-upgrade preflight
    for the k8s 1.34–1.36 hops, because `v1beta1` → `v1` needs every sandbox
    released.
- **Node loss.** Core abandons the claim and restores the last snapshot into
  a new claim name; a reused pod name would collide with the stuck ephemeral
  PVC. Alert on Released or Failed PVs in the `luma-ephemeral-*` classes.

### 7.3 Pod shape and profiles

- **Namespaces:** `luma-sandbox` (and `luma-browser` later), PSS
  `restricted`.
- **Pod security:**
  - `automountServiceAccountToken: false`, `enableServiceLinks: false`;
  - non-root 1000, RuntimeDefault seccomp, no privilege escalation, drop all
    capabilities.
- **PriorityClasses:** Luma control plane −5. **`luma-sandbox` −10**, with
  `preemptionPolicy: Never`.
- **Resources:**
  - requests 250m / 768Mi;
  - limits **1 CPU** / **4Gi**, because one CLI plus a Nix evaluation plus
    pandas needs it;
  - ephemeral-storage 2 Gi for the container layer.
- **ResourceQuota in `luma-sandbox`:**
  - `limits.memory` **8 Gi** in total. Limits are what can OOM the BestEffort
    household pods.
  - **Concurrency target: 2 sandbox pods**, warm pool of 1 included. So: pod
    count 2, `limits.cpu` 2, and `fast` storage 10 Gi.
  - Storage per StorageClass.
  - No LoadBalancer or NodePort Services.
- **Node affinity:** preferred anti-affinity away from `middle` (thermals,
  and its GPU and Ceph history). When the GPU worker joins (D23), make that
  affinity **required** before any extended-family member gets sandboxes.
- **Profiles (Chris, D13), as generic ephemeral volumes at `/work`:**

  | Profile | StorageClass (new, **`reclaimPolicy: Delete`**, `discard`) | Caps | Ships |
  |---|---|---|---|
  | `fast` | `luma-ephemeral-ssd` (the SSD pool) | 5 Gi per volume; 10 Gi namespace quota | phase 4 |
  | `slow` | `luma-ephemeral-hdd` (the HDD pool) | 10 Gi per volume | **Only after a spike proves a working write throttle.** krbd ignores librbd QoS, and k8s has no per-volume I/O limits. Candidates: ceph-csi cgroup `io.max`, or a blkio limit on the pod. Until then, `slow` is disabled. |

- **Ceph backstop:** Core refuses new claims, and stops write-heavy
  sandboxes, while OSD commit latency stays above 500 ms. Disk-only size
  caps don't bound how much a sandbox rewrites.

### 7.4 Nix

- **Image:** nix2container from the `luma` flake, with `initializeNixDatabase`,
  `nixUid`/`nixGid` 1000, and single-user daemonless Nix. It contains:
  - nix, bash, coreutils, git, cacert, curl, zstd, sandboxd;
  - python3 with pandas, matplotlib and openpyxl;
  - nodejs;
  - a pinned nixpkgs registry entry.
- **`nix.conf`:**
  - `sandbox = false` and an empty `build-users-group`. Talos keeps user
    namespaces off.
  - `max-jobs = 1`, `cores = 1`.
  - Substituters: in-cluster Attic `luma-envs`, then cache.nixos.org via the
    proxy.
  - `connect-timeout = 5`, `fallback = true`, so an Attic outage doesn't hang
    sessions.
- **Environment as code:** `/work/.luma/flake.nix` plus its lock file. `/nix`
  is never snapshotted.
- **Where new store paths land** is the builder's call (§18), under a rule
  that node-NVMe writes stay minimal. One option is a small SSD ephemeral
  volume for the writable store.
- **Pre-built environments (later):** CI builds `envs/*` and pushes them with
  a CI-only token. **Pods are pull-only and sandboxes never push.**
- **Attic changes:** add the in-cluster Service host to `allowed-hosts`, and
  plan for PVC growth.

### 7.5 Workspace persistence

- **When:** on idle, periodically while dirty, and on release.
- **How:** Core streams `tar --zstd` of `/work/c/<conv>` from sandboxd (never
  buffered in memory) to `luma-workspaces/ttl-7d/<person>/<conv>/<ts>.tar.zst`.
- **Expiry:** the B2 lifecycle hides objects after 7 days and deletes them a
  day later (D15). No Object Lock. B2 credentials stay in Core.
- **Restore:** claim a pod, PUT the tarball, extract, and re-realise the
  flake.
- **Termination:** `terminationGracePeriodSeconds` leaves room for a final
  snapshot. The hard `shutdownTime` backstop exists. Maintenance mode
  (§5.2.6) snapshots before drains.

### 7.6 Computer use (on demand)

This design is kept for when Chris wants it.
- **Browser pod** in `luma-browser`: Xvnc, openbox, and Chromium with
  `--proxy-server=<smokescreen-web>`, an ephemeral profile, CDP on loopback,
  and a 1 Gi `/dev/shm`. Plus an `@playwright/mcp --cdp-endpoint` sidecar,
  using accessibility snapshots first and vision as the fallback.
- **Takeover:** noVNC at `browser.luma.chrismiller.xyz`, behind the admin
  tier, WS-proxied by Core.
- **Spend:** its own Anthropic workspace with the smallest limit.
- **Accepted risk:** Chromium runs without its own sandbox, so the pod is the
  boundary and nothing private is in it.

## 8. Identity and authorization

### 8.1 Dex (upgraded in phase 0a)

- **v2.46.0:** sessions and remembered consent, per-client
  `allowedConnectors`, `jti`, security fixes.
- **Expiry block:** about 1 h for ID and access tokens, with rotating refresh
  tokens and an idle limit.
  - Test oauth2-proxy, Immich and Gramps refresh first; hourly logins are a
    regression.
  - Short expiry is **not revocation**. Revocation comes from the short TTL
    plus refresh-token revocation.
- **Consent:** dynamically registered clients **must** show a consent screen
  that names the redirect host. Static first-party clients may skip it. Dex
  can't scope the skip per client, so phase 0a chooses one of:
  - a small Dex patch;
  - a consent page in the current facade (hyprmcp's `/oauth/authorize`
    handler, or a minimal mcp-edge pulled into 0a);
  - turning the skip off globally, with remembered consent.
- **New static client:** `luma-core`, confidential (the BFF).
- **Microsoft connector:** stays on `userPrincipalName`. **Never switch to the
  generic OIDC connector against Entra `common`** (nOAuth). The registry pins
  which connector each email may arrive through.
- **Never use Dex as an STS.** Its OIDC-connector token exchange skips the
  client check.

### 8.2 People registry

The registry is a SealedSecret in this repo:

```yaml
people:
  chris: {tier: admin,  household: true,  emails: {google: [...], microsoft: [...]},
          ha_user_id: ..., gramps_user: ...}
  <id>:  {tier: family, household: false, emails: {google: [...]}, gramps_user: ...}
house:   {ha_user_id: luma-house, household: true}   # voice satellites / unknown speaker
```

- Pre-commit generates `emails-admin` and `emails-family`, the Gramps
  new-user list, the mcp-edge allowlist, and Core's registry. This extends
  `scripts/mirror-family-list.sh`.
- During the Chris-only pilot, the registry holds Chris only. The live
  oauth2-proxy lists are switched to the generator only when the first
  relative joins (phase 2).
- Memory, approvals, artifacts and budgets key on **person id**.

### 8.3 luma-auth (a module inside Core, phase 2)

- **Endpoints:** `/auth/token` (RFC 8693 exchange only) and `/auth/jwks`,
  ES256. No login UI, no refresh tokens, no DCR.
- **Inputs:**
  - Core's own session state (for user context);
  - Kubernetes **TokenReview** for workload tokens (projected,
    audience-scoped, pod-bound). Not offline JWKS: the cluster's `jwks_uri`
    is node-specific;
  - the registry;
  - an actor-policy YAML.
- **Output claims:** `sub` (person), `tier`, `household`, `src`, `aud`,
  `act`, `sid` and `jti`, valid 5–10 minutes. Gateway-audience tokens are
  the exception, at 60 minutes (§6).
- **Every exchange is bound to a live `sid`** that belongs to that `sub`.
  The one exception is `src=hosted-mcp`: its subject is a Dex token
  presented via mcp-edge (signature, audience and allowlist checked), and it
  is bound to that token's `jti` and expiry. Dex JWKS is an input for this
  case only.
- **`src` values:** `web`, `ha`, `hosted-mcp`, `schedule` and `sandbox`.
  Policy is decided on **tier, household and src** together. Examples:
  - `src=ha` never gets personal-catalog tools, or person-scoped memory from
    a voice satellite;
  - `src=hosted-mcp` never gets HA or homelab.
- **Availability:** JWKS is cached for at least 1 h by agentgateway, mcp-edge
  and the MCP servers. Core being down already means Luma is down.

### 8.4 Token flows

| Flow | Hops |
|---|---|
| Family member in the PWA → Gramps | Cookie → Core (person) → conductor asks for a capability → Core mints `aud=gramps-mcp`, `sub=person`, `sid`, 5 min → gramps-mcp validates → luma-auth exchange (actor = gramps-mcp SA, `aud=gramps-api`) → Gramps `/api/token/exchange/` → Gramps JWT (15 min; the user's role ∩ `mcp` scope) |
| Chris's Claude Code / Claude.ai → Gramps MCP | Dex via mcp-edge (DCR with **exact** redirect URIs from an allowlist; consent shown; CIMD later) → mcp-edge (signature, **aud** ∈ facade clients, allowlist, policy) → gramps-mcp → the same exchange, `src=hosted-mcp` |
| Approval | §10 |
| HA Assist (phone) → Luma → HA | ha-luma sends the HA SA token, `context.user_id` and `satellite_id` → Core (`src=ha`; person if it's an authenticated Companion user, otherwise `house`) → `/assist`. ha-luma mints a **5-minute HA token for the speaker** and passes it out of band. Luma uses it only on `/api/mcp/assist`, and it never enters model context. |
| Sandbox → artifact publish | Through the `artifacts.publish` tool via the conductor. Core pulls files from sandboxd by session; the owner is the session's person. |
| Opening an artifact | §12.3 |

## 9. MCP layer

### 9.1 mcp-edge (Go, `luma/mcp-edge/`)

- **Origin:** grows out of `images/mcp-jwt-auth`, and lifts hyprmcp's
  MIT-licensed handlers.
- **Metadata:** consistent PRM and AS metadata. The 401 carries
  `resource_metadata`.
- **Registration:** DCR via Dex gRPC, with **exact redirect URIs** from an
  allowlist (claude.ai, chatgpt.com and Gemini callback URLs). Loopback only
  if Chris needs it for Claude Code. Rate-limited.
- **Authorization:**
  - **Audience validation:** `aud` or `azp` must be a facade-created client.
  - **In 0a this check lives in `mcp-jwt-auth`.** It verifies the Dex JWKS
    signature and requires `aud` or `azp` to be a facade-created client
    (existing ids grandfathered; new ones prefixed `mcp-`).
  - If Dex access tokens carry no usable `aud`, a **minimal mcp-edge** (DCR
    facade plus re-minted audience-bound tokens for `/gramps/mcp`) is pulled
    into 0a. Tokens without a verifiable audience are rejected; they never
    fall back to the allowlist alone.
  - The allowlist comes from the registry.
  - Per-tool policy in git (tool → tier, household, src, approval).
  - It uses the `Mcp-Method`/`Mcp-Name` headers (2026-07-28 spec).
- **Audit and metrics:** an audit log, and
  `mcp_requests_total{server,tool,person,decision}`.
- **Phased:** 0a can be patches to `mcp-jwt-auth` (audience and allowlist)
  plus the DCR allowlist. mcp-edge v1 replaces hyprmcp in phase 2.
- **Reassess ToolHive** at 1.0, or if hosted clients need many servers.

### 9.2 Servers

| Server | Phase | Auth | Who |
|---|---|---|---|
| **gramps-mcp** (patched, §9.3) | 0a (self-auth), 2 (as user) | Validates its own token; acts as the user | Family (default agent), Chris's hosted clients |
| **memory** tools | 1 | Session identity, through the conductor's proxy server | Every non-incognito agent |
| **artifacts** | 4 | Session identity | Agents allowed to publish |
| **HA** `/api/mcp` (admin chat), `/api/mcp/assist` (Assist path) | 3 | Chat: Chris's HA OAuth connection (CIMD; verify 2026.10). Assist: the speaker's 5-minute token. Locks and notify are never exposed. | Chat: admin. Assist: household (D11, D29). |
| **Fastmail** (catalog) | 5 | Per-user OAuth in Core's vault. The consent tier is **read + draft**; sending requires approval. | Personal opt-in; the mail agent has **no web tools** |
| homelab-read (`kubernetes-mcp-server` read-only, `mcp-for-argocd` read-only, `mcp-grafana --disable-write`) | on demand | Each in read-only mode, least-privilege SA, no Secrets, no exec | Admin |
| homelab-act (ours) | on demand | Its own token plus an approval token. About 6 curated tools mirroring CLAUDE.md S2, a ValidatingAdmissionPolicy-limited SA, no generic apply or patch | Admin |
| SearXNG | 6 or when search fees pass $15/mo | Internal, behind smokescreen | Research agents, local agents |

Tool scoping: 5–8 tools per agent, trimmed at the source (`enabled_tools`).

### 9.3 Gramps "as themselves" (phase 2)

**grampsweb image patch** (this repo, `images/grampsweb`):
- `ACCESS_TOKEN_SCOPE_MCP="mcp"` goes in `ACCESS_TOKEN_SCOPE_PERMISSIONS`
  only, and **includes delete** (D21). Approval is enforced in gramps-mcp.
- `TokenExchangeResource` (RFC 8693):
  1. verifies the subject token against luma-auth's JWKS (issuer, `aud`,
     and actor = gramps-mcp's SA);
  2. resolves the user: OIDC account, else a **unique active email match
     that is pinned to the registry's connector for that person**, else
     create through the family-editor patch. Ambiguous matches are refused;
  3. returns `issue_access_token(user, scope="mcp")`.
- The edge denies `/api/token/exchange/` publicly.
- Rebase onto v26.10.1 (API 3.23.x).

**gramps-mcp patch** (`nixos-configs`, applied by the repo flake):
- ASGI middleware validates the bearer (401 otherwise) and sets a
  `ContextVar` (`stateless_http=True`).
  - This part lands in **0a**, validating Dex JWKS: issuer, expiry,
    `aud`/`azp` ∈ facade clients, plus the allowlist.
  - In phase 2 it switches to luma-auth tokens.
- A per-subject token broker (exchange → `/api/token/exchange/`, cached until
  60 s before expiry), with retry-once on a 401.
- No shared account in HTTP mode, failing closed. Laptop stdio keeps password
  mode.
- **Delete tools require an approval token** (§10).

**Gramps agent hardening.** Tree content is **untrusted input**:
- `create_media(source_url)` is **disabled for this agent**. It is an
  in-cluster SSRF and exfiltration path.
- The agent **has no `artifacts.publish`**.
- `gramps-web-api` egress goes through smokescreen once enforcement lands
  (phase 4).

## 10. Approvals (phase 2)

**Rule.** Approval is enforced **at the resource**. Granting an approval
requires the **approver's verified identity**.

**One policy source.** Each tool's approval requirement lives in the agent
definition (`approval:`), and the MCP server enforces the same policy.
Clients can't skip it.

**Flow:**
1. **Pause.** The tool's call hits the conductor's PreToolUse hook. The
   hook returns `defer`, and the turn ends with `tool_deferred`. This works
   because the proxies are a local HTTP MCP server (SDK issue #370).
2. **Record.** Core writes an approval record: requesting person, agent,
   tool, args hash, nonce, a 2-minute expiry, single use, and the **required
   approver**. The required approver is the requesting person for their own
   data; an admin for admin actions.
3. **Notify:**
   - **Web Push** to the required approver's PWA installs, for everyone. The
     action posts to Core **with the approver's session cookie**.
   - **HA Companion actionable notification** to the approver's own devices,
     for household members only. HA's action event carries the responding
     HA user. ha-luma forwards it, signed with the HA SA token, and Core maps
     the HA user to a person and **checks that it is the required approver**.
4. **Approve.** Core (luma-auth) signs a single-use token bound to the
   record. The session resumes, and the call carries the token. The MCP
   server verifies it and **redeems the `jti` through Core**, which stores it
   as consumed in `acid-luma` (single use).
5. **Deny or no answer** within the window: the tool result is `denied` or
   `expired`.

**Other rules:**
- In-session questions (`AskUserQuestion`) are UX only and never a security
  gate.
- Everything is audited.

## 11. Agents in git

```
cluster/luma/agents/<id>/AGENT.md        # frontmatter + body = static system prompt
cluster/luma/agents/<id>/evals/*.yaml    # golden tasks (synthetic fixtures only — repo is public)
cluster/luma/skills/<name>/SKILL.md      # agentskills.io spec fields only
cluster/luma/mcp/servers.yaml            # name → url, transport, auth ref, per-tool policy
cluster/luma/models.yaml                 # tier → {harness, model, effort default, cache TTL} — site config
```

`AGENT.md` frontmatter:

```yaml
name: gramps
description: Ask about and update the family tree
tiers: [fast, smart]           # allowed params.tier; first = default
requires: {tools_max: 8, vision: false}   # capabilities a model/harness must offer
workspace: none                # none | fast | slow | user-choice (fixed per session)
mcp: [gramps.search, gramps.get_person, gramps.edit_person, gramps.delete_person]
approval: {gramps.delete_*: requester}
untrusted_inputs: [gramps.*]   # disables fetch-by-URL and publishing for this agent
skills: [gramps-research]
subagents: [deep-think]
limits: {max_turns: 30, max_tool_calls: 60}
audience: {tier: family}       # or {tier: admin} / {household: true}
memory: person                 # person | person+household | none
```

- **Model binding** (done-when #5): the `harness` and `model` for each tier
  live in **`models.yaml`**. Agent definitions only state requirements.
  Moving an agent to local means changing `models.yaml`; Core refuses a
  binding that doesn't meet `requires`.
- **Location:** this repo, delivered by Argo as a ConfigMap. The `luma` repo
  ships the format, the compiler and the default `luma` agent.
- **Compiler:** Agent SDK only until phase 6, then OpenHands or Goose. Agent
  versions apply to new sessions only.
- **Phase 1 agents:**
  - `luma` (chat, `fast` default, web tools, memory);
  - `deep-think` (a subagent on `smart`, explicit effort).

## 12. Caching, memory, artifacts, budgets

### 12.1 Caching and cost rules

1. **Render order:**
   1. tools (sorted, frozen per agent version, all MCP servers connected
      before the first request);
   2. a **fully static** system prompt;
   3. the fenced `<luma-context>` block (profile snapshot, plus household
      notes when allowed). It comes through SessionStart `additionalContext`;
      as a fallback, it opens the first user turn.
   4. append-only history;
   5. volatile data (date, HA state) as appended context.
2. Model pinned per session. Tool set fixed per session (D38).
3. **The gateway is semantically exact for Anthropic.** It forwards
   `anthropic-beta`/`version`, streams SSE unbuffered, keeps `ping`, and
   passes errors through. A byte-diff test is part of phase 1 (§17).
4. **TTL is per agent:** 1h for chat; 5m for Assist, sandbox loops,
   subagents and compaction.
5. Haiku chat compacts before 100K (§6).
6. **Background work** (memory consolidation, later scheduled agents) uses
   Batch, except where a same-model fork of a warm session is cheaper. For
   example, the session-end summary runs as a fork, at 0.1× cache reads, in
   the same workspace.
7. **Standing regression tests.** These run in-cluster, nightly, on
   `luma-ci`:
   - two identical Haiku requests must show `cache_read_input_tokens > 0`;
   - a cross-conversation request must read cache covering tools + system;
   - captured request bodies replayed through the raw API with
     `prefix_mismatch_behavior: "error"` must replay their thinking blocks.
     This is the only way to see silent drops: Claude Code auto-strips
     thinking on a 400.
   - Alert on gateway 400s whose message says "bound to a different
     conversation".
8. **SLO:** cache-read share of input tokens ≥ 80%, **token-weighted, over
   `request-class=main` only, excluding each session's first request**.
   Investigate `cache_creation` spikes that compaction headers don't
   explain.
9. **Expected cost** (reviewer's model; 8 people × 20 turns/day, Haiku
   default): **about $95–110/month**.
   - Largest lines: web search (about $11), the Smart tier for Chris (about
     $20), sandbox tasks (about $25), evals (about $14, weekly and
     Haiku-gated).
   - Tail risks: injected runaway web search; Opus sandbox tasks; Haiku over
     100K.

### 12.2 Memory

- **Phase 1:** `profile.md` per person (≤1.5k tokens), `remember` and
  `forget` tools, and a profile editor page in the PWA.
- **Phase 2:** household notes, readable by **household** members and
  `house` only, plus a session-end summary into dated notes.
- **Later:** nightly consolidation into a revertable diff, and recall search.
- **Memory is data, not instructions** (anti-poisoning):
  - Snapshots are inserted as fenced, quoted context. The system prompt says
    memory content is user-provided data and never instructions.
  - Every `remember` write records its provenance (session, turn, whether
    untrusted tool content was in context that turn).
  - Writes made in a turn that read untrusted content are **held for the
    person's confirmation** in the UI.
  - **Household-scope writes always need a human confirmation.**
- **SDK auto-memory stays off.** Incognito reads and writes nothing.

### 12.3 Artifacts (phase 4; host `share.luma.chrismiller.xyz`, Chris D17)

The host is a small Go service, `luma/artifacts/`, run from the same image as
Core as a separate Deployment. Add `share.luma` (or `*.luma`) as a SAN on
`wildcard-cert.yaml`.

**DNS.** The `*.chrismiller.xyz` wildcard covers nested names through
wildcard synthesis, as long as `luma.chrismiller.xyz` has no explicit record.
Verify `share.luma` with `dig`, and add a terraform record if it doesn't
resolve. The same applies to `browser.luma`.

**Tested in Chromium, Firefox and WebKit:** with the sandbox CSP, the document
has an opaque origin. Its scripted requests carry no cookies, and storage and
service workers are blocked. The page's own document request still carries
the `.chrismiller.xyz` cookies, which is why rule 4 exists.

**Mandatory rules.** Any violation is a same-site XSS on every
`chrismiller.xyz` app.

1. **Headers on every response**, including 404, 500 and redirects. They are
   set by the app **and** by a Traefik headers middleware. Phase 4 artifacts
   are single-file with everything inlined, so **no CDN is allowed**:

   ```
   Content-Security-Policy: sandbox allow-scripts; default-src 'none';
     script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline';
     font-src 'self' data:; img-src 'self' data: blob:; media-src 'self' data: blob:;
     connect-src 'none'; form-action 'none'; base-uri 'none'; frame-src 'none';
     frame-ancestors 'self' https://luma.chrismiller.xyz
   X-Content-Type-Options: nosniff
   Referrer-Policy: no-referrer
   Permissions-Policy: camera=(), microphone=(), geolocation=(), usb=(), payment=()
   Cache-Control: private, no-store   (public: short max-age)
   ```

2. **Never send:**
   - `allow-same-origin`, `allow-forms`, `allow-modals`, `allow-downloads`,
     `allow-popups-to-escape-sandbox`;
   - `Access-Control-Allow-*`, `Set-Cookie`, CORP.
3. **Content types from an allowlist.** Anything else is
   `application/octet-stream` plus `attachment`.
4. **Strip cookies.** A `Cookie: ""` request-header middleware sits *after*
   forward-auth.
5. **No access logs** on the share routers.
6. **Visibility changes are human clicks.** Changing visibility to
   **family or public** happens only on the PWA's "My artifacts" page.
   The model can only publish **private**.
7. **Cryptographic identity.** Family and private routes check identity
   with a signature, never from a forwarded email header. oauth2-proxy (family
   tier) passes the ID token, and the host verifies the Dex signature,
   audience and allowlist. Owner and visibility are then rechecked.
8. **Tests** cover every error path and header.

**Routes:**
- `/a/<id>/`: private or family, behind the family-tier pair plus rule 7.
- `/p/<slug>/`: public. A 128-bit capability, rotatable and revocable (410),
  with an optional expiry.

Multi-file artifacts (`/s/<token>/`) come later.

**Storage:** Postgres `bytea` in `acid-luma`: about 2 MB per file and 10 MB
per artifact, with immutable sha256 versions.

**Publish tool:** `publish(title, files|sandbox_paths, entry)` returns a
private artifact, plus `list` and `revoke`.

**Upgrade path:** a separate registrable domain is still the upgrade path,
and it is required before self-hosting any Sandpack-style bundler.

### 12.4 Gateway and budgets

- **agentgateway standalone**, v1.6.x pinned:
  - **2 replicas with a PDB**, ClusterIP only, Traefik untouched;
  - route type `detect` for `/v1/messages`;
  - JWT policy for luma-auth tokens. In phase 1 the conductor uses a static
    internal bearer (a SealedSecret); per-session tokens arrive with
    sandboxes.
  - **Real workspace keys live only here.**
- **Price catalog overlay** for `claude-haiku-5-5`, with 1h writes at 2×.
  The catalog has no context-length tiers or per-request fees, so **the
  gateway's dollars are informational only**. Core's ledger (§5.2.5) is the
  dollar source of truth, and the Console is ground truth.
- **Anthropic workspaces.** Limits sum to $200 or less (D3, D22). Keys are
  held by the gateway.

  | Workspace | Purpose | Default limit |
  |---|---|---|
  | `luma-interactive` | PWA chat, Gramps | $150 |
  | `luma-house` | HA Assist, so runaways elsewhere can't break voice | $10 |
  | `luma-background` | Batch jobs only: nightly consolidation, later scheduled agents. Provisioned with the first Batch job. | $10 |
  | `luma-ci` | evals and regression tests | $20 |
  | `luma-computer-use` | on demand | $10, carved from interactive when enabled |

  A session never switches workspace, because caches are per workspace.
  So session-end summaries (forks) and **sandbox sessions bill to
  `luma-interactive`**. `luma-house` is provisioned in phase 3.
- **Metrics:** `agentgateway_gen_ai_client_token_usage{gen_ai_token_type}`
  labelled by person, agent, tier and request class. The ServiceMonitor
  carries `release=prometheus`. Rules are PrometheusRule CRDs in namespace
  `prometheus` with the selector labels, plus a deploy check that they appear
  in `/api/v1/rules`.
- **Why not LiteLLM:** 17 advisories in 2026, two exploited in the wild, a
  PyPI compromise, and memory growth.

## 13. Network, egress, logs, evals

### 13.1 NetworkPolicy enforcement (phase 4, via GitOps — D25)

- **Install:** kube-network-policies **v1.1.2 or later** as an Argo-managed
  DaemonSet in `kube-system`, at `--v=1`.
  - **Fail-open, plus the canary** (my call). Enforcement is fail-open at
    boot anyway, so fail-closed only turns agent crashes into outages.
    NetworkPolicy is defence in depth here.
  - Don't let Talos ≥1.13's bundled copy run alongside it.
  - Add it to Renovate's gated list.
- **Prerequisite checks**, first on kind, then on one node:
  - whether only policy-selected pods' traffic is queued (Ceph runs on the
    pod network);
  - whether kubelet probes and node-to-pod traffic are exempt from
    default-deny ingress.
- **Dormant policies:**
  - Validate argo-cd's six. `argocd-redis` accepts only its peers; metrics
    are open.
  - The two MetalLB policies **select nothing** (`instance=metallb-release`
    vs actual `instance=metallb`) and no Argo app owns them. Propose deleting
    them, which needs Chris's OK.
  - Snapshot Argo and MetalLB health before and after.
  - Write a **pre-approved break-glass `kubectl` procedure** into the plan
    (S2/S4), in case argo-cd can't sync its own revert.
- **Rules:**
  - Allow by podSelector; post-DNAT IPs apply.
  - The API server needs `ipBlock` node IPs on :6443.
  - Dex is reached through its in-cluster Service. The public name hairpins
    to the WAN IP.
- **Luma policy set:**
  - Default-deny in each `luma-*` namespace.
  - Sandbox pods reach only the conductor, agentgateway, smokescreen `code`
    and Attic. **All MCP traffic goes through the conductor's proxies**, so
    no tool credentials or MCP endpoints are reachable from a sandbox.
  - **No DNS for sandboxes.** Use `hostAliases` to **reserved fixed
    ClusterIPs**.
  - Ingress only from Core and the conductor.
- **Canary.** A sandbox-labelled pod probes, **directly**, the router SSH,
  Mosquitto `.8:1883`, gramps-mcp, the KVM `.2` and `1.1.1.1:443`. It also
  probes **through smokescreen** to a LAN IP, the WAN IP, an IPv6 ULA, and a
  `*.chrismiller.xyz` host. Every probe must fail. A PrometheusRule in
  namespace `prometheus` alerts if any succeeds.
- Every service still authenticates itself.

### 13.2 Egress proxy

- **Phase 1:** one smokescreen deployment, `web`, for the conductor CLI's
  WebFetch. It blocks home and carries the curated `exfil-deny` list.
- **Phase 4:** a `code` deployment for sandboxes, and **Blocky sidecars**.
- **Deny list:**
  - our domains (`*.chrismiller.xyz`, `*.realliance.net`,
    `*.werethemille.rs`, `*.buttert.art`, plus the bare names);
  - RFC1918, `100.64.0.0/10`, `169.254.0.0/16`, the **WAN IP**;
  - **IPv6** `fc00::/7`, `fe80::/10`, `::ffff:0:0/96`, and our IPv6
    prefix if there is one (verify; or refuse AAAA at the proxy);
  - NAT64, 6to4 and Teredo.
- **Patch smokescreen** to reject raw-IPv4 CONNECT targets in `open` roles.
- **Blocklists in three layers.** smokescreen spends 140 ms per connection
  at 100k entries (benchmarked) and has no hot reload. So:
  1. **In smokescreen** (small lists, in git):
     - `exfil-deny`: tunnels, request catchers, OAST services, and paste or
       file-drop sites (as researched), plus `api.telegram.org`;
     - `discord.com`, which is a policy call;
     - a false-positive allowlist.
  2. **Blocky** as smokescreen's resolver:
     - HaGeZi Pro and TIF (medium for `web`, mini for `code`);
     - doh, doh-vpn-proxy-bypass, urlshortener, dyndns;
     - Phishing Army and URLhaus;
     - `loading.strategy: blocking`;
     - sized after measuring its memory.
  3. **Later:** newly-registered-domain (NRD) lists, or Cloudflare Gateway
     "New Domains" as Blocky's upstream.
- **"Open web, never home"** for both profiles (D14). The GitHub `gh-aw`
  ecosystem allowlist stays on file for an opt-in strict profile.
- **GitOps:**
  - Feed URLs are pinned to commits and bumped by an auto-merged daily PR.
    URLhaus and Phishing Army refresh at runtime as a documented exception.
  - Keys live in SealedSecrets.
  - HaGeZi is GPL-3.0, so include the license if it is vendored.
- **Limits:**
  - **Web-capable agents get no credentialed or write tools to other
    systems, and no other people's data.**
    - The default `luma` agent does see its own person's memory and
      attachments while it has web access. That residual exfiltration risk
      is **accepted**, and mitigated by the exfil denylist, search caps,
      fenced memory and no auto-loaded images.
    - HA admin tools live on a separate **no-web** agent.
    - Lists stop only commodity abuse.
  - The proxies' own egress is the public internet on 80/443, plus kube-dns.

### 13.3 Logs

- **fluent-bit excludes every `luma-*` namespace** from the Azure and syslog
  outputs (an input exclude on the namespace path). The namespaces are:
  - **`luma-system`:** Core, the conductor, the artifacts host,
    agentgateway, smokescreen and `acid-luma`;
  - **`luma-sandbox`**;
  - **`luma-browser`**.
- **Core and the conductor log metadata only:** ids, tool names, durations,
  decisions. Never message content or tool arguments.
- **Debugging:** `kubectl logs` and the session event log in `acid-luma`.
- kube-network-policies runs at `--v=1` so it doesn't flood router syslog.

### 13.4 Evals and upgrades (Chris, D30)

- **Where they run:** in-cluster, as an Argo-managed CronJob or Job that
  goes through the gateway on `luma-ci`, with a hard limit of about $20.
- **What gates a bump:**
  - Renovate groups SDK, CLI, agentgateway, agent-sandbox and assistant-ui
    bumps **weekly**, with a 3-day minimum age;
  - each bump is gated on a 10–20-task **Haiku** smoke set, plus the cache
    tests, a resume-from-previous-CLI test and a large-session resume test;
  - a small Sonnet set runs weekly;
  - WebSearch is stubbed;
  - trials run sequentially, so later trials read the cache.
- **Fixtures:** golden tasks use a **synthetic Gramps fixture tree** and
  synthetic people. Real family data never goes into the public repos.

## 14. Phases (lean order — Chris, D28)

Each phase gets its own plan, and its exit criteria are verified. Cluster
work runs in parallel with code wherever possible.

**0a. Live auth fixes** (this repo; first)
- Dex v2.46 with an expiry block, plus refresh tests.
- Consent for dynamic clients, and a DCR exact-redirect allowlist.
- `mcp-jwt-auth` audience and allowlist. First verify whether Dex access
  tokens carry `aud`, or take the §17 fallback.
- gramps-mcp self-auth, still on the shared account.
- Metadata fixes: the 401 `resource_metadata` and the issuer mismatch.

*Exit:*
- A foreign-redirect registration is refused.
- **A dynamically registered client shows a consent screen naming its
  redirect host.**
- A token for another client is rejected at mcp-edge or `mcp-jwt-auth`.
- An unauthenticated in-cluster POST to gramps-mcp gets a 401.
- Chris's Claude Code works, after a reconnect.
- The edge snapshot diff is clean.

**0b. Platform prep for phase 1** (this repo; in parallel with phase 1 code)
- **Ceph back to its prior state** (doctor run, then any repair Chris
  approves) before touching Rook.
- **Right-sizing pass**, as its own plan:
  - first, give the BestEffort household-critical pods real requests
    (nfs-ganesha, or fix its growth; MDS; HA; Grafana; MetalLB);
  - then reclaim:
    - Rook mons 4 → 1.5 Gi (they stay system-node-critical), mgr, CSI
      plugins;
    - Prometheus 4 → 2.5 Gi, gonic;
    - **OSD requests are left alone** until we know whether
      `osd_memory_target` follows the request or the limit.

  Target: about 8 GiB of net headroom on top of today's, with no node over
  85% real use. These are Rook changes under S4/S7, so Chris approves them
  in the plan.
- **Prometheus `ruleSelector`/`ruleNamespaceSelector` fix**, so that stock
  alerts load cluster-wide, including for Luma's namespaces (crash-looping
  pods, PVCs filling, quota exceeded). Luma's own rules live in namespace
  `prometheus` either way.
- **fluent-bit exclusion** for `luma-*`.

*Exit:* headroom measured, test rule visible in `/api/v1/rules`, Ceph back to
its normal WARN noise.

**1. Lean chat** (Chris only; the `luma` repo plus this repo)
- **Core:**
  - BFF auth;
  - sessions, with an event log and replay-from-id;
  - conversation list, and fork for edit and regenerate;
  - model pinning through `models.yaml`;
  - the SessionStore on `acid-luma` (PITR, `synchronous_commit` on);
  - attachments (images and PDF);
  - the cost ledger, global-cap warnings, and basic maintenance mode.
- **Conductor:** a local CLI subprocess per conversation, with the explicit
  tools (WebSearch and WebFetch via smokescreen; memory tools), idle eviction
  and resume.
- **agentgateway:** 2 replicas, the `luma-interactive` and `luma-ci` keys, a
  static internal bearer, metrics and rules.
- **Config delivery:** `AGENT.md` files and `models.yaml` are delivered from
  this repo as an Argo ConfigMap, which Core reads at start and on change.
- **smokescreen `web`:** deny-home plus `exfil-deny`.
- **PWA v1:**
  - streaming chat with tool cards;
  - history list and fork;
  - image, camera and PDF attach;
  - Android install, with **login that persists in installed mode on a real
    phone**;
  - reconnect and resume after Android backgrounding;
  - plain-language errors for 429/529, a conductor restart, and the budget
    cap;
  - the profile editor.

  Not in v1: `share_target`, Web Push, full-text search, dictation (Gboard's
  mic covers it).
- **Agents:** `luma` and `deep-think`, in the final `AGENT.md` format.
- **Grafana:** dollars per person per day, cache-read share by request
  class, plus the regression tests on `luma-ci`.

*Exit:*
- A week of Chris's daily use on Android.
- The SLO is met and the regression tests are green.
- An `acid-luma` **restore drill** is done.
- `luma.chrismiller.xyz` is in the edge snapshot.
- The phase-1 spikes are resolved: the gateway byte-diff, the SessionStore
  resume prefix being byte-identical, and the TTL env var.

**2. Gramps agent; household members, then first relatives**
- **luma-auth** module and the people registry with its generator,
  including the household flag.
- The **grampsweb exchange patch**, rebased, and the **gramps-mcp per-user
  patch**.
- **mcp-edge v1** replaces hyprmcp.
- **Approvals** (§10): in-app plus Web Push. HA push arrives with ha-luma in
  phase 3.
- The Gramps agent definition and per-tool policy (`servers.yaml`).
- Household notes in memory, with confirmed writes.
- **Household members onboard here,** and then the first extended-family
  relatives.
- `share_target` in the PWA.
- **Gate:** Chris has reviewed Anthropic's Commercial Terms for household and
  family use before the first relative is onboarded (§16).

*Exit:*
- A relative's edit appears under their own Gramps user.
- A delete waits for their approval and runs only after it.
- Chris's hosted-client connection works through mcp-edge.

**3. HA Assist**
- **ha-luma:**
  - maps `context.user_id` to a person (authenticated Companion users),
    otherwise `house`; satellites always map to `house`;
  - timeout of 6 s or less, with a spoken "I'll send it to your phone"
    fallback;
  - continues the same conversation across HA's 5-minute expiry, for up to
    30 minutes (my call).
- **House agent:** a **person-agnostic static prefix**, so one warm cached
  prefix serves everyone. Person context comes through the SessionStart
  hook, or the first turn (§6). A prewarmed house CLI is kept ready.
- `luma-house` workspace; `prefer_local_intents` on.
- **Non-admin HA users** for household members.
- **HA chat tools for admin**, on a separate **no-web `home` agent**,
  through Chris's HA OAuth connection.
  - The **connections vault and per-user connect flow** are built here.
    Phase 5 reuses them for Fastmail.
- **HA push approvals** for household members (§10 step 3), now that ha-luma
  exists.
- `luma-house` workspace key.
- Per-hop latency histograms.
- **Voice (D31):**
  - **Start CPU-only.** Run Wyoming `rhasspy/wyoming-whisper` with sherpa
    (Parakeet TDT v3; faster-whisper base-int8 as the alternative) plus
    `wyoming-piper` with streaming. Both go on `bottom`, which is the
    coolest node and has the most headroom.
    - About 1.5–2 Gi of requests.
    - No Talos change, no device plugin, no privileged pods.
  - **One pipeline.** `prefer_local_intents` is on, and Luma is the
    conversation agent. speech-to-phrase on a second wake word is optional.
  - **Benchmark on the node first.** The budget is: end of speech about
    0.5–1 s, STT about 0.5 s, Luma's first token 1–2 s, Piper's first
    sentence about 0.2 s. That makes 2.5–4 s, but **only if ha-luma streams**
    deltas, so that Piper starts before the reply ends.
  - **Step up if CPU misses the target.** Move to whisper.cpp or parakeet.cpp
    on Vulkan on **middle's Polaris** (8 GiB VRAM), using an image we build.
    - Expose `renderD128` through `squat/generic-device-plugin` with a count
      of 2–4. That lets **Plex share the card** for hardware transcoding,
      which also removes middle's nightly 95 °C CPU-transcode load.
    - Only after middle's memtest verdict.
    - The CPU pipeline stays as the fallback. Failover is manual.

*Exit:*
- A Companion-app request that **needs the model** (not a local intent)
  reaches Luma as the right person.
- Time to first response is 4 s or less on the warm path.
- A satellite request runs as `house`.

**4. Sandbox and artifacts.** Prerequisite: the **NVMe swaps done** (#2661).
Within the phase, enforcement and the canary must be green before any
sandbox or the artifact host ships.
- kube-network-policies, the dormant-policy validation, and the canary.
- agent-sandbox; the **`fast`** template; the Nix image.
- Remote spawn, plus per-session gateway JWTs.
- B2 snapshots with the 7-day lifecycle.
- smokescreen `code`, plus Blocky lists.
- **The `slow` profile only after the throttle spike passes.**
- Artifact host with every §12.3 rule and its tests; "My artifacts".
- Extended-family sandbox access waits for **required** affinity to a
  non-control-plane worker (GPU box), unless Chris accepts otherwise at that
  time.

*Exit:*
- "Chart this spreadsheet" works end to end.
- A family-only artifact can't be opened while logged out.
- Visibility changes only by a click.
- Every canary probe fails.
- A drain under maintenance mode loses no work.

**5. Mail.** Fastmail connection; the mail agent (no web tools, read + draft,
send behind approval).

**6. Local inference.**
- The GPU box (D23).
- llama-swap with llama.cpp, or vLLM.
- The second harness (OpenHands agent-server, via an ACP adapter) and
  SearXNG.
- A one-week bake-off on agent evals.
- Voice on the GPU box, if CPU and Polaris both missed the target.

**On demand.** These designs are kept in this document:
- homelab-read and homelab-act (§9.2);
- computer use (§7.6);
- scheduled and proactive agents (Batch, run caps, ntfy or HA push);
- the OpenAI-compatible and MCP adapters (§5.1).

### Capacity (ops reviewer's estimate; memory requests)

| Phase | Steady Luma requests | Peak |
|---|---|---|
| 1 | ≈3.3 GiB (conductor ≈1.8 GiB at the 4-CLI cap; Core, 2 gateway replicas, smokescreen, `acid-luma` ×2; limits ≈8.5) | — |
| 2 | ≈4.6 GiB (6-CLI cap, mcp-edge) | — |
| 3 | ≈7.3 GiB (includes about 1.7 GiB of CPU voice on `bottom`, and the house CLI) | ≈8 GiB |
| 4 | ≈9.8 GiB | ≈12 GiB (sandbox limits are quota-capped at 8 GiB) |
| 5–6 | ≈11 GiB | ≈14.8 GiB |

Check placement per node, not only in total. Voice is pinned to `bottom`,
sandboxes avoid `middle`, and HA runs on `top`.

Headroom after an honest right-sizing pass is about 17 GiB, so steady state
fits. The single-node drain shortfall stays at about 21 GiB, so **Luma is
shed during drains**. Only a worker node adds real capacity.

## 15. Local models (phase 6)

- **Where it runs:** the gateway hides it. Either a Talos worker (NVIDIA
  extensions plus GPU Operator with the driver disabled) or a NixOS
  satellite for a unified-memory box. Weights stay on local NVMe, never Ceph
  SMR.
- **Engine:** llama.cpp `llama-server` behind llama-swap (Anthropic
  `/v1/messages`, GBNF and JSON-schema constraints, hot-swap), or vLLM above
  about 3 concurrent streams. Sampling settings are fixed per model at the
  server.
- **Harness:** the **OpenHands agent-server**, with **Goose** as the fallback,
  over an ACP adapter.
  - The Agent SDK is used against local backends only experimentally,
    pinned, with a lean config. Anthropic doesn't support it, and its
    releases keep breaking templates, grammars and prefix caches.
- **Small-model rules:**
  - 5–8 tools with flat schemas;
  - repair or drop unknown arguments rather than rejecting the call;
  - native function calling with the right parser;
  - short horizons;
  - a stable short prefix;
  - loop and narration detection;
  - escalate to Claude on failure.
- **Candidates:** Qwen3.6-35B-A3B if VRAM allows, gpt-oss-20b, Qwen3.5-9B.
  Choose by our evals.
- **Early experiments (optional).** llama.cpp's Vulkan backend on middle's
  8 GiB Polaris card can run a ~4–9B Q4 model. That makes it a cheap way to
  start the harness bake-off and the local-model evals before the GPU box
  arrives. It is shared with voice and Plex, and it waits for the memtest.
  It is **not** a production backend.
- **Routing (`models.yaml`):** local for:
  - privacy-sensitive work;
  - agents with few tools and short tasks;
  - reversible actions;
  - background or offline work.

  Claude for everything else.
- **Why local:** privacy and control, offline resilience, and learning. Not
  cost: Haiku 5.5 is cheaper than a GPU box's idle power at household
  volume.

## 16. Security model

**Trust boundaries:**
1. Clients.
2. The edge.
3. Core: the trust anchor, holding credentials, the vault and luma-auth.
4. **The conductor: semi-trusted.** It parses model output and tool results,
   and holds no user credentials; it gets per-call capabilities.
5. Conductor-local CLIs: model-chosen tool calls, no shell.
6. Sandbox and browser pods: **untrusted code**.
7. MCP servers: each validates its own audience.
8. Downstream apps: they have the final say.

| Threat | Mitigation |
|---|---|
| Prompt-injection exfiltration | Per-agent trifecta split: the mail agent has no web; the Gramps agent has no URL fetch and no publishing; HA admin tools live on a no-web agent. Web-capable agents get no credentialed or write tools and no other people's data. The default agent's own memory and attachments are an accepted risk (§13.2). The model can publish only private artifacts; family and public visibility are human clicks. Strict `img-src` on the Luma host. A curated exfil denylist. |
| Memory poisoning, persistent or cross-user | Memory is fenced as data. Writes record provenance. A write made in a turn that read untrusted content needs confirmation. Household writes always need confirmation (§12.2). |
| Key or credential theft | No real keys in pods. Gateway keys live only in the gateway. User tokens stay in Core, and the conductor gets per-call, session-bound capabilities. B2 credentials stay in Core. |
| Identity spoofing | Identity comes from signed tokens only: the BFF cookie → Core, TokenReview, luma-auth, and the Dex signature on artifacts. **Never** from forwarded headers or tool arguments. The registry pins connectors. Microsoft uses UPN only. Voice satellites map to `house`. |
| Approval bypass or forgery | Verified at the resource. The approver's identity is verified and must match the required approver (§10). Single-use `jti`s are stored. |
| Lateral movement | Default-deny NetworkPolicy (phase 4). Every service authenticates. No DNS for sandboxes. The proxy denies home, including IPv6, and raw IPv4 is patched. The canary tests both direct and proxied paths. |
| Container escape onto an etcd node (runc, D13) | PSS restricted, RuntimeDefault seccomp, non-root, no capabilities, no SA token, limits, quotas, negative priority. Family sandboxes get **required** affinity to a worker once one exists. **Residual risk accepted** while sandboxes are Chris and household only. |
| Artifact XSS on the parent domain | The §12.3 rules, enforced twice, with tests. No CDNs. Cryptographic identity. |
| Runaway cost | Workspace hard limits (house and CI separate). Web-search caps. `max_turns` and tool limits. The Core ledger with an 80% warning. Orphan-CLI kill. |
| Data leaving the house | fluent-bit exclusion. Metadata-only logs. Inference goes to Anthropic, which is accepted (D3). |
| OpenClaw-class exposure | No unauthenticated control surface. No tokens in browsers or URLs. WS Origin checks. No skill marketplace: skills are vendored and reviewed. |

**Terms.**
- Agent SDK use falls under Anthropic's Commercial Terms, with an API key
  and no claude.ai logins.
- The product is "Luma, powered by Claude", never "Claude Code".
- Disclose that users are talking to an AI.
- The Commercial Terms say "not for consumer use". **Chris reviews this
  before onboarding relatives** (the phase 2 gate).

## 17. Verifications (spike in the named phase)

| Item | Phase | If it fails |
|---|---|---|
| Dex access tokens carry a usable `aud` (cross-client trust) | 0a | Pull a minimal mcp-edge into 0a that re-mints audience-bound tokens for `/gramps/mcp`; reject tokens without a verifiable audience |
| Per-client consent mechanism | 0a | Turn the skip off globally, with Dex remembered consent |
| Gateway output byte-diffed against input; `detect` usage from SSE `message_delta`; 1h TTL beta forwarded | 1 | A small Go passthrough with usage extraction |
| SessionStore resume is byte-identical to the original prefix | 1 | File an upstream bug; pin the version; keep conversations in one live CLI longer |
| `CLAUDE_CODE_PROMPT_CACHE_TTL`, compaction env vars, SessionStart `additionalContext` | 1 | Accept defaults and measure; context opens the first user turn and Core re-injects it after compaction |
| Per-message effort change is cache-preserving through the gateway | 1 | Escalate with the subagent only |
| `defer` resume with the local-HTTP proxy server (#370) | 2 | A `canUseTool` pending promise with a durable record and a 2-minute timeout |
| HA 2026.10 PKCE/CIMD; the Companion action event carries the responding user | 3 | A long-lived token for an HA admin `luma` user (admin-only); Web Push for approvals |
| CPU Parakeet + Piper latency on `bottom` (15-minute benchmark); ha-luma streaming into Piper | 3 | Polaris + Vulkan whisper.cpp on middle; otherwise text Assist until the GPU box |
| kube-network-policies: queues only selected pods; probes exempt | 4 | Exempt probes explicitly; delay enforcement |
| agent-sandbox on k8s 1.33; pod-name reuse after node loss | 4 | Our own minimal claim controller |
| HDD write throttle (cgroup `io.max` via ceph-csi or pod blkio) | 4 | `slow` stays disabled |
| Nix under PSS restricted; evaluation memory | 4 | Larger limit; bake more into the image |
| Blocky memory with TIF medium; smokescreen raw-IPv4 patch | 4 | TIF mini; Squid (peek-and-splice) |
| Fastmail DCR and refresh through Core | 5 | Per-user JMAP API tokens |

## 18. What's left to the builder

- Package layout within `luma` (`core/`, `conductor/`, `mcp-edge/`,
  `artifacts/`, `web/`, `images/`, `charts/`). A Helm chart is published
  like patchbay's, and this repo pins `targetRevision`.
- PWA design inside assistant-ui. Depend on `ExternalStoreRuntime` (and
  `RemoteThreadListRuntime`) only, to limit churn.
- The internal Core↔conductor message schema, within §5.1's event shapes.
- Metric names beyond those required; dashboard layout.
- Where new Nix store paths land (§7.4); snapshot cadence.
- The format of mcp-edge's per-tool policy, and of `models.yaml`.
- The phase 0a consent mechanism (§8.1).
- Exact resource numbers, once measured. The §14 table is the starting
  point.

## 19. Research and review record (2026-10-18)

**Research passes:**
- repo recon;
- mobile and front-ends;
- gateway, memory, sandbox and local models;
- computer use, Nix and MCP auth;
- LibreChat (rejected as the harness: no release supports Haiku 5.5, and an
  unstable prefix: #16911, #17102, #16877);
- inference gateway;
- sandbox, computer use and NetworkPolicy;
- MCP layer;
- identity and authorization;
- artifacts, memory, HA and mobile (an empirical three-engine browser test);
- whole-platform zoom-out;
- Agent SDK runtime and UI;
- open-model harnesses;
- egress filter lists (smokescreen benchmarked).

**Reviews of v1:**
- **Builder check:** 8 questions, resolved in this version.
- **Security:**
  - the "nOAuth" critical was refuted against Dex source (the Microsoft
    connector uses UPN);
  - the approver identity, memory poisoning, Gramps SSRF, artifact identity
    and CDN, IPv6 and conductor concentration findings are adopted.
- **Ops/capacity:** BestEffort OOM risk, the HDD throttle gap, the rule
  selector, Azure log export, SPOFs, phase order, `acid-luma` durability and
  the HA latency path are adopted.
- **Cost/caching:** the dynamic-boundary gap behind gateways, fixed tool
  sets, helper-model pinning, search caps, the ledger, the SLO definition,
  the eval cost and the SDK issues are adopted.
- **Scope:** the lean phase 1, internal protocol, merged luma-auth, phase
  reorder, mobile basics and the BFF are adopted. The separate artifact
  domain was declined (Chris).
