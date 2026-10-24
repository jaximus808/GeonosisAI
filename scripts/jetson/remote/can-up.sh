#!/usr/bin/env bash
# TDM-44: bring up the SocketCAN interface(s) for the I2RT YAM arm. Run by rdf-can.service at boot
# and on USB-CAN hotplug (udev rule in 99-rdf-robot.rules). Idempotent; never fails the boot.
# Same as yam-sdk-notes.md §8 step 1: ip link set can0 up type can bitrate 1000000
#   RDF_CAN_IFACES   space-separated interface names (default can0)        TO VERIFY with the I2RT SDK
#   RDF_CAN_BITRATE  bit rate (default 1000000: 1 Mbit/s, what the YAM motors use)  TO VERIFY
#   RDF_CAN_TXQUEUELEN (default 1000; the default 10 drops frames at 30 Hz x 7 motors)
set -uo pipefail
IFACES="${RDF_CAN_IFACES:-can0}"
BITRATE="${RDF_CAN_BITRATE:-1000000}"
TXQ="${RDF_CAN_TXQUEUELEN:-1000}"

for ifc in $IFACES; do
  if ! ip link show "$ifc" >/dev/null 2>&1; then
    echo "rdf-can: $ifc not present (USB-CAN adapter unplugged?); skipping"
    continue
  fi
  cur_rate="$(ip -details link show "$ifc" | sed -n 's/.*bitrate \([0-9]*\).*/\1/p' | head -n1)"
  if ip link show "$ifc" | grep -q 'state UP\|,UP' && [ "$cur_rate" = "$BITRATE" ]; then
    echo "rdf-can: $ifc already up at $BITRATE"
    continue
  fi
  ip link set "$ifc" down 2>/dev/null
  # restart-ms (auto-recover from bus-off) is not supported by every driver (gs_usb may refuse it)
  ip link set "$ifc" type can bitrate "$BITRATE" restart-ms 100 2>/dev/null \
    || ip link set "$ifc" type can bitrate "$BITRATE"
  if ip link set "$ifc" txqueuelen "$TXQ" \
     && ip link set "$ifc" up; then
    echo "rdf-can: $ifc up at $BITRATE (txqueuelen $TXQ)"
  else
    echo "rdf-can: failed to bring up $ifc" >&2
  fi
done
exit 0
