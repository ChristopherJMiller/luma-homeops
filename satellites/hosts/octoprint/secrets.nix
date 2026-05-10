{ config, lib, pkgs, ... }:

{
  # age.secrets.octoprint-api-key = {
  #   file = ../../secrets/octoprint-api-key.age;
  #   owner = "octoprint";
  #   group = "octoprint";
  # };
  #
  # Wire the secret into services.octoprint after creating it with:
  #   cd satellites && agenix -e secrets/octoprint-api-key.age
  # The recipients are taken from secrets/recipients.nix.
}
