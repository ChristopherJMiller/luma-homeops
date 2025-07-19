{ pkgs ? import <nixpkgs> {} }:

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
  ];
}
