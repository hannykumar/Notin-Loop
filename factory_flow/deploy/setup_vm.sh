#!/usr/bin/env bash
# One-shot setup of the live demo on a fresh Ubuntu 24.04 VM (run as a sudo user, from anywhere):
#   DASHBOARD_PASSWORD='...' bash setup_vm.sh
# Then copy the LLM settings:  scp factory_flow/.env <user>@<ip>:/opt/notin-loop/factory_flow/.env
#
# Public:  https://<ip-with-dashes>.sslip.io  (Caddy: automatic HTTPS + basic auth, user "judge")
# Private: dashboard :8099, stable :8101, canary :8102 stay on 127.0.0.1.
set -euo pipefail
: "${DASHBOARD_PASSWORD:?set DASHBOARD_PASSWORD}"
REPO_URL="${REPO_URL:-https://github.com/hannykumar/Notin-Loop}"
APP_DIR=/opt/notin-loop
RUN_USER="$(id -un)"

sudo apt-get update -qq
sudo apt-get install -y -qq git python3.12 python3.12-venv debian-keyring debian-archive-keyring apt-transport-https curl
if ! command -v caddy >/dev/null; then   # official Caddy apt repo
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y -qq caddy
fi

sudo mkdir -p "$APP_DIR" && sudo chown "$RUN_USER" "$APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then git -C "$APP_DIR" pull -q --ff-only; else git clone -q "$REPO_URL" "$APP_DIR"; fi
python3.12 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/factory_flow/requirements.txt"
git config --global user.email >/dev/null || git config --global user.email "sre-factory@localhost"
git config --global user.name  >/dev/null || git config --global user.name  "sre-factory"

# systemd: the agent + target service + dashboard, restarted on failure
sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__USER__|$RUN_USER|g" "$APP_DIR/factory_flow/deploy/notin-loop.service" \
  | sudo tee /etc/systemd/system/notin-loop.service >/dev/null

# Caddy: HTTPS on <ip>.sslip.io, basic auth on everything, proxy to the dashboard
PUBLIC_IP="$(curl -s https://api.ipify.org)"
HOST="${PUBLIC_IP//./-}.sslip.io"
HASH="$(caddy hash-password --plaintext "$DASHBOARD_PASSWORD")"
sed -e "s|__HOST__|$HOST|g" -e "s|__HASH__|$HASH|g" "$APP_DIR/factory_flow/deploy/Caddyfile" | sudo tee /etc/caddy/Caddyfile >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable -q notin-loop caddy
sudo systemctl restart caddy
if [[ -f "$APP_DIR/factory_flow/.env" ]]; then sudo systemctl restart notin-loop; else
  echo "!! copy factory_flow/.env to $APP_DIR/factory_flow/.env, then: sudo systemctl restart notin-loop"; fi
echo "public URL: https://$HOST   (user: judge)"
