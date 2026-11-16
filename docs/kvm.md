# kvm-rdp: the work Mac over RDP

Remote access to the work Mac Studio through the ES3 IP-KVM at `192.168.0.2`,
with no raw internet exposure of the Mac or the KVM.

- **Bridge:** `kvm-rdp-mac` (ns `kvm`, Argo `kvm`). It speaks RDP with TLS and NLA
  on `10.96.0.50:3389` and drives the KVM over HTTPS. Source:
  [kvm-rdp](https://github.com/ChristopherJMiller/kvm-rdp).
- **Gateway** (stage 3): rdpgw at `kvm.mac.luma.chrismiller.xyz`, behind the
  oauth2-proxy admin tier.
- **Design:** `docs/superpowers/specs/2026-10-07-kvm-rdp-deployment-design.md`.
  The bridge's own design is kvm-rdp's spec, rev 6.4.

## Rules

- **Never log out of the KVM**, by script or by API. ES3 logout is global: it ends
  every session, the bridge's and the web UI's. Logins coexist.
- **One automated KVM client:** one replica, `Recreate`, no PDB, no HPA.
- **No LoadBalancer or NodePort for 3389 or 9464.** The admin port has no auth.
- An RDP session through the bridge types into the work Mac. Only Chris opens one.

## Secrets

`./sign.sh` seals `cluster/kvm/kvm-rdp-mac.secret.yaml` (git-crypt) into
`kvm-rdp-mac.yaml`. It holds two keys:

- `kvm-password`: the ES3 password.
- `nla-password`: what Windows App or FreeRDP sends as user `kvm`. 22 characters
  at least.

To rotate either one, edit the file, `rm cluster/kvm/kvm-rdp-mac.yaml && ./sign.sh`,
then commit. A KVM password change shows as `kvm_auth_failed` until the new
Secret lands.

The bridge certificate (`kvm-rdp-mac-tls`, `mac-bridge.kvm.chrismiller.xyz`) is
cert-manager's. The bridge restarts on renewal, but only when no client holds a
session.

## KVM factory reset (deferred)

kvm-rdp §9.3 asks for a factory reset, because the ES3 has a pre-auth RCE on
`:8888`. Chris deferred it on 2026-11-16. After a reset:

1. Set the preset to 1920×1080 at 30 fps, and keep Broadcasting IP off.
2. Put the new password in the Secret and reseal it.
3. Retake the pin. The output on all three ports must match:

   ```sh
   IMG=ghcr.io/christopherjmiller/kvm-rdp:sha-a58a10d@sha256:97c6734518dc0e39a577ab3b3b535a84842d0a75abbb7a9b00f6fd0a653fa2a9
   for p in 443 8881 8889; do
     docker run --rm --entrypoint /usr/local/bin/kvm-probe "$IMG" fingerprint --host 192.168.0.2 --port "$p"
   done
   ```

   Put the value in `spki_sha256` in `cluster/kvm/config/bridge-mac.toml`. Until
   then every session fails with `kvm_cert_mismatch`. `fingerprint` sends no login.
   Every other `kvm-probe` subcommand can log in or type into the Mac.

## Image bumps

kvm-rdp has no semver tags, so Renovate leaves it alone (`.renovaterc.json`).
Bump the `sha-…@sha256:…` pin by hand, after both checks pass:

```sh
D=sha256:...   # the new digest
ID='https://github.com/ChristopherJMiller/kvm-rdp/.github/workflows/ci.yml@refs/heads/main'
cosign verify ghcr.io/christopherjmiller/kvm-rdp@$D \
  --certificate-identity "$ID" --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify oci://ghcr.io/christopherjmiller/kvm-rdp@$D \
  --repo ChristopherJMiller/kvm-rdp --cert-identity "$ID" --deny-self-hosted-runners
```

Run them with cosign v3 or later (`nix-shell -p cosign`). rdpgw comes from
`images/rdpgw`, pinned to an upstream commit. A new commit re-runs the stage-3
gateway checks.

## Alerts

- **`KvmBridgeDown`:** Prometheus can't scrape the bridge for 10 minutes.
- **`KvmBridgeUpstreamFatal`:** the bridge dropped a session for a KVM-side cause.
  - `kvm_cert_mismatch` means the pin is stale; see the factory reset section.
  - `kvm_auth_failed` means the password is wrong.
  - `kvm_unreachable` means the KVM is off or off the network.

The useful series are `kvm_rdp_sessions_total`, `kvm_rdp_kvm_logins_total`,
`kvm_rdp_disconnects_total{cause}` and `kvm_rdp_frames_dropped_total{reason}`.
