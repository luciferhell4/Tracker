#!/usr/bin/env bash
#
# Install Wallet Monitor on a fresh Hostinger VPS (Ubuntu/Debian).
#
#   sudo ./deploy/install.sh                       loopback only, reach it by SSH tunnel
#   sudo ./deploy/install.sh --temporary           serve on the VPS IP / hstgr.cloud hostname
#   sudo ./deploy/install.sh monitor.example.com   serve on your own domain
#
# --temporary is the "no domain yet" option: nginx answers on whatever address
# the request arrived on, behind basic auth and a self-signed certificate.
#
set -euo pipefail

MODE="loopback"
DOMAIN=""
case "${1:-}" in
  "")            MODE="loopback" ;;
  --temporary|--temp|--ip|--public) MODE="temporary" ;;
  -*)            echo "Unknown option: $1" >&2; exit 1 ;;
  *)             MODE="domain"; DOMAIN="$1" ;;
esac
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
# Run from APP_DIR: config.toml points the database at a RELATIVE path, so the
# working directory decides where it lands. Importing from anywhere else writes
# the wallets to a database the service never opens, and the dashboard then
# comes up empty with nothing obviously wrong.
( cd "$APP_DIR" && sudo -u wallet "$APP_DIR/.venv/bin/wallet-monitor" sync ) \
  || echo "    (import failed; run it yourself later: cd $APP_DIR && sudo -u wallet .venv/bin/wallet-monitor sync)"

if [[ -f "$APP_DIR/data/tracker.sqlite" ]]; then
  IMPORTED="$( cd "$APP_DIR" && sudo -u wallet "$APP_DIR/.venv/bin/wallet-monitor" wallets 2>/dev/null | tail -1 )"
  echo "    ${IMPORTED:-watchlist empty}"
fi

echo "==> Installing the service and the scan timer"
install -m 644 "$APP_DIR/deploy/wallet-monitor.service"      /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/wallet-monitor-scan.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/wallet-monitor-scan.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now wallet-monitor.service
systemctl enable --now wallet-monitor-scan.timer

make_password() {
  if [[ -f "$HTPASSWD" ]]; then
    return
  fi
  PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 20)"
  htpasswd -bc "$HTPASSWD" admin "$PASSWORD" >/dev/null 2>&1
  chown root:www-data "$HTPASSWD"; chmod 640 "$HTPASSWD"
  echo
  echo "    Dashboard login -> user: admin   password: $PASSWORD"
  echo "    (shown once; change it with: htpasswd $HTPASSWD admin)"
  echo
}

case "$MODE" in
  domain)
    echo "==> Configuring nginx for $DOMAIN"
    make_password
    sed "s/monitor.example.com/$DOMAIN/g" "$APP_DIR/deploy/nginx.conf" \
      > /etc/nginx/sites-available/wallet-monitor
    ln -sf /etc/nginx/sites-available/wallet-monitor /etc/nginx/sites-enabled/wallet-monitor
    rm -f /etc/nginx/sites-enabled/default
    nginx -t && systemctl reload nginx
    echo "    Point $DOMAIN at this VPS in Hostinger's DNS (an A record to this IP),"
    echo "    then run:  certbot --nginx -d $DOMAIN"
    ;;

  temporary)
    echo "==> Configuring nginx on this server's own address"
    make_password

    # A self-signed certificate so the dashboard password is not sent in the
    # clear. Certbot cannot issue for a bare IP, so this is the honest option
    # until a real domain points here.
    if [[ ! -f /etc/nginx/ssl/wallet-monitor.crt ]]; then
      mkdir -p /etc/nginx/ssl
      openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
        -keyout /etc/nginx/ssl/wallet-monitor.key \
        -out    /etc/nginx/ssl/wallet-monitor.crt \
        -subj "/CN=$(hostname -f 2>/dev/null || hostname)" >/dev/null 2>&1
      chmod 600 /etc/nginx/ssl/wallet-monitor.key
    fi

    install -m 644 "$APP_DIR/deploy/nginx-temporary.conf" \
      /etc/nginx/sites-available/wallet-monitor
    ln -sf /etc/nginx/sites-available/wallet-monitor /etc/nginx/sites-enabled/wallet-monitor
    rm -f /etc/nginx/sites-enabled/default
    nginx -t && systemctl reload nginx

    PUBLIC_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    HOSTNAME_FQDN="$(hostname -f 2>/dev/null || true)"
    echo
    echo "    Open the dashboard at:"
    [[ -n "$HOSTNAME_FQDN" ]] && echo "      https://$HOSTNAME_FQDN/"
    [[ -n "$PUBLIC_IP"     ]] && echo "      https://$PUBLIC_IP/"
    echo
    echo "    The certificate is self-signed, so the browser warns once. That is"
    echo "    expected without a domain. When you have one, point it here and run:"
    echo "      certbot --nginx -d your.domain"
    ;;

  loopback)
    echo "==> No address given, so nginx was left alone."
    echo "    The dashboard is on 127.0.0.1:8787 only. Reach it with an SSH tunnel:"
    echo "      ssh -L 8787:127.0.0.1:8787 root@<this-server>"
    echo "    Or re-run with --temporary to serve it on this server's own address."
    ;;
esac

echo
echo "==> Done."
systemctl --no-pager --lines=0 status wallet-monitor.service || true
echo "    Watchlist:  sudo -u wallet $APP_DIR/.venv/bin/wallet-monitor -c $APP_DIR/config.toml wallets | tail -1"
echo "    Logs:       journalctl -u wallet-monitor -f"
echo "    Scan now:   systemctl start wallet-monitor-scan.service"
