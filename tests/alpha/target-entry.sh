#!/bin/sh
# Install the hub's public key for `lab`, start sshd, then run dockerd in the
# foreground (its socket is group "docker", so `lab` can use it).
set -e
mkdir -p /home/lab/.ssh
if [ -n "$AUTHORIZED_KEY" ]; then echo "$AUTHORIZED_KEY" > /home/lab/.ssh/authorized_keys; fi
chown -R lab:lab /home/lab/.ssh && chmod 700 /home/lab/.ssh && chmod 600 /home/lab/.ssh/authorized_keys 2>/dev/null || true
/usr/sbin/sshd
exec dockerd-entrypoint.sh dockerd --host=unix:///var/run/docker.sock --group docker
