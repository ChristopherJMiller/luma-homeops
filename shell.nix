{ pkgs ? import (fetchTarball "https://github.com/NixOS/nixpkgs/archive/nixos-unstable.tar.gz") {} }:

pkgs.mkShell {
  packages = with pkgs; [
    git
    kubectl
    pre-commit
    kubeseal
    nodejs_20
    kustomize
    talosctl
    kubernetes-helm
    argocd
  ];
}
