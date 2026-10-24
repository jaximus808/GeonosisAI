# TDM-44: arm64 Ubuntu 22.04 stand-in for a JetPack 6 Jetson (L4T R36 = Ubuntu 22.04), reachable over
# ssh, with systemd as PID 1 so the unit files really load. Not L4T: no NVIDIA kernel, no CAN/camera
# hardware, no nvidia-l4t-* packages. Used by scripts/jetson/test/run_docker_test.sh.
FROM --platform=linux/arm64 ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      openssh-server sudo python3 ca-certificates systemd systemd-sysv dbus \
    && rm -rf /var/lib/apt/lists/* \
    && systemctl enable ssh \
    && systemctl mask getty.target console-getty.service systemd-logind.service \
    && useradd -m -s /bin/bash -G sudo jetson \
    && printf 'jetson ALL=(ALL) NOPASSWD:ALL\n' >/etc/sudoers.d/jetson && chmod 440 /etc/sudoers.d/jetson \
    && printf '# R36 (release), REVISION: 3.0, GCID: 0, BOARD: generic, EABI: aarch64, DATE: TDM-44 container stand-in\n' >/etc/nv_tegra_release
COPY authorized_keys /home/jetson/.ssh/authorized_keys
RUN chown -R jetson:jetson /home/jetson/.ssh && chmod 700 /home/jetson/.ssh && chmod 600 /home/jetson/.ssh/authorized_keys
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
