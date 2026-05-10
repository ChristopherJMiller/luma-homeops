{ config, lib, pkgs, ... }:

{
  # Board selection happens in lib/mkHost.nix via the `board` argument, which
  # imports modules/boards/pi-3b.nix. This file is for per-host hardware
  # quirks: USB device pinning, GPIO, extra serial, etc.

  # If multiple USB serial devices are ever attached, pin the printer to a
  # stable name via udev. Until then, OctoPrint auto-detects /dev/ttyUSB0 or
  # /dev/ttyACM0 on its own.
  #
  # services.udev.extraRules = ''
  #   SUBSYSTEM=="tty", ATTRS{idVendor}=="XXXX", ATTRS{idProduct}=="YYYY", SYMLINK+="ttyPRINTER"
  # '';
}
