#!/usr/bin/env bash
# Remove what belongs to the build machine before its disk becomes the AstraBox
# cloud image: logs, shell history, SSH keys, cloud-init state and the machine
# ID. Each machine started from the image creates its own on its first boot.
# Run it last; the machine is not meant to be logged into again afterwards.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run as root." >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get -q -y autoremove --purge
apt-get -q -y clean
rm -rf /var/lib/apt/lists/*

# Nothing of AstraBox runs yet: the first boot starts it.
rm -rf /var/lib/astrabox-cloud /root/astrabox-credentials.txt /var/log/astrabox-first-boot.log
rm -f /etc/astrabox/cloud.env

# Keys and credentials of whoever built the image.
rm -f /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys
rm -f /etc/ssh/ssh_host_*
rm -f /root/.bash_history /home/*/.bash_history /root/.lesshst /root/.viminfo
rm -rf /root/.docker /root/.cache /tmp/* /var/tmp/*

# No password login for root, as the marketplaces require; cloud-init installs
# the launching user's key.
passwd -l root >/dev/null 2>&1 || true

# Logs, and the identity cloud-init and systemd give each machine.
find /var/log -type f -exec truncate -s 0 {} +
if command -v cloud-init >/dev/null 2>&1; then
  cloud-init clean --logs --seed
fi
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
ln -sf /etc/machine-id /var/lib/dbus/machine-id

sync
echo "Clean. Stop this machine and create the image now."
