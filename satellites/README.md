# satellites

NixOS-based edge devices in orbit around the `galaxy` Kubernetes cluster.

Where `cluster/` is GitOps-managed by Argo CD (push-from-repo, decoded
in-cluster), `satellites/` is GitOps-managed by [comin](https://github.com/nlewo/comin)
(pull-from-device, decoded on-device). Each satellite holds a static IP in
**upper orbit** (`192.168.0.243-.254`), runs an immutable-ish NixOS, and
fetches its declarative state from this repo.

## Topology

```
.1            VyOS router
.5            Talos VIP (KubePrism)
.6-.8         MetalLB pool "default" — public ingress
.9-.229       DHCP dynamic
.230-.239     MetalLB pool "lan-internal" — cluster services exposed to LAN
.240-.242     top / middle / bottom (cluster nodes)
.243-.254     UPPER ORBIT — satellites
```

## Current fleet

| Callsign     | Role                       | Board     | IP             |
|--------------|----------------------------|-----------|----------------|
| `octoprint`  | 3D printer / OctoPrint     | Pi 3B     | 192.168.0.243  |

## Adding a satellite

```sh
nix-shell ../shell.nix --run './scripts/new-host.sh <callsign> <board>'
```

This scaffolds `hosts/<callsign>/`, generates an ed25519 host key into
`.keys/` (git-crypt encrypted at rest), and prints the followup edits.

Then:
1. Wire `nixosConfigurations.<callsign>` + the matching `sdImage-<callsign>`
   into `flake.nix`.
2. Add the pubkey to `secrets/recipients.nix`.
3. Add a DHCP static-mapping entry in
   `../router/ansible/group_vars/vyos_routers.yml`.
4. Build & flash: `./scripts/flash.sh <callsign> /dev/sdX`.

## Day-2

The device polls this repo every minute (`services.comin.remotes[*].poller.period`).
Edit the host's config, sign-tag the commit (key must be in
`satellites.comin.gpgPublicKeyPaths`), push to `satellites/release`,
and the next poll picks it up.

Reboots happen automatically during the host's maintenance window
(`satellites.comin.autoReboot.window`, default `03:00`) when the booted
generation differs from the current generation. Non-kernel changes apply
immediately on poll.

## Safety rules (mirror of the cluster S-rules in CLAUDE.md)

**SAT1. Pull, never push.** comin is the only thing that writes to a
satellite's filesystem. Don't `ssh ... nixos-rebuild` to a satellite —
your change will be overwritten on the next poll.

**SAT2. Sign your commits.** Devices verify against
`satellites.comin.gpgPublicKeyPaths`. An unsigned tag will not deploy.

**SAT3. Touch base.nix carefully.** A bad networking change in
`modules/base.nix` can leave a device unable to phone home and the only
remedy is a re-flash. Treat changes to base.nix like changes to
`nodes/controlplane.yaml`.

**SAT4. Don't widen `lan-internal` MetalLB without checking DHCP.** The
range `192.168.0.230-.239` was carved out of DHCP. If you grow it down,
shrink `dhcp.range_stop` in router/ansible FIRST.

**SAT5. Verify rollback before you need it.** Pin the comin branch to a
known-good prior tag and confirm the device reverts on next poll. Test it
on `octoprint` once; trust it on the rest of the fleet.

## Phase 2 / open items

- LAN git mirror (cluster-hosted) so comin doesn't depend on GitHub uptime.
- CI: `nix flake check` + per-host toplevel build on PR.
- Journal upload to cluster (Loki) so volatile journald doesn't lose
  evidence on crash.
- mTLS enforcement (the `edge-ca` ClusterIssuer scaffold is dormant).
- Webcam streaming on `octoprint`.
- Secrets-rotation runbook (device lost/stolen).
