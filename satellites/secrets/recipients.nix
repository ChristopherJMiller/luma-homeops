let
  hosts = {
    # octoprint = "ssh-ed25519 AAAA... populated by scripts/new-host.sh";
  };

  operators = {
    chris = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICHR4q3amhKDhCF6+xa3oTXJX2ycN503+cEo/gpnOkFt git@chrismiller.xyz";
  };
in
{
  inherit hosts operators;

  # Per-secret recipient lists are assembled in secrets.nix (sibling to .age
  # files) using these maps. Example:
  #
  #   { "octoprint-api-key.age".publicKeys = [ operators.chris hosts.octoprint ]; }
}
