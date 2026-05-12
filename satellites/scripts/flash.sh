#!/usr/bin/env bash
# Flash a satellite's SD image to a card.
#
# By default fetches the latest successful CI artifact from
# .github/workflows/satellites-sd-images.yml on the satellites/release
# branch — much faster than building locally under aarch64 emulation.
#
# Usage:
#   ./scripts/flash.sh <hostname> [device]
#   ./scripts/flash.sh octoprint
#   ./scripts/flash.sh octoprint /dev/sdb     # skip auto-detect
#
# If no device is given, scans removable USB block devices and either
# auto-picks the single candidate or prompts interactively.
#
# Flags:
#   --build      Build the image locally with nix instead of downloading
#   --branch B   Branch to look for successful runs on (default: satellites/release)
#   --run ID     Use a specific GitHub Actions run id instead of latest success
#
# Requires:
#   - gh authenticated as someone with read access to the repo
#   - sudo (for dd)
#   - zstd (for image decompression)

set -euo pipefail

# Refuse to run as root — `gh` auth lives in the invoking user's keyring,
# and sudo discards that. The script will sudo internally for `dd` only.
if [[ $EUID -eq 0 ]]; then
  echo "error: don't run this with sudo — gh auth lives in your user keyring." >&2
  echo "       run as your normal user; the script will prompt for sudo at dd time." >&2
  exit 1
fi

# Pick a sudo askpass helper so the password prompt is a real GUI dialog
# inside a desktop session (KDE/Plasma ships ksshaskpass; GNOME has its
# own). Falls back to terminal prompt if nothing is available or no DE.
SUDO="sudo"
if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
  for cand in ksshaskpass /run/wrappers/bin/ksshaskpass x11-ssh-askpass; do
    helper=$(command -v "$cand" 2>/dev/null || true)
    [[ -n "$helper" ]] || continue
    export SUDO_ASKPASS="$helper"
    SUDO="sudo -A"
    break
  done
fi

HOSTNAME=""
DEVICE=""
MODE="download"   # or "build"
BRANCH="satellites/release"
RUN_ID=""
WIFI_SSID="${WIFI_SSID:-}"
WIFI_PSK="${WIFI_PSK:-}"
NO_WIFI=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build)  MODE="build"; shift ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --run)    RUN_ID="$2"; shift 2 ;;
    --ssid)   WIFI_SSID="$2"; shift 2 ;;
    --no-wifi) NO_WIFI=1; shift ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# //; s/^#//'
      exit 0 ;;
    *)
      if   [[ -z "$HOSTNAME" ]]; then HOSTNAME="$1"
      elif [[ -z "$DEVICE"   ]]; then DEVICE="$1"
      else echo "unexpected arg: $1" >&2; exit 2
      fi
      shift ;;
  esac
done

if [[ -z "$HOSTNAME" ]]; then
  echo "usage: $0 <hostname> [device] [--build] [--branch B] [--run ID]" >&2
  echo "  e.g. $0 octoprint" >&2
  exit 2
fi

ROOT_DEV=$(findmnt -n -o SOURCE / | sed 's/[0-9]*$//')
HOME_DEV=$(findmnt -n -o SOURCE /home 2>/dev/null | sed 's/[0-9]*$//' || true)

# --- pick target device interactively if not given ---

