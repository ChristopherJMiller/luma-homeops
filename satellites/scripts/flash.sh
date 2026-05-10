#!/usr/bin/env bash
# Flash a satellite's SD image to a card. Run from anywhere; resolves paths
# relative to the satellites/ flake.
#
# Usage: ./scripts/flash.sh <hostname> <device>
#   e.g. ./scripts/flash.sh octoprint /dev/sdb

set -euo pipefail

HOSTNAME="${1:-}"
DEVICE="${2:-}"

if [[ -z "$HOSTNAME" || -z "$DEVICE" ]]; then
  echo "usage: $0 <hostname> <device>" >&2
  echo "  e.g. $0 octoprint /dev/sdb" >&2
  exit 2
fi

if [[ ! -b "$DEVICE" ]]; then
  echo "error: $DEVICE is not a block device" >&2
  exit 1
fi

# Refuse to write to anything currently mounted as / or /home.
ROOT_DEV=$(findmnt -n -o SOURCE / | sed 's/[0-9]*$//')
HOME_DEV=$(findmnt -n -o SOURCE /home 2>/dev/null | sed 's/[0-9]*$//' || true)
if [[ "$DEVICE" == "$ROOT_DEV" || "$DEVICE" == "$HOME_DEV" ]]; then
  echo "error: $DEVICE backs the running system. Refusing." >&2
  exit 1
fi

SIZE_BYTES=$(blockdev --getsize64 "$DEVICE")
SIZE_GB=$(( SIZE_BYTES / 1024 / 1024 / 1024 ))
if (( SIZE_GB > 256 )); then
  echo "error: $DEVICE is ${SIZE_GB} GB — that's probably not an SD card. Refusing." >&2
  exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
FLAKE_DIR=$(cd "$SCRIPT_DIR/.." && pwd)

echo ">>> Building SD image for $HOSTNAME (this can take a while under emulation)"
nix build --print-out-paths "$FLAKE_DIR#sdImage-$HOSTNAME"

IMG=$(find result/sd-image -maxdepth 1 -name '*.img.zst' -o -name '*.img' | head -1)
if [[ -z "$IMG" ]]; then
  echo "error: no image found under result/sd-image/" >&2
  exit 1
fi

echo
echo ">>> Image: $IMG"
echo ">>> Target: $DEVICE  (${SIZE_GB} GB)"
lsblk "$DEVICE"
echo
read -r -p "Write this image to $DEVICE? Everything on it will be erased. [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }

if [[ "$IMG" == *.zst ]]; then
  zstdcat "$IMG" | sudo dd of="$DEVICE" bs=4M conv=fsync status=progress
else
  sudo dd if="$IMG" of="$DEVICE" bs=4M conv=fsync status=progress
fi
sync

echo
echo ">>> Done. You can eject $DEVICE and boot it in the Pi."
