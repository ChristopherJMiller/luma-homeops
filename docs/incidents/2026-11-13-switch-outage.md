# Two-node operation after the 2026-11-13 switch interruption

Top remains offline with no Ethernet carrier. Middle and bottom serve the
cluster while physical access to the rack is unavailable for about two weeks.
The service priorities are Forgejo, Immich, family services and Home Assistant.
Plex and CI workers remain available; media acquisition, its automation and
Trivy vulnerability scanning are paused.

## Current evidence

At 00:17 PST, the router's LAN and switch-management links dropped together
and returned about 78 seconds later. The router did not reboot. A switch
restart or power interruption is plausible; the exact cause is unconfirmed.

Middle and bottom's DHCP-derived names changed. Restoring their original
identities, pod networking and storage brought services back. Top's physical
NIC still reports no carrier under both Talos and independent SystemRescue
Linux. Register, EEPROM, interrupt and internal loopback self-tests pass;
the link test fails. Power cycling top, restarting negotiation, limiting it
to 1 Gb/s and reloading its driver did not restore carrier.

The switch still forwards LAN traffic, but its configured management address
192.168.99.2 does not answer ARP. The factory address 192.168.10.200 also did
not answer probes through either router cable, untagged or on VLANs 1/20/99.
DHCP leases/history, LAN ARP discovery and IPv6 multicast discovery identified
no alternate switch address. Renegotiating router eth3 restored its normal
1 Gb/s link but did not restore management. LAN traffic is visible on eth3,
contrary to the intended VLAN 99 isolation. This could be a management fault
or unexpected configuration; a frozen switch is not proven.

The switch has no remotely controlled power outlet or connected serial
recovery path. Current TRENDnet Management Utility 2.0.5 requires switch
firmware 3.00.007 or newer; this switch was recorded running 3.00.003.
The older utility is published for hardware v1, rather than this v2 switch.
No supported additional remote recovery path has been identified.

## Media pause

At the capacity check, actual memory use was about 43–44%, but requested
memory was 97% on middle and 99% on bottom. Suspending these eight Deployments
releases 2996 MiB and 2850 millicores of requests on middle and reduces acquisition,
library maintenance and database writes:

- `media/mm-reconciler`
- `media/mm-maintainerr`
- `media/mm-ombi`
- `media/mm-sonarr`
- `media/mm-radarr`
- `media/mm-lidarr`
- `media/mm-prowlarr`
- `media/mm-sabnzbd`

The GitOps Application `mega-media-release` has
`argocd.argoproj.io/skip-reconcile: "true"`. This permits the temporary
Deployment scaling exception without Argo restoring replicas or launching
media configuration hooks. Chart values and all PVCs remain in place.
The chart hardcodes one replica, so setting an unsupported Helm replica
value would not pause these workloads. Disabling chart components would also
remove their PVC manifests and is unsuitable for this pause.

After the annotation is present and no sync operation is running, stop the
automation first, then its targets:

```sh
kubectl -n media scale deployment mm-reconciler mm-maintainerr mm-ombi --replicas=0
kubectl -n media scale deployment mm-sonarr mm-radarr mm-lidarr mm-prowlarr mm-sabnzbd --replicas=0
```

Plex, the media PostgreSQL cluster, family/archive volumes, File Browser,
WebDAV and backup CronJobs continue running. Forgejo, Immich, Gramps, Home
Assistant, authentication and their dependencies retain normal reconciliation.
Argo's displayed status for the paused media Application is stale by design;
use `kubectl -n media get deployments,pods,pvc` to inspect its actual state.

## Vulnerability scanning pause

The `trivy-operator` Application also has `skip-reconcile: "true"`. Its
`trivy-operator` Deployment and `trivy-server` StatefulSet are scaled to zero,
releasing another 1600 MiB and 300 millicores of requests on bottom. The server
StatefulSet's PVC retention policy is `Retain` for both scale-down and deletion;
its existing 10 GiB cache claim and all scan reports remain in place.

