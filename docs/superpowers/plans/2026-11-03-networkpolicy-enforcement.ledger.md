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

Objects as deleted (`metadata.managedFields` stripped for readability):

```yaml
apiVersion: v1
items:
  - apiVersion: networking.k8s.io/v1
    kind: NetworkPolicy
    metadata:
      creationTimestamp: "2024-10-06T22:01:01Z"
      generation: 1
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
      resourceVersion: "21524148"
      uid: b0770096-9deb-418a-a6ff-b14e77758ae2
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
      creationTimestamp: "2024-10-06T22:01:01Z"
      generation: 1
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
      resourceVersion: "21524149"
      uid: 8d0dc36b-ea73-4335-ad32-4576a6b91af8
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
  resourceVersion: ""
```

After the delete: `metallb-system` has no NetworkPolicies. TCP checks ok for .6:22, .7:443, .8:1883, .230:2049, .231:5000; `wg-gateway` LB IP `192.168.0.232`.
Gate G after: nodes 3 Ready; Ceph HEALTH_WARN, 145 active+clean; Argo only `rook`/`rook-cluster` Degraded; no bad pods; netpols now 5 in `argo-cd` only; edge diff empty once per-request OAuth PKCE/state values are normalised.
