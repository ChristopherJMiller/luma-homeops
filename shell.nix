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
  ];
}
