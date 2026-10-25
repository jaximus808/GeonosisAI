#!/usr/bin/env bash
# TDM-119: run a bring-up step on the Jetson INTERACTIVELY under the user systemd manager, so the i2rt fork's
# 250 Hz CAN thread gets SCHED_FIFO (the user manager has RTPRIO 20; a Tailscale ssh shell has 0, where the
# fork only warns and runs at normal priority). Env: robot.local.env + teleop.env (no secrets needed).
#   tdm119_rt.sh preflight | hold [--seconds 10] | jog [--ready] [--rest-at-end] | sign-off | verified-check
#   tdm119_rt.sh passive-read [SECONDS] [--channel can_left|can_right]   # TDM-118 joints-only read (no enable)
# Passes YAM_ALLOW_GRIPPER_CALIBRATION / YAM_GRIPPER_LIMITS through when set in your shell.
set -euo pipefail
APP="$HOME/geonosis/app/services/robot-client"; CFG="$HOME/.config/geonosis"
mod=rdf_robot_client.tdm119_bringup; args=("$@")
if [ "${1:-}" = passive-read ]; then mod=rdf_robot_client.tdm118_readonly; args=(--passive-read "${2:-10}" "${@:3}"); fi
props=(-p "EnvironmentFile=$CFG/robot.local.env" -p "EnvironmentFile=-$CFG/teleop.env" -E PYTHONUNBUFFERED=1)
for v in YAM_ALLOW_GRIPPER_CALIBRATION YAM_GRIPPER_LIMITS; do
  [ -n "${!v:-}" ] && props+=(-E "$v=${!v}")
done
exec systemd-run --user --pty --wait --collect --quiet --unit="tdm119-step-$$" \
  --working-directory="$APP" "${props[@]}" "$APP/.venv/bin/python" -m "$mod" "${args[@]}"
