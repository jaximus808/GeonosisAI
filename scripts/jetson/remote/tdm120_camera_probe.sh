#!/usr/bin/env bash
# TDM-120: find each RealSense's colour node on the northstar Jetson. Query-only (VIDIOC_QUERYCAP/ENUM_FMT
# via v4l2-ctl): it never streams. Refuses while vrteleop-rh runs, because vrteleop owns the cameras then.
#   bash tdm120_camera_probe.sh            # formats per node + the YUYV 640x480 node per camera
#   bash tdm120_camera_probe.sh --rs       # also rs-enumerate-devices -s (RealSense serial <-> USB port)
set -u
if systemctl is-active --quiet vrteleop-rh 2>/dev/null; then
  echo "vrteleop-rh is running and owns the cameras. Stop it first: sudo systemctl stop vrteleop-rh" >&2; exit 3
fi
busy="$(fuser /dev/video* 2>/dev/null | tr -s ' ')"
[ -n "$busy" ] && echo "note: some /dev/video* nodes are open by pids:$busy" >&2
for port in 1.1.3 1.1.2 1.2.2; do
  echo "== USB port 1-$port"
  for n in /dev/v4l/by-path/platform-3610000.usb-usb-0:"$port":*-video-index*; do
    [ -e "$n" ] || continue
    fmts="$(v4l2-ctl -d "$n" --list-formats 2>/dev/null | grep -oE "'[A-Z0-9 ]{4}'" | tr -d "'" | tr '\n' ' ')"
    yuyv640=""
    if grep -q YUYV <<<"$fmts" && v4l2-ctl -d "$n" --list-formats-ext 2>/dev/null | awk '/YUYV/{f=1} /\[[0-9]+\]/{if($0!~/YUYV/)f=0} f' | grep -q "640x480"; then yuyv640="  <- colour YUYV 640x480"; fi
    printf '  %-70s -> %-12s %s%s\n' "$(basename "$n")" "$(readlink -f "$n")" "${fmts:-(no capture formats: metadata node)}" "$yuyv640"
  done
done
if [ "${1:-}" = --rs ] && command -v rs-enumerate-devices >/dev/null; then
  echo "== rs-enumerate-devices -s"; rs-enumerate-devices -s 2>&1 | head -10
fi
