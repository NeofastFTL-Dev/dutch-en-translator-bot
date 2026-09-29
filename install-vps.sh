#!/usr/bin/env bash
# One-shot installer for Ubuntu VPS.
#   curl -fsSL https://raw.githubusercontent.com/NeofastFTL-Dev/dutch-en-translator-bot/main/install-vps.sh | sudo bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo."
  exit 1
fi

apt-get update
apt-get install -y git python3 python3-venv python3-pip ffmpeg libopus0

WORKDIR=/tmp/dutch-en-translator-bot
rm -rf "$WORKDIR"
git clone --depth 1 https://github.com/NeofastFTL-Dev/dutch-en-translator-bot.git "$WORKDIR"
cd "$WORKDIR"
bash deploy/ubuntu-setup.sh
