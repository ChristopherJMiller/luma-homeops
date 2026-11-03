# NetworkPolicy enforcement ledger

## Baseline (2026-11-03, before A1)

- G1: 3 nodes Ready, none SchedulingDisabled; Ceph 145 pgs, all active+clean.
- G2: Ceph HEALTH_WARN ("1 clients failing to respond to cache pressure"; auth-insecure checks muted). Argo: only `rook` and `rook-cluster` Degraded.
- G3: edge snapshot, 40 hosts; saved to scratch `edge-a1-before.txt`.
- G4: no non-Running/Succeeded pods. NetworkPolicies: 5 in `argo-cd`, 2 in `metallb-system` (7 total).

## Answers

- 2026-11-03: Chris pre-approved A1 on the condition that Step 2 proves both policies are orphans. It did.

## A1 - removed the two orphaned MetalLB policies

Proof of orphan: no Argo app tracks a NetworkPolicy in `metallb-system`; 0 pods with `instance=metallb-release`; 2 policies present.

Objects as deleted, with server-set fields (uid, resourceVersion, creationTimestamp, generation) removed so `kubectl apply -f` can re-create them:

```yaml
apiVersion: v1
items:
  - apiVersion: networking.k8s.io/v1
    kind: NetworkPolicy
    metadata:
      labels:
        app.kubernetes.io/component: controller
        app.kubernetes.io/instance: metallb-release
        app.kubernetes.io/managed-by: Helm
        app.kubernetes.io/name: metallb
        app.kubernetes.io/version: 0.14.8
        argocd.argoproj.io/instance: metallb-release
        helm.sh/chart: metallb-6.3.16
      name: metallb-release-controller
      namespace: metallb-system
    spec:
      egress:
        - {}
      ingress:
        - ports:
            - port: 7472
              protocol: TCP
            - port: 9443
              protocol: TCP
      podSelector:
        matchLabels:
          app.kubernetes.io/component: controller
          app.kubernetes.io/instance: metallb-release
          app.kubernetes.io/name: metallb
      policyTypes:
        - Ingress
        - Egress
  - apiVersion: networking.k8s.io/v1
    kind: NetworkPolicy
    metadata:
      labels:
        app.kubernetes.io/component: speaker
        app.kubernetes.io/instance: metallb-release
        app.kubernetes.io/managed-by: Helm
        app.kubernetes.io/name: metallb
        app.kubernetes.io/version: 0.14.8
        argocd.argoproj.io/instance: metallb-release
        helm.sh/chart: metallb-6.3.16
      name: metallb-release-speaker
      namespace: metallb-system
    spec:
      egress:
        - {}
      ingress:
        - ports:
            - port: 7472
              protocol: TCP
            - port: 9443
              protocol: TCP
      podSelector:
        matchLabels:
          app.kubernetes.io/component: speaker
          app.kubernetes.io/instance: metallb-release
          app.kubernetes.io/name: metallb
      policyTypes:
        - Ingress
        - Egress
kind: List
metadata:
```

After the delete: `metallb-system` has no NetworkPolicies. TCP checks ok for .6:22, .7:443, .8:1883, .230:2049, .231:5000; `wg-gateway` LB IP `192.168.0.232`.
Gate G after: nodes 3 Ready; Ceph HEALTH_WARN, 145 active+clean; Argo only `rook`/`rook-cluster` Degraded; no bad pods; netpols now 5 in `argo-cd` only; edge diff empty once per-request OAuth PKCE/state values are normalised.

## A2 — Argo's chart policies off (2026-11-03)

Chris pre-approved A2 on 2026-11-03, conditional on render check 5 before and 0 after, and Gate G before matching the Task 1 baseline. Both held.
Render check (temp copy of chart, `helm template`): 5 NetworkPolicies before, 0 after.
Commit `47eb70d8` adds `argo-cd.global.networkPolicy.create: false` in `cluster/argo-cd/values.yaml`; pushed 10:22 local.
Gate G before: 3 nodes Ready; Ceph HEALTH_WARN (warn codes MDS_CLIENT_RECALL plus the muted AUTH_INSECURE_*), 145/145 active+clean; Argo only `rook`/`rook-cluster` Degraded; no bad pods; 5 netpols, all `argo-cd`. `scripts/ceph-gate.sh` latency check read 1278 ms (>= 1000 ms), which is the SMR slow-op noise, not a PG state change.
Sync: `argo-cd` reached Synced/Healthy at the new sha within 10 s of the wait starting.
Gate G after: `kubectl get netpol -A` prints `No resources found`; same nodes, Ceph and Argo state as before (ceph-gate latency 1238 ms); no bad pods; edge diff empty (PKCE/state/nonce normalised), `ci.chrismiller.xyz` row included.
