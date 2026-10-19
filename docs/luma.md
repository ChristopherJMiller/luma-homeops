# Luma (household agent platform)

Luma is the in-house agentic assistant for this household, named after the
homelab. It is "Claude.ai plus Claude Code on the web, for this household":
chat with memory and tools, agents defined in git (first a Gramps agent),
Home Assistant both ways, Nix sandboxes, and shareable artifacts. Data stays
in galaxy, and the model behind any agent can be swapped, including for a
local GPU box later.

**This page is the entry point.** Read it first when picking Luma back up,
then the plan for the current phase.

| What | Where |
|---|---|
| Umbrella design (approved 2026-10-18) | `docs/superpowers/specs/2026-10-18-luma-design.md`. Decisions are in §3, phases in §14, open verifications in §17. |
| Research and review digest | `docs/luma/research-2026-10-18.md`: verified facts, numbers and sources, so nothing has to be re-researched |
| Product code | [ChristopherJMiller/luma](https://github.com/ChristopherJMiller/luma) (public, Apache-2.0), cloned at `~/Repos/luma`. Monorepo: Go Core, TS conductor, assistant-ui PWA, Helm chart, images. Product plans live in its `docs/plans/`. |
| HA integration | [ChristopherJMiller/ha-luma](https://github.com/ChristopherJMiller/ha-luma) (public), cloned at `~/Repos/ha-luma`. Starts in phase 3. |
| Site config (this repo) | `cluster/luma/` (agents, `models.yaml`, MCP server list) and the Argo apps, Dex client, secrets and Postgres (from phase 1). Homeops plans live in `docs/superpowers/plans/2026-10-18-luma-*.md`. |
| Public host | `luma.chrismiller.xyz` (PWA and API). Artifacts: `share.luma.chrismiller.xyz` (phase 4). |

## Shape in one paragraph

The pieces, in order of the request path:

1. **Luma Core** (Go). An AgentCore-style session API, shaped like
   Anthropic's Managed Agents. It owns sessions, the append-only event log,
   model pinning, memory, approvals, the connections vault and the cost
   ledger. It also runs the PWA's login as a backend-for-frontend, and from
   phase 2 hosts `luma-auth`, a token-exchange module.
2. **The conductor** (TypeScript). It hosts the **Claude Agent SDK**, which
   is the harness, so caching and append-only history are its job. It runs
   the Claude Code CLI locally, or in a sandbox pod from phase 4.
3. **agentgateway.** A byte-faithful Anthropic passthrough that holds the
   real keys.
4. **MCP servers.** Each validates its own audience, and acts as the user.
5. **Clients.** Our **assistant-ui PWA** and the `ha-luma` integration. Both
   are clients of Core, and neither ever touches a prompt.

## Phase status

Update this table whenever a phase moves.

| Phase | What | Plan | Status |
|---|---|---|---|
| 0a | Live auth fixes: Dex 2.46 with expiry and consent, DCR redirect allowlist, `mcp-jwt-auth` audience check, gramps-mcp self-auth, MCP metadata fixes | `docs/superpowers/plans/2026-10-18-luma-0a-auth-fixes.md` | plan drafted, in review |
| 0b | Platform prep: Ceph gate, right-sizing pass, Prometheus rule-selector fix, fluent-bit exclusion of `luma-*` | `docs/superpowers/plans/2026-10-18-luma-0b-platform-prep.md` | plan drafted, in review |
| 1 | Lean chat, Chris only: Core, conductor, agentgateway, smokescreen `web`, PWA v1, `luma` + `deep-think` agents | product: `~/Repos/luma/docs/plans/phase-1-lean-chat.md`; deploy: `docs/superpowers/plans/2026-10-18-luma-phase1-deploy.md` | plans drafted, in review |
| 2 | Gramps agent; household members, then first relatives. luma-auth, people registry, grampsweb and gramps-mcp delegation patches, mcp-edge v1, approvals with Web Push | not written | — |
| 3 | HA Assist: ha-luma, `house` identity, CPU voice, HA admin tools on a no-web agent, connections vault, HA push approvals | not written | — |
| 4 | Sandbox and artifacts: kube-network-policies via GitOps, agent-sandbox `fast` profile, Nix image, artifact host | not written | gated on the NVMe swaps |
| 5 | Mail (Fastmail official MCP) | not written | — |
| 6 | Local inference: GPU box, llama-swap, OpenHands agent-server bake-off, search MCP | not written | gated on hardware |
| on demand | Homelab read/act tools, computer use, scheduled agents, OpenAI-compatible and MCP adapters | designs in the spec | — |

## Gates and blockers outside Luma

- **Ceph / node `middle` (2026-10-18).**
  - PG 1.c had a bad replica on osd.6. It was repaired 2026-10-19 01:16→01:53 UTC (evening of 10-18 Pacific) with
    Chris's approval: 0 inconsistents, health back to the usual WARN.
  - **The memtest86+ trigger on `middle` is met**: three bad non-primary
    replicas on middle's disks since 10-11, with SMART clean, so RAM, DMA or
    SATA is suspected. Chris runs memtest in a maintenance window (see the
    `known_issues` memory note).
  - Until then, nothing new is placed on `middle`, and that includes its
    Polaris GPU.
- **NVMe swaps (#2661)** gate phase 4. Sandbox scratch writes would hit the
  worn system disks.
- **Memory headroom:** requests are at 89–91% per node. The 0b right-sizing
  pass comes before phase 1 deploys anything sizeable.
- **Anthropic Commercial Terms.** Chris reviews the "not for consumer use"
  clause before the first relative is onboarded (the phase 2 gate).
- **Anthropic Console.** Chris creates the workspaces and their keys. Phase 1
  needs `luma-interactive` ($150), `luma-ci` ($20) and an org Admin key for
  the Usage API reconcile. `luma-house` ($10) arrives in phase 3, and
  `luma-background` ($10) with the first Batch job. The total stays at $200
  or less. Keys get sealed and are never pasted into chat.

## Decisions after spec approval (2026-10-18/19)

These are Chris's answers during the plan reviews. Where they refine the
spec, they win.

**MCP registration (0a).**
- Registration on `mcp.chrismiller.xyz` is **open, with an exact-redirect
  allowlist plus Dex consent**.
- Until consent is live and verified, the stopgap stays: `/oauth/register`
  answers 403, as of commit `c3fbca0a`. It is lifted in an explicit commit
  afterwards.
- Dex **sessions are enabled** (`DEX_SESSIONS_ENABLED`), so consent is
  remembered.
- The DCR client "ChatGPT" (`7QITXD4SBM…`) is Chris's. It stays
  grandfathered and reconnects through the facade. The cluster holds no Dex
  refresh tokens today.

**Renovate PR #2662.** It bundles Dex 2.46 and oauth2-proxy 7.15.5. **Do not
merge it until 0a is done.** 0a splits those dependencies out of the
grouped PR.

**Phase 0b.**
- **No HDD OSD request cut.** Reclaim from Argo repo-server and immich-ml
  instead.
- **Alertmanager:** the weekday window moves to `America/Los_Angeles`.
- **Router syslog:** **fix the VyOS rsyslog receiver** through Ansible
  rather than dropping the output.
- **Satellites:** `.243` and `.244` are intentionally off, so their scrape
  targets are silenced.

**Phase 1.**
- The **layered task order is kept**. No walking-skeleton restructure.
- The nightly **Usage API reconcile runs in-cluster with an org Admin API
  key**, held in its own SealedSecret.
- **Workspaces:** only `luma-interactive` ($150) and `luma-ci` ($20) are
  created now.
- **Keys:** Chris pastes the Anthropic keys himself into the `*.secret.yaml`
  files the deploy plan names, then runs `./sign.sh`. Keys never go through
  chat.
- **Egress:** Discord is denied. Blocking pastebin and file-drop reads is
  accepted.
- `x-luma-*` headers reach Anthropic with **opaque ids only**.

## How to resume

1. Read this page, then the spec's §3 decisions and §14 for the phase you're
   in.
2. Open the plan for the current phase. Each one starts with Global
   Constraints and a Review Focus section, and its tasks are checkboxes.
   Ticked boxes show progress.
3. **Before executing any plan:** check the cluster state (S1), for example
   with the `cluster-snapshot` skill. Confirm the phase's gates above.
4. **Ways to execute:**
   - subagent-driven (`superpowers:subagent-driven-development`), with a
     fresh implementer and a reviewer per task;
   - native (`superpowers:executing-plans`).

   Chris wants **adversarial reviews of each plan before it is executed**,
   and a whole-branch review at the end.
5. **Writing the next phase's plan:** use `superpowers:writing-plans` with
   the spec, then review it (builder check plus adversarial reviewers), then
   update the table above.
6. **Questions for Chris** go through the AskUserQuestion tool, batched.

## Conventions

- **This repo:** commit straight to `main`, and run pre-commit inside
  `nix develop` (never `--no-verify`, S6). Secrets live in `*.secret.yaml`
  and are sealed with `./sign.sh`. Never quote them, and use `--no-textconv`
  on diffs that leave the terminal.
- **The `luma` repo** follows patchbay's pattern:
  - a flake dev shell, with CI as `nix develop -c make …`;
  - actions pinned by SHA, and a `ci-ok` aggregate check;
  - images `ghcr.io/christopherjmiller/luma-*:sha-<commit>`;
  - a Helm chart published like patchbay's, which this repo pins by
    `targetRevision`. Re-pinning an image is a second, manual commit (sharp
    edge #6).
- **Both product repos are public.** No secrets and no real family data go
  in them; evals use synthetic fixtures.
