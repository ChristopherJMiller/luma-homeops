# NetworkPolicy enforcement on galaxy: design and rollout plan

- **Date:** 2026-11-03
- **Status:** proposed. Nothing in it has been executed, and it needs Chris's review.
- **Supersedes:** the "documented intent only" stance in
  `docs/superpowers/specs/2026-11-02-forge-bot-and-ci-design.md`, where the runner gets no policy and
  forge-bot's policy is not a control.
- **Implements early:** Luma spec §13.1 (D25), "kube-network-policies via GitOps, dormant policies
  validated first". The forgejo-runner pulls it forward from Luma phase 4. Choices in this doc match
  §13.1 unless it says otherwise.
- **Related:** #2506 (Talos/k8s upgrade sequence), CLAUDE.md S2/S4/S5.

## 1. Recommendation

Run **upstream kube-network-policies (v1.1.3 or later, pinned by digest) as an Argo-managed
DaemonSet in `kube-system`, beside the Talos-managed flannel**. Leave the CNI, kube-proxy and the
Talos machine config alone, and **do not turn on Talos's built-in `kubeNetworkPoliciesEnabled`**,
either now or during the #2506 hops.

It is the only option that adds enforcement without touching the pod network. It only enforces for
pods a policy selects: with no policy present it queues no packets, so installing it does nothing
we can't measure. It is the same engine Talos chose for its own flannel option, so the policy
semantics stay the same if we ever switch to the built-in copy. Rollback is a git revert, or a
`kubectl delete` when Argo can't help.

The first real policy is the forgejo-runner egress set in §6.3. Then comes a slow, per-namespace
adoption that never touches `rook`, `kube-system`, `metallb-system` or `prometheus`.

Cilium is the better end state on paper: host firewall, FQDN rules, Hubble flows and ClusterNetworkPolicy.
But a live CNI migration on three control-plane-only nodes, with Ceph on the pod network and six
Talos/k8s hops queued, is the riskiest change this cluster could take. Revisit it at a rebuild, not
now (§4.4, §10).

## 2. Why now

The forgejo-runner (namespace `forgejo-runner`, host mode, being deployed now) runs CI code from
any `realliance` member as uid 1000 in a pod. Today that code can reach:

- every ClusterIP Service: Forgejo, Postgres, the Argo repo-server, mcp-gateway, and the rest;
- through flannel SNAT, the whole LAN, including the BMCs (`.117`, `.119`, `.151`, per
  `cluster/ipmi-exporter/scrapeconfig.yaml`) and the unpatched ONE KVM at `.2`;
- through NAT reflection on the router's WAN address, router-local services. See §3.4: the
  zigbee2mqtt frontend on `:8585` is reachable this way even if RFC1918 is blocked.

More runners and less-trusted workloads are coming: Luma `luma-sandbox` and `luma-browser`. We need
an enforcement point that standard NetworkPolicy YAML can target, and that we can adopt one
namespace at a time.

## 3. Current state and audit (verified read-only, 2026-11-03)

### 3.1 Platform facts

| Item | Value | How it was checked |
|---|---|---|
| Talos / k8s / kernel | v1.10.9 / v1.33.13 / 6.12.63-talos, containerd 2.0.7 | `kubectl get nodes -o wide`, `talosctl version` |
| CNI | Talos-managed flannel `ghcr.io/siderolabs/flannel:v0.26.7` (vxlan, `--ip-masq`), plus portmap | DaemonSet `kube-system/kube-flannel` |
| kube-proxy | **nftables mode** (`--proxy-mode=nftables`, `--cluster-cidr=10.244.0.0/16`) | DaemonSet args |
| Pod CIDR | `10.244.0.0/16`. Nodes: bottom `10.244.0.0/24`, top `10.244.3.0/24`, middle `10.244.4.0/24` | kube-controller-manager args, `node.spec.podCIDR` |
| Service CIDR | `10.96.0.0/12`. The `kubernetes` Service is `10.96.0.1`, kube-dns is `10.96.0.10` | kube-apiserver `--service-cluster-ip-range` |
| Node addresses | `192.168.0.240` top (plus VIP `.5`), `.241` middle, `.242` bottom. flannel.1 is `10.244.X.0/32`, cni0 is `10.244.X.1/24` | `talosctl get addresses` |
| Kernel features | `NFT_QUEUE`, `NETFILTER_NETLINK_QUEUE`, `NF_CONNTRACK_LABELS`, `NFT_CT` built in (`=y`), and `/proc/net/netfilter/nfnetlink_queue` is present | siderolabs/pkgs `release-1.10` kernel config, `talosctl ls` |
| containerd NRI | **off** (`/var/run/nri` does not exist) | `talosctl ls` |
| Policy engine | none: no Cilium, Calico, kube-router or Antrea | `kube-system` pod list, CRDs |
| Pod Security | Namespaces opt in with `pod-security.kubernetes.io/enforce` labels. `kube-system` has none | namespace labels |
| Node taints | none (all three are control-plane and schedulable) | `kubectl get nodes` |
| Free host port for metrics | `:9080` is free on the nodes | `talosctl netstat -l -t` |

### 3.2 The seven existing (inert) policies