if [[ -z "$DEVICE" ]]; then
  echo ">>> Scanning for removable USB block devices..."
  # lsblk: top-level disks only (-d), no header (-n), full path (-p),
  # show name/size/transport/removable-flag/model. Filter to USB+removable
  # and exclude the running root.
  mapfile -t candidates < <(
    lsblk -d -n -p -o NAME,SIZE,TRAN,RM,MODEL 2>/dev/null \
      | awk -v root="$ROOT_DEV" -v home="$HOME_DEV" '
          $3=="usb" && $4=="1" && $1!=root && $1!=home { print }
        '
  )

  if [[ ${#candidates[@]} -eq 0 ]]; then
    echo "error: no removable USB devices detected." >&2
    echo "       insert the SD card (via a USB reader) and try again, or pass /dev/sdX explicitly." >&2
    exit 1
  fi

  echo
  echo "Candidate devices:"
  for i in "${!candidates[@]}"; do
    printf "  %d) %s\n" $((i+1)) "${candidates[$i]}"
  done
  echo

  if [[ ${#candidates[@]} -eq 1 ]]; then
    DEVICE=$(echo "${candidates[0]}" | awk '{print $1}')
    echo ">>> One candidate found: $DEVICE (will confirm before writing)"
  else
    read -r -p "Pick a device [1-${#candidates[@]}] or 'q' to quit: " choice
    [[ "$choice" =~ ^[Qq]$ ]] && { echo "aborted"; exit 1; }
    [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#candidates[@]} )) || {
      echo "invalid choice: $choice" >&2; exit 2
    }
    DEVICE=$(echo "${candidates[$((choice-1))]}" | awk '{print $1}')
  fi
fi

if [[ ! -b "$DEVICE" ]]; then
  echo "error: $DEVICE is not a block device" >&2
  exit 1
fi

if [[ "$DEVICE" == "$ROOT_DEV" || "$DEVICE" == "$HOME_DEV" ]]; then
  echo "error: $DEVICE backs the running system. Refusing." >&2
  exit 1
fi

# lsblk -b instead of blockdev: doesn't require root to read /dev/sdX
SIZE_BYTES=$(lsblk -b -d -n -o SIZE "$DEVICE")
SIZE_GB=$(( SIZE_BYTES / 1024 / 1024 / 1024 ))
if (( SIZE_GB > 256 )); then
  echo "error: $DEVICE is ${SIZE_GB} GB — probably not an SD card. Refusing." >&2
  exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
FLAKE_DIR=$(cd "$SCRIPT_DIR/.." && pwd)

# --- obtain the image ---

if [[ "$MODE" == "build" ]]; then
  echo ">>> Building SD image for $HOSTNAME locally (this can take hours under emulation)"
  cd "$FLAKE_DIR"
  nix build --print-out-paths \
    "path:.#packages.aarch64-linux.sdImage-$HOSTNAME"
  IMG=$(find result/sd-image -maxdepth 1 \( -name '*.img.zst' -o -name '*.img' \) | head -1)

else
  if ! command -v gh >/dev/null; then
    echo "error: gh not found. Install GitHub CLI or use --build." >&2
    exit 1
  fi

  # Derive repo from the git remote so the script is reusable across forks.
  REPO=$(git -C "$FLAKE_DIR" remote get-url origin \
    | sed -E 's|^git@github.com:|https://github.com/|; s|\.git$||' \
    | sed -E 's|^https://github.com/||')
  echo ">>> Repo: $REPO"

  if [[ -z "$RUN_ID" ]]; then
    echo ">>> Finding latest successful CI run on $BRANCH..."
    RUN_ID=$(gh run list \
      --workflow satellites-sd-images.yml \
      --repo "$REPO" \
      --branch "$BRANCH" \
      --status success \
      --limit 1 \
      --json databaseId \
      --jq '.[0].databaseId')
    if [[ -z "$RUN_ID" || "$RUN_ID" == "null" ]]; then
      echo "error: no successful run found for $BRANCH" >&2
      echo "       trigger the workflow or try --branch main, or use --build" >&2
      exit 1
    fi
  fi
  echo ">>> Using run id: $RUN_ID"

  DL_DIR=$(mktemp -d -t satellites-flash-XXXXXX)
  trap 'rm -rf "$DL_DIR"' EXIT
  echo ">>> Downloading sdImage-$HOSTNAME artifact to $DL_DIR..."
  gh run download "$RUN_ID" \
    --repo "$REPO" \
    --name "sdImage-$HOSTNAME" \
    --dir "$DL_DIR"

  IMG=$(find "$DL_DIR" -type f \( -name '*.img.zst' -o -name '*.img' \) | head -1)
fi

if [[ -z "${IMG:-}" || ! -f "$IMG" ]]; then
  echo "error: no image file produced" >&2
  exit 1
fi

echo
echo ">>> Image:  $IMG  ($(du -h "$IMG" | cut -f1))"
echo ">>> Target: $DEVICE  (${SIZE_GB} GB)"
echo
lsblk "$DEVICE"
echo

# --- WiFi credentials (prompt unless --no-wifi or already in env) ---

if [[ -z "$NO_WIFI" ]]; then
  if [[ -z "$WIFI_SSID" ]]; then
    read -r -p "WiFi SSID (empty = no WiFi): " WIFI_SSID
  fi
  if [[ -n "$WIFI_SSID" && -z "$WIFI_PSK" ]]; then
    read -r -s -p "WiFi PSK for '${WIFI_SSID}' (input hidden): " WIFI_PSK
    echo
  fi
fi

echo
read -r -p "Write this image to $DEVICE? Everything on it will be erased. [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }

# --- flash ---

if [[ "$IMG" == *.zst ]]; then
  zstdcat "$IMG" | $SUDO dd of="$DEVICE" bs=4M conv=fsync status=progress
else
  $SUDO dd if="$IMG" of="$DEVICE" bs=4M conv=fsync status=progress
fi
sync

# --- WiFi PSK injection (post-dd, before eject) ---
#
# Writes /var/lib/iwd/<SSID>.psk to the SD card's ext4 root partition.
# iwd reads this on first boot. The file lives in mutable system state
# (NOT /nix/store) so it persists across comin generation switches and
# never travels through git/CI/artifacts.

if [[ -n "$WIFI_SSID" && -n "$WIFI_PSK" ]]; then
  echo
  echo ">>> Injecting iwd PSK for SSID '${WIFI_SSID}' into root partition..."
  # NixOS sd-image layout: part 1 = FAT firmware, part 2 = ext4 root.
  # NVMe-style devices use ${DEVICE}p2; SCSI/USB use ${DEVICE}2.
  if [[ -b "${DEVICE}p2" ]]; then ROOT_PART="${DEVICE}p2"
  elif [[ -b "${DEVICE}2" ]]; then ROOT_PART="${DEVICE}2"
  else
    echo "error: cannot locate root partition for $DEVICE" >&2
    exit 1
  fi
  $SUDO partprobe "$DEVICE" 2>/dev/null || true
  mnt=$(mktemp -d -t satellites-mount-XXXXXX)
  $SUDO mount "$ROOT_PART" "$mnt"
  $SUDO mkdir -p "$mnt/var/lib/iwd"
  printf '[Security]\nPassphrase=%s\n' "$WIFI_PSK" \
    | $SUDO tee "$mnt/var/lib/iwd/${WIFI_SSID}.psk" >/dev/null
  $SUDO chmod 0600 "$mnt/var/lib/iwd/${WIFI_SSID}.psk"
  $SUDO chown 0:0  "$mnt/var/lib/iwd/${WIFI_SSID}.psk"
  $SUDO umount "$mnt"
  rmdir "$mnt"
  echo ">>> Wrote /var/lib/iwd/${WIFI_SSID}.psk (chmod 0600 root:root)"
fi

echo
echo ">>> Done. Eject $DEVICE and boot it in the Pi:"
echo "     sudo eject $DEVICE"
