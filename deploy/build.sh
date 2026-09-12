#!/usr/bin/env bash
#
# Build command. Idempotent: safe to re-run on every deploy.
#
#   ./deploy/build.sh              # build in ./.venv
#   APP_DIR=/opt/wallet-monitor ./deploy/build.sh
#
# Produces a virtualenv with the app installed and verifies it before any
# process manager is pointed at it, so a broken build fails here rather than
# in a restart loop.
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV="${VENV:-$APP_DIR/.venv}"
PYTHON="${PYTHON:-python3}"

echo "==> Building in $APP_DIR"

# 3.11 is the floor: the config loader uses tomllib from the standard library.
"$PYTHON" - <<'PY'
import sys
if sys.version_info < (3, 11):
    sys.exit(f"Python 3.11+ required, found {sys.version.split()[0]}")
PY

echo "==> Creating the virtualenv"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip setuptools wheel

echo "==> Installing the application"
# The app has no third-party runtime dependencies; this installs the package
# itself and its console script.
"$VENV/bin/pip" install --quiet "$APP_DIR"

echo "==> Preparing the data directory"
mkdir -p "$APP_DIR/data"

echo "==> Verifying the build"
"$VENV/bin/wallet-monitor" --help >/dev/null
"$VENV/bin/python" -c "import wallet_monitor; print('   wallet-monitor', wallet_monitor.__version__)"

# The dashboard is served from files inside the package; a build that loses
# them starts fine and then serves a blank page, so check now.
"$VENV/bin/python" - <<'PY'
from pathlib import Path
import wallet_monitor
assets = Path(wallet_monitor.__file__).parent / "web_assets"
missing = [n for n in ("index.html", "app.js", "styles.css") if not (assets / n).is_file()]
if missing:
    raise SystemExit(f"   UI assets missing from the installed package: {missing}")
print("   UI assets present")
PY

echo "==> Build complete: $VENV/bin/wallet-monitor"
