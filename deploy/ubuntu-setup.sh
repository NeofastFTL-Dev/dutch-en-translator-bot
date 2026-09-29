#!/usr/bin/env bash
set -euo pipefail
APP_DIR=/opt/dutch-en-translator-bot
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Please run with sudo."
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip ffmpeg libopus0 rsync git

id -u translator >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin translator

mkdir -p "$APP_DIR"
rsync -a --delete --exclude '.venv' --exclude '__pycache__' --exclude '.git' "$SRC_DIR/" "$APP_DIR/"

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "Created $APP_DIR/.env — edit it before starting the service."
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

install -m 644 "$APP_DIR/deploy/translator.service" /etc/systemd/system/translator.service
chown -R translator:translator "$APP_DIR"
chmod 600 "$APP_DIR/.env" || true

systemctl daemon-reload
systemctl enable translator.service

echo
echo "Edit secrets next:"
echo "  sudo nano $APP_DIR/.env"
echo "Then:"
echo "  sudo systemctl start translator"
echo "  sudo systemctl status translator"
