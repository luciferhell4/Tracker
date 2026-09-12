#!/usr/bin/env bash
#
# Install Wallet Monitor on a fresh Hostinger VPS (Ubuntu/Debian).
#
#   curl -fsSL https://raw.githubusercontent.com/luciferhell4/Tracker/claude/wallet-monitor-early-mints-nmi5z3/deploy/install.sh | sudo bash -s -- monitor.example.com
#
# Or clone the repo and run:  sudo ./deploy/install.sh monitor.example.com
#
set -euo pipefail

DOMAIN="${1:-}"
REPO="${REPO:-https://github.com/luciferhell4/Tracker.git}"
BRANCH="${BRANCH:-claude/wallet-monitor-early-mints-nmi5z3}"
APP_DIR=/opt/wallet-monitor
ENV_FILE=/etc/wallet-monitor.env
HTPASSWD=/etc/nginx/.htpasswd-wallet-monitor

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root (sudo)." >&2
  exit 1
fi

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git nginx apache2-utils >/dev/null

echo "==> Creating the service user"
id -u wallet &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin wallet

echo "==> Fetching the code"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  git -C "$APP_DIR" checkout --quiet -B "$BRANCH" "origin/$BRANCH"
else
  rm -rf "$APP_DIR"
  git clone --quiet --branch "$BRANCH" "$REPO" "$APP_DIR"
fi

echo "==> Installing into a virtualenv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR"

mkdir -p "$APP_DIR/data"
chown -R wallet:wallet "$APP_DIR"

# Secrets live outside the repo and are readable only by root and the service.
if [[ ! -f "$ENV_FILE" ]]; then
  echo "==> Writing $ENV_FILE (fill in any keys you have)"
  cat > "$ENV_FILE" <<'ENV'
# Robinhood Chain, HyperEVM and Ink work with NO keys at all, over public RPC.
# These only widen coverage:
#   ALCHEMY_API_KEY   Ethereum, Base, Arbitrum, Abstract, Zora and friends
#   HELIUS_API_KEY    Solana
#   ETHERSCAN_API_KEY fallback for chains Etherscan V2 indexes
# ALCHEMY_API_KEY=
# HELIUS_API_KEY=
# ETHERSCAN_API_KEY=
# DISCORD_WEBHOOK_URL=
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_CHAT_ID=
ENV
  chmod 640 "$ENV_FILE"
  chown root:wallet "$ENV_FILE"
fi

echo "==> Importing the watchlist from the published Notion tables"
sudo -u wallet "$APP_DIR/.venv/bin/wallet-monitor" -c "$APP_DIR/config.toml" sync \
  || echo "    (import failed; run 'wallet-monitor sync' yourself once the VPS can reach notion.site)"

echo "==> Installing the service and the scan timer"
install -m 644 "$APP_DIR/deploy/wallet-monitor.service"      /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/wallet-monitor-scan.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/wallet-monitor-scan.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now wallet-monitor.service
systemctl enable --now wallet-monitor-scan.timer

if [[ -n "$DOMAIN" ]]; then
  echo "==> Configuring nginx for $DOMAIN"
  if [[ ! -f "$HTPASSWD" ]]; then
    PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 20)"
    htpasswd -bc "$HTPASSWD" admin "$PASSWORD" >/dev/null 2>&1
    chown root:www-data "$HTPASSWD"; chmod 640 "$HTPASSWD"
    echo
    echo "    Dashboard login -> user: admin   password: $PASSWORD"
    echo "    (shown once; change it with: htpasswd $HTPASSWD admin)"
    echo
  fi
  sed "s/monitor.example.com/$DOMAIN/g" "$APP_DIR/deploy/nginx.conf" \
    > /etc/nginx/sites-available/wallet-monitor
  ln -sf /etc/nginx/sites-available/wallet-monitor /etc/nginx/sites-enabled/wallet-monitor
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl reload nginx
  echo "    Now point $DOMAIN at this VPS in Hostinger's DNS (an A record to this IP),"
  echo "    then run:  certbot --nginx -d $DOMAIN"
else
  echo "==> No domain given, so nginx was left alone."
  echo "    The dashboard is on 127.0.0.1:8787 only. Re-run with a domain to expose it."
fi

echo
echo "==> Done."
systemctl --no-pager --lines=0 status wallet-monitor.service || true
echo "    Watchlist:  sudo -u wallet $APP_DIR/.venv/bin/wallet-monitor -c $APP_DIR/config.toml wallets | tail -1"
echo "    Logs:       journalctl -u wallet-monitor -f"
echo "    Scan now:   systemctl start wallet-monitor-scan.service"
