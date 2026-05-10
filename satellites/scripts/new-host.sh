#!/usr/bin/env bash
# Scaffold a new satellite host: directory + per-device SSH host key +
# recipients map entry.
#
# Usage: ./scripts/new-host.sh <hostname> <board>
#   e.g. ./scripts/new-host.sh weather-sensor pi-zero-2w
#
# Boards correspond to module files under modules/boards/.

set -euo pipefail

HOSTNAME="${1:-}"
BOARD="${2:-}"

if [[ -z "$HOSTNAME" || -z "$BOARD" ]]; then
  echo "usage: $0 <hostname> <board>" >&2
  echo "  e.g. $0 weather-sensor pi-zero-2w" >&2
  exit 2
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
FLAKE_DIR=$(cd "$SCRIPT_DIR/.." && pwd)

BOARD_FILE="$FLAKE_DIR/modules/boards/$BOARD.nix"
if [[ ! -f "$BOARD_FILE" ]]; then
  echo "error: no board module at $BOARD_FILE" >&2
  echo "available boards:" >&2
  ls "$FLAKE_DIR/modules/boards/" | sed 's/\.nix$//' | sed 's/^/  /' >&2
  exit 1
fi

HOST_DIR="$FLAKE_DIR/hosts/$HOSTNAME"
KEY_FILE="$FLAKE_DIR/.keys/${HOSTNAME}_ed25519"

if [[ -d "$HOST_DIR" ]]; then
  echo "error: $HOST_DIR already exists. Refusing to clobber." >&2
  exit 1
fi
if [[ -e "$KEY_FILE" ]]; then
  echo "error: $KEY_FILE already exists. Refusing to clobber." >&2
  exit 1
fi

mkdir -p "$HOST_DIR" "$FLAKE_DIR/.keys"

echo ">>> Generating ed25519 host key for $HOSTNAME"
ssh-keygen -t ed25519 -N "" -C "satellite:$HOSTNAME" -f "$KEY_FILE" >/dev/null
chmod 600 "$KEY_FILE"
PUBKEY=$(cat "${KEY_FILE}.pub")

echo ">>> Writing $HOST_DIR/default.nix"
cat >"$HOST_DIR/default.nix" <<EOF
{ config, lib, pkgs, ... }:

{
  imports = [
    ./hardware.nix
  ];

  satellites.comin.autoReboot = {
    enable = false;
    window = "03:00";
  };
}
EOF

echo ">>> Writing $HOST_DIR/hardware.nix"
cat >"$HOST_DIR/hardware.nix" <<EOF
{ config, lib, pkgs, ... }:

{
  # Per-host hardware quirks live here. Board baseline comes from
  # modules/boards/$BOARD.nix (selected in satellites/flake.nix).
}
EOF

cat <<EOF

Done. Next steps:

  1. Add a nixosConfigurations entry in satellites/flake.nix:

       $HOSTNAME = mkHost {
         hostName = "$HOSTNAME";
         board = "$BOARD";
         modules = [ ./hosts/$HOSTNAME ];
       };

     Also add:

       sdImage-$HOSTNAME =
         self.nixosConfigurations.$HOSTNAME.config.system.build.sdImage;

  2. Add the host's pubkey to satellites/secrets/recipients.nix under hosts:

       $HOSTNAME = "$PUBKEY";

  3. Get the device's MAC, then add a DHCP reservation in
     router/ansible/group_vars/vyos_routers.yml (upper-orbit, .243-.254).

  4. Build and flash:
       ./scripts/flash.sh $HOSTNAME /dev/sdX
EOF
