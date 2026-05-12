{ config, lib, pkgs, ... }:

{
  # No agenix secrets yet. CUPS auth uses PAM against the admin user from
  # base.nix; printer config is mutated via the web UI and persists in
  # /var/lib/cups (mutable state, not part of the Nix closure).
}
