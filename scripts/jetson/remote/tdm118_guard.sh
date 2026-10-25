#!/usr/bin/env bash
# TDM-118: ExecStartPre of geonosis-robot.service. Exit 3 (no restart) when GeonosisAI must not start:
#   * the user's vrteleop-rh owns the CAN buses and the RealSense cameras while it runs;
#   * the CAN link GeonosisAI reads (YAM_CAN_CHANNEL) is missing or down.
# Read-only: it never touches vrteleop, CAN or the cameras.
set -u
if systemctl is-active --quiet vrteleop-rh 2>/dev/null; then
  echo "geonosis-robot: vrteleop-rh is running and owns CAN + cameras. Stop it first: sudo systemctl stop vrteleop-rh" >&2
  exit 3
fi
ch="${YAM_CAN_CHANNEL:-}"
if [ -z "$ch" ]; then
  echo "geonosis-robot: YAM_CAN_CHANNEL is not set (~/.config/geonosis/robot.local.env)" >&2; exit 3
fi
state="$(cat "/sys/class/net/$ch/operstate" 2>/dev/null || echo missing)"
case "$state" in
  up|unknown) ;;
  *) echo "geonosis-robot: CAN $ch is $state (vrteleop-can.service brings it up at 1 Mbit/s)" >&2; exit 3 ;;
esac
echo "geonosis-robot: guard ok (vrteleop-rh inactive, $ch $state)"