| Policy | Owner | Selects today | Allows | If enforced unchanged |
|---|---|---|---|---|
| `metallb-system/metallb-release-controller` | **nobody.** It is left over from the Bitnami `metallb-6.3.16` chart (v0.14.8), carries only the old label-tracking `argocd.argoproj.io/instance`, and appears in neither the `metallb` nor the `metallb-release` app's resource list | **nothing**: it selects `instance=metallb-release`, but the live pods carry `instance=metallb` (chart 0.15.x, release `metallb`) | ingress TCP 7472 and 9443 from anywhere, all egress | Nothing. But if someone "fixed" the selector, it would start restricting the controller, and an enforcer at v1.1.0 or older would have the speaker bug below |
| `metallb-system/metallb-release-speaker` | nobody (as above) | **nothing** (same mismatch). The live speaker DaemonSet `metallb-speaker` is `hostNetwork` | the same | Nothing. **Trap:** kube-network-policies **v1.1.0 and older** put the node IPs of matched `hostNetwork` pods into the enforcement set ([#380](https://github.com/kubernetes-sigs/kube-network-policies/issues/380)). If this selector ever matched, every non-root packet to or from that node would be queued and judged as speaker traffic. Fixed in v1.1.1 ([#389](https://github.com/kubernetes-sigs/kube-network-policies/pull/389)) |
| `argo-cd/argo-install-argocd-server` | Argo CD chart 10.9.2 (`global.networkPolicy.create: true` is the chart default) | `argocd-server` (1 pod) | all ingress (`ingress: [{}]`) | Nothing |
| `argo-cd/argo-install-argocd-application-controller` | the chart | controller-0 | ingress TCP `metrics` (8082) from any pod in any namespace | Nothing in steady state. Nothing else dials the controller. Prometheus is a pod, so it is allowed. Kubelet probes come from root on the node, which is exempt (§3.4) |
| `argo-cd/argo-install-argocd-applicationset-controller` | the chart | appset controller | ingress `metrics` (8080) from any pod | Port `webhook` (7000) becomes unreachable. That is harmless: no SCM webhook or Ingress points at it. Probe port 8081 is used only by kubelet |
| `argo-cd/argo-install-argocd-repo-server` | the chart | both repo-servers | ingress `repo-server` (8081) from server, app-controller and appset; `metrics` (8084) from any pod | Nothing. These three are the only real clients |
| `argo-cd/argo-install-argocd-redis` | the chart | redis (2 containers) | ingress `redis` (6379) from server, repo-server and app-controller; `metrics` (9121) from any pod | Nothing. I checked the `REDIS_*` env: exactly those three components use Redis. The appset controller does not, and notifications and dex are not deployed |

The Luma spec counts six argo-cd policies. There are five now, because Dex is disabled.

None of the seven has an Egress section that restricts anything. The Argo set is correct, and it is
worth having: a pod that can dial the repo-server can ask it to render manifests with Argo's
repository credentials. But Argo is also the **rollback path**, so step A2 switches the set off
until the end of the rollout (§7).

### 3.3 Host-network pods (never subject to NetworkPolicy)

`kube-system`: apiserver, controller-manager, scheduler, kube-flannel and kube-proxy (×3 each).
`metallb-system`: speaker ×3. `prometheus`: node-exporter ×3. `rook`: the CephFS and RBD CSI
nodeplugins (×3 each). `telephony`: `patchbay-pbx` (Asterisk on `top`).

Ceph **mons and OSDs are on the pod network** (34 pods in `rook`), and so are Traefik, CoreDNS,
Prometheus and every webhook.

### 3.4 What the enforcer actually sees

These facts come from reading kube-network-policies' dataplane (v1.0.0 to v1.1.3) and this
cluster's addresses. They decide every policy below.

1. **Where it hooks.** The `postrouting` chain is a filter chain at priority `srcnat − 5`: after
   DNAT, before SNAT. DNS on UDP 53 is judged in `prerouting` at `dstnat + 5`, also after DNAT
   ([controller.go](https://github.com/kubernetes-sigs/kube-network-policies/blob/v1.0.0/pkg/dataplane/controller.go)).
   - **Source** is the real pod IP. flannel's and kube-proxy's masquerade happen later.
   - **Destination** is the backend after kube-proxy DNAT. A rule for a Service must therefore
     select the **backend pods** (for DNS: `kube-system`, `k8s-app=kube-dns`, port 53), not
     `10.96.x.x`.
   - kube-proxy answers **LoadBalancer IPs (.6/.7/.8/.230–.232) from inside the cluster**. A pod
     that dials `192.168.0.7` is DNATed to a Traefik pod IP, so an `ipBlock` for `.7` never
     matches, and "deny 10.0.0.0/8" does block it.
2. **Only selected local pods are queued.** Each node puts into an nftables set the IPs of its
   **local** pods that at least one policy selects. Only packets whose source or destination is in
   that set go to NFQUEUE (`ManagedIPs`). With no policies, the set is empty and nothing is queued.
   Ceph, Traefik and the rest are untouched until a policy selects them.
3. **Root on the node is exempt; other host processes are not.** The chain accepts
   `meta skuid 0` (kubelet probes and root daemons) before the queue rule.
   - **kube-apiserver runs as uid 65534** (Talos static pod `runAsUser: 65534`), so its calls to
     webhooks are judged.
   - Kernel sockets (krbd and kcephfs, the CSI mounts) have no uid, so they are judged too.
4. **Host-originated sources depend on the path.** For a non-root host process dialing a pod:
   - to a pod on the **same node**, the policy sees the **node IP** (`192.168.0.24x`), before
     kube-proxy's masquerade;
   - to a pod on **another node**, it sees that node's **flannel.1 address** (`10.244.0.0`,
     `10.244.3.0`, `10.244.4.0`). kube-proxy masquerades host→ClusterIP traffic whose source is
     outside the cluster CIDR, and the receiving node judges the packet;
   - direct host→local-pod traffic leaves from cni0 (`10.244.X.1`).

   An ingress rule for the API server (webhooks) or for kernel Ceph clients needs **all nine
   addresses**. That list is the `allow-from-nodes` component (§6.2). Validate it with deny logs
   before relying on it.
5. **`ipBlock` also matches pod and Service IPs** (`evaluateIPBlocks` runs on every destination).
   An "internet" rule therefore has to `except` `10.0.0.0/8`, which covers both cluster CIDRs.
6. **Hairpin through the WAN IP.** `git.realliance.net` and every `*.chrismiller.xyz` name resolve
   to the WAN address `152.44.247.88` (DHCP, in `152.44.247.0/25`).
   - A pod's packet to it leaves with the pod IP, is masqueraded to the node IP, and the router
     reflects `:80`, `:443` and `:22` to `.7`/`.6` (NAT rules 210/310/410).
   - **Any other port on the WAN address is router input**, and the router accepts some of it from
     `NET-INSIDE`: DNS on 53, and **zigbee2mqtt on `:8585`** (input rules 40 and 60; source is the
     node IP after masquerade).
   - So "0.0.0.0/0 except RFC1918" still lets a CI job drive the Zigbee frontend. The runner policy
     carves out the WAN /25 and allows it only on TCP 443.
7. **Fail-open, and its windows.** The default is `--fail-open=true`. The nfqueue rule carries
   `bypass`, so when the agent is gone, queued packets pass and the rules are left in place.
   - With `--fail-open=false`, a graceful shutdown **deletes** the rules, so traffic passes anyway
     during restarts. Only a crash blackholes.
   - Either way, **enforcement has a gap whenever the agent on a node restarts** (rollouts, node
     reboots).
   - Without NRI, there is also a gap while a **new pod's IP** is missing from the informer.
8. **Strict mode is on by default.** A policy change re-judges established connections
   immediately: allowed ones survive, newly denied ones are cut. Before v1.1.3, replies on
   connections opened before the agent started could be dropped
   ([v1.1.3 notes](https://github.com/kubernetes-sigs/kube-network-policies/releases/tag/v1.1.3), #418).

### 3.5 Traffic that needs care when a namespace adopts policy

| Flow | As seen by the enforcer | Rule |
|---|---|---|
| Kubelet probes → pods | root on the node: exempt | none needed |
| API server → webhooks (`dns/cert-manager-webhook`, `metallb-system/metallb-webhook-service`, `rabbitmq-system` ×2, `prometheus/prometheus-kube-prometheus-operator`). Four of them are `failurePolicy: Fail` | node IP or flannel/cni0 address (§3.4 item 4) | `allow-from-nodes` component on the webhook port. **A missing rule breaks admission cluster-wide** |
| Traefik → backends (every Ingress, in 19 namespaces) | Traefik pod IPs (`traefik` ns, `app.kubernetes.io/name=traefik`) | allow from namespace `traefik` |
| Traefik forwardAuth and errors → oauth2-proxy (4 forwardAuth and 3 errors middlewares) | Traefik pods → `oauth2-proxy` pods :4180/:4181 | allow from `traefik` in `oauth2-proxy`. Leave Traefik's egress unrestricted |
| LAN clients → LoadBalancer Services with `externalTrafficPolicy: Cluster` (`traefik`, `forgejo-ssh`, `mosquitto`, `registry`) | client IP if the backend is local; **node or flannel address if it was forwarded** | you cannot tell WAN from LAN here. Don't write source-IP ingress rules on these |
| LB with `etp: Local` (`ops/wg-gateway`, `rook/...nfs-satellites-lb`) | real client IP | source-IP rules are possible |
| Prometheus → targets (13 ns with ServiceMonitors, 4 with PodMonitors, plus pod annotations) | Prometheus pod in `prometheus` | allow from namespace `prometheus` on the metrics port. Leave Prometheus's egress open (it scrapes node IPs and BMCs through ipmi-exporter) |
| ipmi-exporter → BMCs | `prometheus` pod → `.117`, `.119`, `.151` | if `prometheus` ever gets egress policy, this is its one LAN exception |
| Argo controller/server/repo-server → API server, GitHub, Helm/OCI repos | egress | leave Argo's egress open. Its ingress is the chart set |
| Ceph kernel clients (krbd/kcephfs) → mons/OSDs | node and flannel addresses (no socket uid) | **never put policy on `rook`** without `allow-from-nodes`. Out of scope |
| MetalLB memberlist 7946 and speaker metrics 7472 | host network | unaffected |
| Patchbay operator → Asterisk (hostNetwork on `top`) | pod → node IP | egress in `telephony` needs `ipBlock 192.168.0.240/32` |
| HA → LAN devices, mosquitto `.8` | pod → LAN | leave `home-assistant` egress open |
| Runner job → its own cache server | pod-local, over loopback inside the pod's netns | never reaches nftables |

## 4. Options

### 4.1 (a) kube-network-policies, CNI-agnostic, nftables + NFQUEUE

**What it is.** A kubernetes-sigs project from SIG Network, by Antonio Ojea.

- It judges the first packet of each connection in userspace (NFQUEUE), then marks it with a
  conntrack label.
- kind's kindnet embeds it ([kind#3612](https://github.com/kubernetes-sigs/kind/pull/3612)), and
  Talos chose it for flannel.
- Activity: v1.0.0 (2025-11), v1.1.0 (2026-06), v1.1.1 (07-28), v1.1.2 (09-28), v1.1.3 (11-02).
  One open issue.
- Image flavours
  ([flavours doc](https://github.com/kubernetes-sigs/kube-network-policies/blob/v1.1.3/site/content/docs/user/flavors-and-architectures.md)):
  `standard` (NetworkPolicy), `npa-v1alpha1` (ANP/BANP), `npa-v1alpha2` (ClusterNetworkPolicy)
  and `iptracker`.

**Talos built-in.** Talos **v1.13.0** (2026-04-27) added this to the legacy machine config:

```yaml
cluster:
  network:
    cni:
      name: flannel
      flannel:
        kubeNetworkPoliciesEnabled: true
```

- On a running cluster you then sync the bootstrap manifests (`talosctl upgrade-k8s` to the
  current version).
- Talos v1.14+ uses a `KubeFlannelCNIConfig` document with `kubeNetworkPoliciesEnabled: true`.
- Sources: [v1.13.0 release notes](https://github.com/siderolabs/talos/releases/tag/v1.13.0),
  [Talos flannel doc](https://docs.siderolabs.com/kubernetes-guides/cni/flannel),
  [implementing commit](https://github.com/siderolabs/talos/commit/87615f551183cd322dafebf368a347d928a14442).

It adds a `kube-network-policies` sidecar to the kube-flannel DaemonSet, with
`--hostname-override` and `--v=2` and nothing else. The flavour is `standard`, and **the image tag
is a Talos constant (`KubeNetworkPoliciesVersion`), not a config field**:

| Talos | bundled kube-network-policies | #380 hostNetwork bug |
|---|---|---|
| 1.13.0 – 1.13.5 | v1.0.0 | **present** |
| 1.13.8 – 1.13.12 (latest 1.13) | v1.1.0 | **present** |
| 1.14.0 – 1.14.2 | v1.1.1 | fixed |
| 1.14.3 – 1.14.5 | v1.1.2 | fixed, but lacks the four v1.1.3 datapath fixes |

So **the built-in option is unsafe on every Talos 1.13.x**: a `podSelector: {}` default-deny in any
namespace with a hostNetwork pod enrols a node IP. It is not even reachable until hop 3 of #2506.

How (a) fares against each criterion:

- **Egress `ipBlock` versus SNAT, LB and hostPort.** See §3.4.
  - Judged after DNAT and before SNAT, which is the clean case: the source is the pod, the
    destination is the real backend.
  - In-cluster LB traffic resolves to pods.
  - hostPort traffic is DNATed by portmap in PREROUTING, so it is judged as the pod (none in use
    today).
- **ANP/BANP/CNP.** Only in the `npa-*` flavours. Turning on any admin tier sets `divertAll`:
  **every packet on the node** goes through userspace, Ceph included. Not in the Talos built-in.
- **Host traffic.** Not policed. HostNetwork pods are skipped (v1.1.1+), and root on the node is
  exempt.
- **Cost.** Requests are 100m CPU / 50Mi per node upstream. The only per-packet cost is for
  policed pods.
- **Rollback.** Delete the DaemonSet: with fail-open, `bypass` lets traffic through. Or delete the
  policies: the set empties. Both take seconds.
- **GitOps.** An Argo app with vendored manifests (the upstream Helm chart is stale: it passes
  `--admin-network-policy` flags that the v1.x `standard` binary does not define). Or the Talos
  machine config (S4, with no flags or pinning of our own).

### 4.2 (b) kube-router in firewall-only mode beside flannel

**What it is.** kube-router's Network Policy Controller, run as
`--run-firewall=true --run-router=false --run-service-proxy=false --enable-cni=false`
([user guide](https://kube-router.io/docs/user-guide/)).

- It is maintained: v2.12.0 shipped 2026-11-02.
- k3s ships this exact combination by default: flannel plus kube-router's netpol library
  ([k3s docs](https://docs.k3s.io/networking/networking-services)). That is the strongest maturity
  signal of any option here.

**Drawbacks:**

- **iptables + ipsets.** Its nftables backend is **experimental** (`--use-nftables-for-netpol`,
  [v2.11.0](https://github.com/cloudnativelabs/kube-router/releases/tag/v2.11.0)), while our
  kube-proxy runs nftables. Mixing them works, but adds a second rule framework to debug.
- **Rollback is dirty.** k3s documents that its rules are *not* removed when the controller is
  disabled. Stale DROP rules survive until they are flushed by hand or the node reboots.
- **No ANP or CNP.**
- **No Talos documentation.**
- A past `--run-router=false` CNI bug
  ([#1434](https://github.com/cloudnativelabs/kube-router/issues/1434)).
- It programs every pod's chains, not just policed ones, so it touches the whole datapath from
  day one.

How (b) fares against each criterion:

- **Semantics.** Like (a): judged in FORWARD, after DNAT and before SNAT.
- **Cost.** One agent per node, typically somewhat heavier than (a).
- **GitOps.** An Argo DaemonSet.

Verdict: workable, but it is (a) with a worse rollback and no admin-policy path.

### 4.3 (c) Calico policy-only / Canal

Felix needs to own the pod interfaces.

- Its "policy-only" modes exist for CNIs whose veth names Felix can predict (AWS `eni*`, Azure
  `azv*`). Talos's flannel uses the bridge plugin, whose veths have random names, so
  **Calico cannot police the existing flannel pods**.
- The supported route is **Canal**: Calico's CNI plugin with flannel networking
  ([Tigera doc](https://docs.tigera.io/calico/latest/getting-started/kubernetes/flannel/install-for-flannel)).
  That **replaces the CNI config**: `cluster.network.cni.name: none` in Talos, every pod
  re-created, and the same blast radius as (d) with fewer benefits.

How (c) fares against each criterion:

- **nftables dataplane.** GA since 3.31 and requires kube-proxy in nftables mode, which we have
  ([nftables doc](https://docs.tigera.io/calico/latest/getting-started/kubernetes/nftables)).
- **Admin policy.**
  - ANP arrived in 3.29.
  - **3.32 removed ANP/BANP**: they silently stop being enforced, replaced by ClusterNetworkPolicy
    v1alpha2 ([3.32 post](https://tigera.io/blog/whats-new-in-calico-v3-32/),
    [upgrade notes](https://docs.tigera.io/calico/latest/operations/upgrading/upgrade-notes)).
  - Calico's own GlobalNetworkPolicy and tiers are the mature cluster-wide tool.
- **Host traffic.** Via HostEndpoints.
- **Cost.** calico-node per node, plus kube-controllers and possibly Typha. Hundreds of MiB.
- **Rollback.** Another CNI swap.
- **GitOps.** The Tigera operator via Argo, plus a Talos machine-config change.

Verdict: no.

### 4.4 (d) Migrate to Cilium

**Talos.** Supported and documented
([Talos Cilium guide](https://docs.siderolabs.com/kubernetes-guides/cni/deploying-cilium)):

- `cluster.network.cni.name: none` (on v1.14+, delete `KubeFlannelCNIConfig`);
- for kube-proxy replacement, also `cluster.proxy.disabled: true` plus KubePrism `localhost:7445`;
- `cgroup.autoMount.enabled=false`, and no `SYS_MODULE`.

That guide covers **new clusters only**. It also warns that Talos only creates missing resources
from inline manifests and never updates or deletes them.

**Live migration.** Cilium's documented dual-overlay procedure
([migration doc](https://docs.cilium.io/en/stable/installation/k8s-install-migration/)):

- a **new pod CIDR** (for example `10.245.0.0/16`) and a different VXLAN port;
- Cilium installed with `cni.customConf`, `policyEnforcementMode: never` and
  `bpf.hostLegacyRouting`;
- then, **per node**: cordon, drain, label, restart the agent, **reboot**, validate, uncordon;
- NetworkPolicy is disabled for the whole migration.

**Here that means:**

- three drain-and-reboot cycles of control-plane nodes, each moving Ceph OSDs and mons (5–30 min
  each, plus recovery on SMR);
- Talos `podSubnets` and kube-proxy `--cluster-cidr` would no longer match pod IPs;
- removing flannel by hand, because Talos does not delete it;
- re-checking MetalLB L2, the CephNFS LB and the PBX host networking;
- doing it interleaved with the etcd 3.6 hop.

Every step is S4/S5.

**What you get:**

- eBPF dataplane, with kube-proxy replacement optional;
- **host firewall**: the only option here that can police node and host-network traffic to the
  BMCs and KVM;
- `toFQDNs` egress;
- CiliumClusterwideNetworkPolicy;
- Kubernetes ClusterNetworkPolicy from **1.20** behind `k8sClusterNetworkPolicy.enabled`
  ([Cilium policy doc](https://docs.cilium.io/en/latest/network/kubernetes/policy/));
- Hubble flow visibility, which is exactly what writing policies for 40 namespaces needs.

**Semantics differ.** `toCIDR`/`ipBlock` does not match pods or nodes by default (identities do;
see `policy-cidr-match-mode`).

**Cost.** Agent ~300–500 MiB per node plus the operator (plus Hubble relay). That matters at ~84%
memory requested.

**Rollback.** A node-by-node migration back.

Verdict: revisit at a natural rebuild (§10), not as a fix for one runner.

### 4.5 Comparison

| | (a) kube-network-policies (Argo) | (a′) Talos built-in | (b) kube-router NPC | (c) Canal | (d) Cilium |
|---|---|---|---|---|---|
| Changes the CNI or pod network | no | no | no | **yes** | **yes** |
| Available on Talos 1.10.9 | **yes** | no (≥1.13; safe only ≥1.14) | yes | yes, with a CNI swap | yes, with a CNI swap |
| Touches the datapath with zero policies | chains installed, nothing queued (empty set) | same | yes (chains for every pod) | yes | yes |
| `ipBlock` semantics | post-DNAT, pre-SNAT; matches pod IPs | same | same | same | identity-based; CIDR ≠ pods/nodes |
| Admin / cluster-wide policy | `npa-v1alpha2` flavour (divertAll) | none | none | CNP (3.32+), GlobalNetworkPolicy | CCNP, KCNP (1.20+) |
| Polices host traffic | no | no | no | HostEndpoints | host firewall |
| Rollback | revert, or delete the DaemonSet (seconds) | machine config + sync | stale rules remain | CNI swap | CNI swap |
| Deployed by | Argo | Talos machine config (S4) | Argo | Argo + machine config | machine config + inline or Argo |
| Cost per node | ~50–100 Mi | same | moderate | high | high |

## 5. Decision

Option **(a), Argo-managed**.

- **Version.** kube-network-policies **v1.1.3+**, `standard` flavour, pinned by digest. **Never
  below v1.1.1** (#380). v1.1.3 is one day old, and the no-op install and canary (A3, B1) are its
  soak. If it regresses, v1.1.2 is the fallback, with the #418 caveat in §3.4 item 8.
- **Talos built-in: never alongside ours.** Two agents would each judge every packet. At Talos
  1.13 it is unsafe. At 1.14+ it would trade our flags, pinning and priority class for one fewer
  Argo app. **Stay Argo-managed.** If that ever changes, delete our DaemonSet first.
- **No admin tier now.** Cluster-wide guardrails (no pod reaches BMCs or the KVM) are not
  achievable with `standard`. Options for later are in §10. The per-namespace runner policy covers
  the current threat.

## 6. Design

### 6.1 The enforcer: `cluster/kube-network-policies/` (new Argo app, namespace `kube-system`)

The manifests are vendored from upstream `install.yaml` at the pinned tag (ServiceAccount,
ClusterRole for pods, namespaces and networkpolicies `list`/`watch`, and the binding). A kustomize
patch on top:

```yaml
# cluster/kube-network-policies/daemonset-patch.yaml (outline)
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: kube-network-policies
  namespace: kube-system
spec:
  updateStrategy:
    rollingUpdate: {maxUnavailable: 1}      # one node at a time (S5); each restart is a short fail-open gap
  template:
    spec:
      hostNetwork: true
      priorityClassName: system-node-critical   # memory is ~84% requested; this must never be preempted
      tolerations:
        - operator: Exists
      containers:
        - name: kube-network-policies
          image: registry.k8s.io/networking/kube-network-policies:v1.1.3@sha256:<pin>
          args:
            - /bin/netpol
            - --hostname-override=$(MY_NODE_NAME)
            - --v=1                    # Luma §13.3: keep router syslog and Azure quiet
            - --log-verdicts=deny      # one line per denied packet (v1.1.3+)
            - --fail-open=true         # explicit; see §3.4 item 7
            - --nfqueue-id=98
            - --metrics-bind-address=:9080
            - --disable-nri=true       # NRI is off on Talos; turn this off again if NRI is ever enabled
          ports:
            - {name: metrics, containerPort: 9080}
          resources:
            requests: {cpu: 50m, memory: 64Mi}
            limits: {memory: 256Mi}
      # Drop upstream's /var/run/nri hostPath (absent on Talos). Keep /var/run/netns.
```

Alongside it:

- **Argo Application** `kube-network-policies`: automated with prune and selfHeal, and
  `ServerSideApply`.
- **PodMonitor** (`release: prometheus`) on port `metrics`.
- **PrometheusRule** (`release: prometheus`, namespace `prometheus`). Rules ship as CRDs, not
  ConfigMaps, or they never load:
  - `KubeNetworkPoliciesAgentNotReady`: DaemonSet ready < desired for 5m;
  - `KubeNetworkPoliciesQueueDrops`: `increase(kube_network_policies_nfqueue_queue_dropped[10m]) > 0`
    or `nfqueue_user_dropped`;
  - `KubeNetworkPoliciesVerdictLogDropped`;
  - `NetpolCanaryFailing` (§6.4).
- **Renovate.** Add `registry.k8s.io/networking/kube-network-policies` to the
  `"automerge": false` rule for controllers with authority, digest bumps included. A bump changes
  the datapath on every node.

### 6.2 Policy conventions

- **Reusable kustomize components** in `cluster/components/netpol/`:
  - `default-deny`: `podSelector: {}`, Ingress + Egress;
  - `allow-dns`: egress to `kube-system`/`k8s-app=kube-dns`, UDP and TCP 53;
  - `egress-internet-only`: the `ipBlock` in §6.3;
  - `allow-from-traefik`;
  - `allow-from-prometheus`;
  - `allow-from-nodes`: the nine host-source addresses from §3.4 item 4, as `/32`s.

  If a node is rebuilt with a new podCIDR, edit `allow-from-nodes` (one file).
- **Never select pods in:** `kube-system`, `rook`, `metallb-system`, `prometheus` or `argo-cd`
  (except the chart's own set, step B4). Also not in any namespace with a `failurePolicy: Fail`
  webhook until `allow-from-nodes` has been proven against deny logs. That rules out `dns` and
  `rabbitmq-system` for now.
- **Default-deny** means `policyTypes: [Ingress, Egress]`. A namespace adopts egress restriction
  only when someone has listed its dependencies.
- **Allow by selector.** Select pods and namespaces, never ClusterIPs or LB IPs (§3.4 item 1).
- **The policy is what the pod needs.** Describe it in a comment on every policy.

### 6.3 First real policy set: `forgejo-runner`

Add this as `cluster/forgejo-runner/networkpolicy.yaml` and list it in that kustomization. Land it
only after the runner's own rollout is finished and verified, and coordinate with whoever owns that
deployment. The namespace has no Service, no probes and no scrape, so nothing needs ingress.

```yaml
---
# Default-deny for everything in the namespace: the runner now, job pods if it ever leaves host mode.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: forgejo-runner
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
---
# CI egress: DNS, the forge through the router's NAT reflection, and the public internet.
# Never the cluster (pods, Services, LB IPs, nodes), the LAN (BMCs, KVM, router, switch), the
# tailnet, or the router's own services on the WAN address.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: runner-egress
  namespace: forgejo-runner
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    # DNS. Policy sees the CoreDNS pod IPs (post-DNAT), not the 10.96.0.10 ClusterIP.
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - {protocol: UDP, port: 53}
        - {protocol: TCP, port: 53}
    # git.realliance.net (registration --instance, checkout, package pushes) resolves to the WAN
    # address; the router reflects :443 to Traefik .7. The whole DHCP /25 is listed, so a lease
    # change inside it doesn't break CI; only 443, because other ports on it are router input
    # (zigbee2mqtt :8585, DNS :53).
    - to:
        - ipBlock:
            cidr: 152.44.247.0/25
      ports:
        - {protocol: TCP, port: 443}
    # The public internet: GitHub actions (DEFAULT_ACTIONS_URL), Go proxy, npm, B2.
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 0.0.0.0/8
              - 10.0.0.0/8         # pods 10.244.0.0/16, Services 10.96.0.0/12
              - 100.64.0.0/10      # CGNAT, the tailnet behind the router
              - 127.0.0.0/8
              - 169.254.0.0/16     # link-local, metadata-style addresses
              - 172.16.0.0/12
              - 192.168.0.0/16     # LAN, nodes, BMCs .117/.119/.151, KVM .2, voice VLAN, switch OOB, LB IPs
              - 224.0.0.0/4        # multicast
              - 240.0.0.0/4
              - 152.44.247.0/25    # our WAN /25: 443 only, above
```

What this does **not** cover, and the residual risk:

- DNS lookups through CoreDNS can still leak data, and the jobs can still talk to the open
  internet. This is the forge-bot spec's accepted risk.
- **A job running when the agent on its node restarts** has unenforced egress for a few seconds.
- **The runner's first seconds after scheduling** before its IP is enrolled (no NRI). Close this
  with an init-container gate in the runner Deployment. That is a proposal for the runner's
  owner:

  ```yaml
  initContainers:
    - name: netpol-gate   # don't register or take jobs until this pod's egress is enforced
      image: <the runner image>       # it has node
      command:
        - sh
        - -c
        - |
          until node -e 'const s=require("net").connect({host:"10.96.0.1",port:443,timeout:3000});
            s.on("connect",()=>process.exit(1));s.on("timeout",()=>process.exit(0));
            s.on("error",()=>process.exit(1))'; do
            echo "egress not enforced yet; waiting"; sleep 3
          done
  ```

  A timeout means the packet was dropped, so the policy is live and the gate passes. A connect,
  or any error such as a refusal, means it is not enforced yet, so the gate keeps waiting.
- **If the WAN address leaves `152.44.247.0/25`**, the runner can't reach the forge, which fails
  closed and is noticed at once. The new address's router ports are then open to the runner
  again, a regression to today's exposure. The fix is to update both CIDRs. The real fix is the
  router follow-up in §10, which needs no IP in the policy.

### 6.4 Canary: `cluster/netpol-canary/` (permanent)

**Shape.** A DaemonSet `netpol-probe`, one pod per node because each node's agent is a separate
enforcer. Plus a one-replica echo server, and the same components as the runner:

- `default-deny`;
- `allow-dns`;
- `egress-internet-only`;
- egress to the echo server;
- ingress to the echo server from the probe only.

**What the probe does.** It loops every 60s over a fixed matrix with
`agnhost connect --timeout=3s` (the tool upstream NetworkPolicy e2e uses) or `nc`. It is ready only
while the last run matched expectations. `NetpolCanaryFailing` fires when ready < desired for 10m.

**It is a DaemonSet or Deployment, not a CronJob.** Completed Job pods keep their IPs in the API,
which without NRI can confuse IP→pod mapping (#414).

| Must succeed | Must time out (dropped) |
|---|---|
| DNS lookup of `kubernetes.default.svc.cluster.local` | `192.168.0.117:443`, `.119:443`, `.151:443` (BMCs) |
| echo server (same namespace) | `192.168.0.2:80` and `:443` (KVM) |
| `git.realliance.net:443` (via the WAN /25) | `192.168.0.1:22` (router SSH) |
| `1.1.1.1:443` | `10.96.0.1:443` (API ClusterIP) and `192.168.0.240:6443` |
| | `forgejo-http.forgejo.svc.cluster.local:3000` (ClusterIP) |
| | `192.168.0.7:443` (Traefik LB), `.8:1883` (mosquitto), `.230:2049` (NFS), `.231:5000` (registry) |
| | `192.168.0.240:50000` (Talos apid) |
| | `152.44.247.88:8585` (zigbee2mqtt through the WAN address) |

A connection **refused** counts as a failure: a RST means the packet got through.

**Baseline first.** Before B1's policies go in, run the matrix once with no policy and record that
every must-time-out target actually answers today. A target that is down would give a false pass.

The prober is also the **post-hop regression gate for #2506**: after every Talos or k8s step it must
be green on all three nodes (§8).

### 6.5 Break-glass (needs pre-approval as an S2/S4 exception)

Use this when a policy or the agent breaks something and Argo can't sync the revert, because Argo
itself is affected or the fix is urgent. In order, least first:

1. **One bad policy.** Revert it in git and push. If Argo can't apply that:
   1. pause the root app's auto-sync, or it will undo the next step:
      `kubectl -n argo-cd patch application applications --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}'`;
   2. pause the owning app's auto-sync the same way;
   3. `kubectl -n <ns> delete networkpolicy <name>`.
2. **All enforcement off, agent kept.** Delete every NetworkPolicy that Argo isn't about to
   re-create (pause the apps as in step 1). The sets empty within a sync, so nothing is queued.
3. **Agent off.** Pause `applications` and `kube-network-policies`, then
   `kubectl -n kube-system delete daemonset kube-network-policies`. With fail-open the nfqueue
   rules stay, flagged `bypass`, so traffic passes. The nftables table lingers, harmless, until
   each node's next reboot.
4. **Afterwards.** Fix the repo, re-enable auto-sync on the child app, then on `applications`, and
   let Argo reconcile.

## 7. Rollout plan

Read `cluster-snapshot` before every step and save it (S1). Record the baseline:

- Ceph's usual `HEALTH_WARN`;
- the `rook` and `rook-cluster` apps report Degraded in Argo today;
- the edge-ingress 25-host snapshot.

**Never run a step during a Talos/k8s hop**, a node drain or Ceph recovery (S5).

| Step | Change (all via git, except where marked) | Watch | Success | Roll back if | Confirm first? |
|---|---|---|---|---|---|
| **A1** | Delete the two orphan MetalLB policies. This is direct `kubectl`, because no repo source exists | MetalLB controller and speakers, LB IPs `.6/.7/.8/.230/.231/.232` | `kubectl get netpol -n metallb-system` is empty, LBs answer, edge snapshot unchanged | n/a (not enforced). They are saved as YAML in the plan's ledger | **yes** (S4 spirit: unmanaged delete) |
| **A2** | `argo-cd.global.networkPolicy.create: false` in `cluster/argo-cd/values.yaml`. Argo prunes the 5 | `argo-cd` app sync, UI, repo-server | the only NetworkPolicies left in the cluster are none. Argo Synced/Healthy | Argo degraded → revert | **yes** (argo-cd) |
| **A3a** | Add the `kube-network-policies` app with `nodeSelector: kubernetes.io/hostname=bottom`, plus PodMonitor and rules | the DaemonSet on `bottom`; agent logs (flags, "fail-open", empty-set sync); `kube_network_policies_packet_count` and `nfqueue_queue_total` | Ready; packet counters stay **0** with zero policies; edge snapshot and Ceph unchanged; no new non-Running pods | crash-loop, CPU or memory anomaly, any counter > 0, any edge or Ceph change → revert | **yes** (new privileged datapath DaemonSet in `kube-system`) |
| **A3b** | Drop the nodeSelector (all three nodes) after 24h on `bottom` | same, per node | same on all three | same | yes (same change, wider) |
| **B1** | `netpol-canary` namespace: run the baseline matrix with no policy, then add its policies | probe readiness on all three nodes; deny logs on each agent | the §6.4 matrix matches on all three nodes for 24h; other namespaces' traffic unchanged (packet counters move only on canary IPs); echo server's kubelet readiness keeps passing under default-deny (proves the root exemption) | any mismatch → investigate before going on. Revert removes the namespace | no (additive, isolated) |
| **B2** | `forgejo-runner` policies (§6.3), plus the init gate if the runner's owner agrees. **Preconditions:** the runner is idle (no running job; strict mode cuts denied flows) and B1 has been green 24h | runner logs (`FetchTask` long-poll reconnects once); agent deny logs on the runner's node; a real forge-bot CI run | the §9 tests pass; a full forge-bot pipeline (checkout, `go mod download`, tests, ko push to the forge registry) is green | a legitimate dependency is blocked → add an allow rule (or revert if urgent); the runner can't fetch tasks → revert | **yes** (live service, another owner's deployment) |
| **B3** | forge-bot's planned policy (DNS, Forgejo pods :3000, `23.163.0.48/32:443`) now does something; verify it | forge-bot logs and metrics; `ForgeBot*` alerts | a forge-bot cycle completes with zero read errors | read errors → fix the policy | with forge-bot's rollout |
| **B4** | After ≥1 week of B2: `global.networkPolicy.create: true` again (the chart's 5 Argo policies) | Argo UI, sync of several apps, repo-server and redis metrics scrape | every app keeps syncing; no deny-log lines for `argo-cd` | any sync failure → revert; if Argo can't revert itself, break-glass §6.5 step 1 | **yes** (argo-cd) |
| **C…** | One namespace at a time, ingress first (§7.1) | per namespace: its pods' readiness, its Ingress hosts in the edge snapshot, deny logs | a week clean before the next namespace with the same shape | deny lines for legitimate traffic → add a rule, or revert | per namespace; **yes** for anything with a webhook |

### 7.1 Adoption order after B4

Use the components: `default-deny` + `allow-dns` + `allow-from-traefik` + `allow-from-prometheus`
(if scraped) + same-namespace, with **egress open** to begin with.

1. **Leaf web apps with no dependencies across namespaces:** `word-arena`, `realmic`,
   `galaxy-site`, `chrismillerxyz`, `royaltracker`, `realliance-net`.
2. **Apps with their own Postgres** (same namespace): `attic`, `immich`, `gramps`, `forgejo`.
   - `forgejo` also gets the forge-bot ingress on :3000 and SSH from the LB.
   - With `etp: Cluster`, the LB's source is a node or flannel address. Allow port 22 from
     anywhere instead of trying to match sources.
3. **`oauth2-proxy`, `dex`, `mcp-gateway`.** Read the Luma §13.1 rules first: Dex is reached
   through its in-cluster Service.
4. **Luma namespaces** (phase 4): the §13.1 policy set, which is stricter (no DNS for sandboxes).
5. **Egress restriction** namespace by namespace, after listing each one's dependencies (see the
   discovery note below).
6. **Never in this plan:** `rook`, `kube-system`, `metallb-system`, `prometheus`, `telephony`
   (host-network PBX) and `home-assistant` (LAN devices, mosquitto). Webhook namespaces (`dns`,
   `rabbitmq-system`) wait until `allow-from-nodes` has been proven.

**Discovery.** kube-network-policies has no audit mode. To list a namespace's flows:

1. apply an allow-all policy to it (`ingress: [{}]`, `egress: [{}]`);
2. switch the agent to `--log-verdicts=all` for a bounded window. The flag is global, so do this
   only while that namespace is the only policed one besides the runner and canary;
3. read the accept lines.

Every container log goes to Azure Log Analytics and to router syslog, so keep the window short,
or exclude `kube-system/kube-network-policies` in fluent-bit first.

## 8. Interaction with the pending Talos/k8s upgrades (#2506)

- **Do A1–B2 before resuming #2506, but never during a hop.** The enforcer is CNI- and
  Talos-version-agnostic: the 1.10 kernel has every feature it needs, and so do later ones.
  - The runner's exposure is live today; the next hop (Talos 1.11 with etcd 3.6) is a multi-day
    sequence.
  - Doing it first makes each later hop a regression test for enforcement, not a confounder.
- **Add one gate to the talos-upgrade flow:**
  - after each node reboots, wait for `kube-network-policies` Ready on that node and the canary
    probe Ready on that node, then go on;
  - after each `upgrade-k8s`, the prober must be green on all three nodes.
- **During a node reboot:**
  - the runner is drained elsewhere, and the init gate holds it until enforcement is live there;
  - the rebooting node's agent restarts, a fail-open gap for that node's policed pods. That is
    expected and is why the canary exists.
- **Talos 1.13.x: do not set `kubeNetworkPoliciesEnabled`** (bundled v1.1.0, #380). Talos ≥1.14:
  still don't, per §5. If it is ever adopted, remove our DaemonSet first, then sync the bootstrap
  manifests, then re-run B1.
- **k8s 1.34 → 1.37.** NetworkPolicy v1 is stable. v1.1.3 is built on k8s v0.37 client libraries,
  and the APIs it watches (pods, namespaces, networkpolicies v1) are unchanged. Re-run the canary
  after every minor hop anyway.
- **If Cilium is ever chosen (§10), do it after #2506 is finished**, never interleaved.

## 9. Test plan

**1. Enforcement off (A3).** With zero policies:

- `kube_network_policies_packet_count == 0` on all three nodes for 24h;
- the edge snapshot is identical;
- Ceph is unchanged.

This proves claim 2 in §3.4: no policy means no datapath change.

**2. Canary matrix (B1).** The §6.4 matrix on each node, before the policy (baseline: targets
reachable) and after (expected allow/deny). Also check:

- each agent's deny log shows the canary pod's IP towards each blocked target;
- a pod in another namespace still reaches the echo server's namespace-mates only if allowed
  (ingress default-deny proven);
- the kubelet probes keep passing.

**3. Runner (B2).** Run the same matrix two ways:

- with `kubectl exec -n forgejo-runner deploy/forgejo-runner -- node -e '<connect probe>' <host> <port>`
  (node is in the image; a refusal is a failure);
- as a throwaway CI job on a scratch branch of an allowlisted repo, which proves the *job* context
  (same netns) is confined.

Expected results:

| Target | Expected |
|---|---|
| `git ls-remote https://git.realliance.net/realliance/forge-bot` | works |
| `go mod download` against proxy.golang.org | works |
| a GitHub action fetch | works |
| a registry push via `git.realliance.net` | works |
| BMC `.117:443`, KVM `.2:80`, `10.96.0.1:443`, `forgejo-http.forgejo.svc:3000`, `192.168.0.7:443`, `192.168.0.1:22`, `152.44.247.88:8585` | all time out |

The agent's deny log on the runner's node must show those exact five-tuples with the runner pod as
the source.

**4. Restart behaviour.** Delete the agent pod on the runner's node (with Chris's OK; it's the
`kube-system` DaemonSet):

- check that the DaemonSet brings it back;
- check that the matrix is enforced again within a minute;
- record the observed gap.

**5. After every #2506 hop and every kube-network-policies bump:** the canary must be green on all
three nodes (§8).

## 10. Follow-ups (not in this rollout)

- **Router hardening** (S4/S8, through `router/ansible/` with commit-confirm). Limit input rules 40
  (DNS) and 60 (zigbee2mqtt `:8585`) to destination `192.168.0.1`, so router-local services don't
  answer on the WAN address to inside hosts. The WAN /25 carve-out in §6.3 then becomes belt and
  braces, not the only control.
- **BMC/KVM isolation belongs in the network, not only in pod policy.** Node processes and
  hostNetwork pods are never policed, and every pod leaves with a node IP. A management VLAN for
  the BMCs and KVM via `swctl`, with a router allowlist, is the structural fix. ipmi-exporter then
  needs a path: a satellite, or an explicit router rule.
- **Cluster-wide baseline.** If wanted later, evaluate the `npa-v1alpha2` flavour (a
  ClusterNetworkPolicy Baseline tier that denies BMCs/KVM for all pods, with an Accept for
  ipmi-exporter). It means `divertAll`: every packet on every node through userspace, Ceph
  included. Measure CPU and latency on one node first. Or get it from Cilium.
- **NRI on Talos** (a machine-config change to containerd, S4). It closes the new-pod race (#414)
  and lets us drop `--disable-nri`.
- **Revisit Cilium at the next real rebuild.** Triggers: nodes re-provisioned (for example the
  planned per-node flash), a need for host firewall or FQDN egress, or Hubble-grade flow
  visibility for Luma.
- **Docs to update when B2 lands:**
  - CLAUDE.md topology (add "NetworkPolicy: enforced by kube-network-policies", and add a sharp
    edge for §3.4 items 1, 3 and 4);
  - the "flannel does not enforce NetworkPolicy" lines in `docs/forgejo.md`, `docs/gramps.md`,
    `docs/telephony.md` and the forge-bot spec;
  - `docs/luma.md` phase 4 (the enforcer part is done).

## 11. Open questions for Chris

1. A2 then B4 (switch off Argo's chart policies, re-enable later), or keep them on through A3?
   Keeping them means A3 polices Argo from the first minute. I recommend A2.
2. Pre-approve the §6.5 break-glass as a standing S2/S4 exception for this rollout?
3. Does the runner's owner take the `netpol-gate` init container (§6.3)?
4. Go with v1.1.3 one day after its release (the canary is the soak), or start A3 on v1.1.2 and
   bump before B1?
5. Rehearse on kind first, as Luma §13.1 suggested? Per standing preference that is CI or ask
   first, not local e2e.

**Answers, 2026-11-03 (Chris):** approach approved (kube-network-policies as an Argo-managed
DaemonSet beside flannel; canary on `bottom`, then all nodes, then `forgejo-runner`).
1. A2 then B4: yes, Argo's chart policies go off before A3 and back on at B4.
2. Break-glass pre-approval: not yet decided; asked before A3.
3. The runner takes the `netpol-gate` init container: yes.
4. v1.1.3 now (the two-week pin-age rule is waived for this one; the canary is the soak).
5. No kind rehearsal.

## Sources

- Talos v1.13.0 release notes (flannel network policies):
  <https://github.com/siderolabs/talos/releases/tag/v1.13.0>
- Talos commit adding it (DaemonSet sidecar, RBAC):
  <https://github.com/siderolabs/talos/commit/87615f551183cd322dafebf368a347d928a14442>
- Talos `KubeNetworkPoliciesVersion` constant:
  <https://github.com/siderolabs/talos/blob/v1.13.12/pkg/machinery/constants/constants.go>,
  <https://github.com/siderolabs/talos/blob/v1.14.5/pkg/machinery/constants/constants.go>
- Talos flannel doc: <https://docs.siderolabs.com/kubernetes-guides/cni/flannel>
- Talos Cilium doc: <https://docs.siderolabs.com/kubernetes-guides/cni/deploying-cilium>
- kube-network-policies: <https://kube-network-policies.sigs.k8s.io>
  - architecture:
    <https://github.com/kubernetes-sigs/kube-network-policies/blob/v1.1.3/site/content/docs/concepts/architecture.md>
  - flavours and flags:
    <https://github.com/kubernetes-sigs/kube-network-policies/blob/v1.1.3/site/content/docs/user/flavors-and-architectures.md>
  - ANP and divertAll:
    <https://github.com/kubernetes-sigs/kube-network-policies/blob/v1.1.3/site/content/docs/concepts/admin-network-policy.md>
  - dataplane code (hooks, skuid, fail-open):
    <https://github.com/kubernetes-sigs/kube-network-policies/blob/v1.0.0/pkg/dataplane/controller.go>
  - #380: <https://github.com/kubernetes-sigs/kube-network-policies/issues/380>
  - #389: <https://github.com/kubernetes-sigs/kube-network-policies/pull/389>
  - v1.1.3 release notes:
    <https://github.com/kubernetes-sigs/kube-network-policies/releases/tag/v1.1.3>
- kind embeds it in kindnet: <https://github.com/kubernetes-sigs/kind/pull/3612>
- network-policy-api v0.2.0 (ClusterNetworkPolicy v1alpha2):
  <https://github.com/kubernetes-sigs/network-policy-api/releases/tag/v0.2.0>
- kube-router user guide: <https://kube-router.io/docs/user-guide/>
  - v2.11.0 (experimental nftables): <https://github.com/cloudnativelabs/kube-router/releases/tag/v2.11.0>
  - #1434: <https://github.com/cloudnativelabs/kube-router/issues/1434>
- k3s embedded kube-router netpol: <https://docs.k3s.io/networking/networking-services>
- Calico:
  - Canal: <https://docs.tigera.io/calico/latest/getting-started/kubernetes/flannel/install-for-flannel>
  - nftables: <https://docs.tigera.io/calico/latest/getting-started/kubernetes/nftables>
  - 3.32 post: <https://tigera.io/blog/whats-new-in-calico-v3-32/>
  - upgrade notes: <https://docs.tigera.io/calico/latest/operations/upgrading/upgrade-notes>
- Cilium:
  - migration: <https://docs.cilium.io/en/stable/installation/k8s-install-migration/>
  - policy (KCNP in 1.20): <https://docs.cilium.io/en/latest/network/kubernetes/policy/>
- Argo CD chart 10.9.2 values (`global.networkPolicy.create: true`):
  <https://github.com/argoproj/argo-helm/blob/argo-cd-10.9.2/charts/argo-cd/values.yaml>
