#!/usr/bin/env bash
# Usage: ./bootstrap_ssh.sh <inventory_hostname> [bootstrap_ip]
set -euo pipefail

HOST="${1:?Usage: $0 <inventory_hostname> [bootstrap_ip]}"
BOOT_IP="${2:-192.168.1.1}"
PUBKEY="${PUBKEY:-$HOME/.ssh/id_ed25519.pub}"

ssh-keygen -R "$BOOT_IP" >/dev/null 2>&1 || true
cat "$PUBKEY" | ssh -o StrictHostKeyChecking=no "root@${BOOT_IP}" \
  "mkdir -p /etc/dropbear && cat >> /etc/dropbear/authorized_keys && chmod 0600 /etc/dropbear/authorized_keys"

ansible-playbook site.yml --limit "$HOST" -e "bootstrap_ip=${BOOT_IP}" --ask-vault-pass
