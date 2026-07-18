{ pkgs ? import <nixpkgs> {
    config.allowUnfree = true;
  }
}:

pkgs.mkShell {
  packages = with pkgs; [
    git
    kubectl
    krew
    pre-commit
    kubeseal
    nodejs_20
    kustomize
    talosctl
    kubernetes-helm
    argocd
    krew
    pinentry-tty
    ansible
    azure-cli
    terraform

    # satellites/: zstd for flash.sh image decompression. agenix is not in
    # nixpkgs as a top-level pkg; invoke it directly with:
    #   nix run github:ryantm/agenix -- -e satellites/secrets/<file>.age
    zstd

    # lagrange satellite glue: sops to decrypt the lagrange repo's
    # secrets/satellite.yaml (admin token, wg-private-key) and wg/wg-pubkey
    # to derive/inspect WireGuard keys for cluster/lagrange-satellite/.
    sops
    wireguard-tools

    # BMC access (AST2500 on the X570D4I-2T nodes, IPs .117/.119/.151):
    # ipmitool for SEL/sensors/power/SOL over lanplus; Redfish works via
    # plain curl. Creds live in nodes/bmc.env (git-crypt).
    ipmitool

    # operators/ceph-nfs-export-operator/: kopf-based reconciler.
    # python3 + uv for dep management; ruff for lint; docker for local image
    # build/test before pushing to ghcr.
    python313
    uv
    ruff
    docker_29
  ];
}