```sh
kubectl -n trivy-system scale deployment trivy-operator --replicas=0
# Wait for active scan Jobs to finish before stopping their cache server.
kubectl -n trivy-system scale statefulset trivy-server --replicas=0
```

The pause stops cluster scanning and its report refresh; it does not change
Forgejo, either CI runner Deployment, or their build workflows. Use
`kubectl -n trivy-system get deployments,statefulsets,jobs,pods,pvc` to check
the actual paused state. Inspect outstanding scan Jobs before scaling the
server down; none were active at preparation time.

Grafana's UI and two configuration sidecars are also paused with
`grafana.replicas: 0` in the monitoring Application's chart values. This
releases 1216 MiB of memory requests on bottom while retaining the Deployment,
Services and provisioned dashboard configuration. Prometheus, Alertmanager
and exporters remain enabled and reconciliation stays active. Grafana uses
`emptyDir` rather than a PVC; a private copy of its current SQLite database
passed an integrity check before scale-down. Restore its default one replica
by removing the temporary override after capacity recovery.

Top is explicitly cordoned (`NotReady,SchedulingDisabled`), in addition to
its unreachable taints. Keep the cordon until its identity, network and
storage paths have been checked after the physical link returns.

## Storage and backups

Backup CronJobs remain enabled. Post-recovery checks also ran fresh Forgejo
logical database and repository/config backups, plus a Home Assistant restic
snapshot, successfully writing offsite. The fresh Forgejo archive was about
350 MB; the restic snapshot was `bc48142f` at 2026-11-13 19:39:57 UTC.
The manual restic restore canary also passed, restoring selected Home
Assistant, Plex and gonic files into disposable storage and confirming
satellite and CI-ledger snapshots exist.

Prometheus and Alertmanager initially remained blocked by their original
terminating pod records on top. Those pre-reboot processes were fenced by
top's confirmed power cycles and independent live-OS boot. Recovery removes
only the stale pod API records so their StatefulSets can reuse the original
PVCs on healthy nodes. Prometheus temporarily requests 3 GiB instead of 4 GiB
to fit the available reservations; its 4 GiB memory limit remains in place.
Restore the request after capacity recovery. Both monitoring StatefulSets
recovered on middle using normal CSI detach/reattach. Prometheus and
Alertmanager readiness passed; all three backup alert rules evaluated with
health `ok`, and Prometheus discovered an active Alertmanager.

Ceph remains available with six OSDs across two hosts and reduced redundancy.
An etcd snapshot was preserved before recovery. All placement groups were
active at the last check. Pausing media does not restore the missing third
storage replica. Preserve backups and avoid maintenance on the surviving
nodes while top is unavailable.

## Restore

1. Restore top's physical link. The next direct tests are a controlled switch
   power cycle, then inspecting/reseating the cable and transceiver on port 27.
   The recessed factory-reset control must not be used for this recovery.
2. Verify all original nodes are Ready, etcd membership is healthy and all nine
   OSDs return. Top's HDD OSD activation paths may need the same serial/OSD-UUID
   verification and stable by-id correction used on middle and bottom.
3. Check current Ceph health, pod CIDRs, database primaries and backups before
   uncordoning top or resuming acquisition. Inspect the media Application's
   current chart/version diff, since reconciliation has been paused.
4. Remove the `skip-reconcile` annotation and its temporary comment from the
   two paused Applications through a normal GitOps change. Argo restores the
   charts' one-replica workloads and media configuration hooks. Keep this
   incident record for context.
5. Restore the Prometheus request to 4 GiB once capacity permits. Verify
   Prometheus and Alertmanager remain Ready and backup alert rules are loaded.
   Remove `grafana.replicas: 0` to restore the Grafana UI and its sidecars.
6. Verify all eight media Deployments and both Trivy workloads become Ready,
   Plex and CI remain available, PVC UIDs are unchanged, and resource requests
   fit before downloads and scanning resume.

The pause has no automatic expiry: resume after repair and verification,
rather than assuming physical access or networking recovers on a calendar date.
