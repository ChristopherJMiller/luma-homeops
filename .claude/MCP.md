# MCP servers for galaxy ops

This file documents Model Context Protocol servers worth wiring up for galaxy. **Nothing is enabled by default** — adding an MCP server gives Claude direct API access to that system, which has real implications. Opt in by adding the relevant block to `.mcp.json` at the repo root, or globally in `~/.claude/settings.json`.

## Recommended servers

### 1. Kubernetes (Flux159/mcp-server-kubernetes)
Lets Claude run structured kubectl operations against galaxy without going through Bash + text parsing. Inherits whatever access the configured kubeconfig has.

```json
{
  "mcpServers": {
    "kubernetes": {
      "command": "npx",
      "args": ["-y", "mcp-server-kubernetes"],
      "env": {
        "KUBECONFIG": "/tmp/galaxy-kubeconfig"
      }
    }
  }
}
```

**Auth:** Uses whatever kubeconfig is at `KUBECONFIG`. For galaxy, refresh first:

```bash
nix-shell shell.nix --run 'TALOSCONFIG=$PWD/nodes/talosconfig \
  talosctl -n 192.168.0.5 -e 192.168.0.5 kubeconfig --force /tmp/galaxy-kubeconfig'
```

**Risk:** The default kubeconfig is full admin. If that's too much trust to grant the MCP, create a read-only ServiceAccount and bind it to `view` ClusterRole, then generate a kubeconfig from that. Recommended for homelab convenience: just use the admin kubeconfig but use Claude's permission prompts as the second gate.

### 2. Grafana (grafana/mcp-grafana)
Lets Claude query Prometheus/Loki via PromQL/LogQL and inspect dashboards & alert state. Big leverage for "is this rollout healthy" or "show me the error rate" questions.

```json
{
  "mcpServers": {
    "grafana": {
      "command": "npx",
      "args": ["-y", "@grafana/mcp-grafana"],
      "env": {
        "GRAFANA_URL": "https://dashboard.chrismiller.xyz",
        "GRAFANA_API_KEY": "<service-account-token>"
      }
    }
  }
}
```

**Auth:** Mint a Grafana service account → grant `Viewer` role → create a token → paste into env var (don't commit). Read-only tokens are sufficient for query/dashboard inspection.

**Risk:** Read-only by token scope. Lowest-stakes MCP to add.

### 3. Argo CD (akuity/argocd-mcp or community variants)
List apps, sync status, app diff, optionally trigger sync. We mostly use Bash + `argocd` CLI today; MCP just gives the model structured tools instead of parsing text.

```json
{
  "mcpServers": {
    "argocd": {
      "command": "npx",
      "args": ["-y", "argocd-mcp"],
      "env": {
        "ARGOCD_SERVER": "ci.chrismiller.xyz",
        "ARGOCD_AUTH_TOKEN": "<cli-token>"
      }
    }
  }
}
```

**Auth:** `argocd account generate-token` → paste token into env var.

**Risk:** If the token has sync permissions, Claude can trigger Argo syncs. Limit to `read` permissions unless you want hands-off sync orchestration.

## How to enable one

1. Pick which server(s) you want to add
2. Mint the credential as documented above (or for kubernetes, just refresh kubeconfig)
3. Create `.mcp.json` at repo root with the relevant block (do NOT commit secrets — use env vars sourced from a non-committed file, or put the whole config in `~/.claude/settings.json`)
4. Restart the Claude Code session — MCP servers load on init
5. Verify with `/mcp` — the tools should appear under that server's name

## Why not enable by default?

- **Kubernetes** has full admin via kubeconfig — too much trust to wire up reflexively for a single-cluster prod-ish setup
- **Grafana / Argo** need credentials that don't exist yet
- Each MCP server adds tool surface area and per-message context — only worth the cost when actively used

We can revisit and enable selectively when the upgrade work or some other workflow wants the leverage.
