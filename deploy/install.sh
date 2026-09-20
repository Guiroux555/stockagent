#!/usr/bin/env bash
#
# Install stockagent as a system service on a Raspberry Pi (Compute Module 4
# and anything else running systemd).
#
#   sudo ./deploy/install.sh              install or upgrade, then start
#   sudo ./deploy/install.sh --no-start   install without starting
#   sudo ./deploy/install.sh --uninstall  remove the service, keep the data
#
# Idempotent: run it again after `git pull` and it upgrades in place. The
# account lives in /var/lib/stockagent and is never touched by this script,
# not even by --uninstall.
#
# What it does, and why each piece is here, is in deploy/README.md.

set -euo pipefail

AGENT=stockagent
PREFIX=/opt/${AGENT}
STATE=/var/lib/${AGENT}
UNIT_DIR=/etc/systemd/system
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

START=1
WATCHDOG=1
UNINSTALL=0

say()  { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --no-start)     START=0 ;;
    --no-watchdog)  WATCHDOG=0 ;;
    --uninstall)    UNINSTALL=1 ;;
    -h|--help)      sed -n '2,15p' "$0"; exit 0 ;;
    *)              die "unknown option: $1" ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || die "run me with sudo"
command -v systemctl >/dev/null || die "this installs a systemd service; no systemctl here"

# --- uninstall ---------------------------------------------------------------
if [ "$UNINSTALL" -eq 1 ]; then
  step "Removing the service"
  systemctl disable --now "${AGENT}.service" "${AGENT}-backup.timer" 2>/dev/null || true
  rm -f "${UNIT_DIR}/${AGENT}.service" "${UNIT_DIR}/${AGENT}-backup.service" \
        "${UNIT_DIR}/${AGENT}-backup.timer"
  systemctl daemon-reload
  say "removed. ${STATE} was left alone — the account, the price history and"
  say "the backups are all still there. Delete it by hand if you meant to."
  exit 0
fi

# --- prerequisites -----------------------------------------------------------
step "Checking the board"
PYTHON_BIN="$(command -v python3 || true)"
[ -n "$PYTHON_BIN" ] || die "python3 not found — apt install python3 python3-venv"
"$PYTHON_BIN" - <<'PY' || die "python 3.10 or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
"$PYTHON_BIN" -c 'import venv' 2>/dev/null || die "python3-venv is missing — apt install python3-venv"
say "python $("$PYTHON_BIN" -c 'import platform;print(platform.python_version())') on $(uname -m)"

# --- the service user --------------------------------------------------------
step "User"
if id -u "$AGENT" >/dev/null 2>&1; then
  say "$AGENT already exists"
else
  useradd --system --home-dir "$STATE" --no-create-home --shell /usr/sbin/nologin "$AGENT"
  say "created system user $AGENT (no login, no home)"
fi

# --- the code ----------------------------------------------------------------
step "Code"
mkdir -p "$PREFIX"
# Copied rather than symlinked: the unit sets ProtectHome=true, so a checkout
# under /home would be invisible to the service. Re-running this script after a
# `git pull` is the upgrade path.
tar -C "$SRC" \
    --exclude=.git --exclude=.venv --exclude=data --exclude=logs \
    --exclude=__pycache__ --exclude='*.pyc' --exclude=.pytest_cache \
    --exclude=.ruff_cache --exclude='*.egg-info' \
    -cf - . | tar -C "$PREFIX" -xf -
say "installed to $PREFIX"

if [ ! -x "${PREFIX}/.venv/bin/python" ]; then
  "$PYTHON_BIN" -m venv "${PREFIX}/.venv"
  say "created ${PREFIX}/.venv"
fi
# Nothing is installed into it: the project has no runtime dependencies, and a
# venv that needs the internet to be rebuilt is one more thing that can fail on
# a board whose whole point is coping without it.
chown -R root:root "$PREFIX"
chmod -R a+rX "$PREFIX"

install -d -o "$AGENT" -g "$AGENT" -m 0750 "$STATE"

# --- the clock ---------------------------------------------------------------
# The Compute Module has no battery-backed clock. Without NTP it comes back
# from a power cut believing it is whenever it last shut down, and every
# schedule the agent keeps is wall-clock arithmetic.
step "Clock"
if systemctl list-unit-files systemd-timesyncd.service >/dev/null 2>&1; then
  systemctl enable --now systemd-timesyncd.service >/dev/null 2>&1 || true
  say "systemd-timesyncd enabled"
  if systemctl list-unit-files systemd-time-wait-sync.service >/dev/null 2>&1; then
    # Makes time-sync.target mean "NTP has actually answered" rather than
    # "the time service started", which is what the unit orders itself after.
    systemctl enable systemd-time-wait-sync.service >/dev/null 2>&1 || true
    say "systemd-time-wait-sync enabled — time-sync.target now means synchronised"
  fi
else
  say "no systemd-timesyncd; make sure chrony or ntpd is running"
fi

# --- the logs ----------------------------------------------------------------
# A week-long outage is a week of retry messages. On an SD card, an unbounded
# journal is a full filesystem, and a full filesystem is an agent that cannot
# write its own database.
step "Logs"
install -d -m 0755 /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/90-agents.conf <<'CONF'
# Installed by stockagent/deploy/install.sh
[Journal]
Storage=persistent
SystemMaxUse=200M
SystemMaxFileSize=20M
MaxRetentionSec=1month
CONF
systemctl restart systemd-journald >/dev/null 2>&1 || true
say "journal capped at 200 MB, kept across reboots"

# --- the hardware watchdog ---------------------------------------------------
# systemd's WatchdogSec catches an agent that hangs. This catches the kernel
# hanging underneath it, which no amount of Restart=always can.
if [ "$WATCHDOG" -eq 1 ]; then
  step "Hardware watchdog"
  install -d -m 0755 /etc/systemd/system.conf.d
  cat > /etc/systemd/system.conf.d/90-watchdog.conf <<'CONF'
# Installed by stockagent/deploy/install.sh
# The BCM2711 has a hardware watchdog. Ping it every 15s; if the kernel stops
# pinging, the board reboots itself. Run with --no-watchdog to skip this.
[Manager]
RuntimeWatchdogSec=15s
RebootWatchdogSec=2min
CONF
  systemctl daemon-reexec
  say "board will reboot itself if the kernel stops responding for 15s"
fi

# --- the units ---------------------------------------------------------------
step "Service"
install -m 0644 "${PREFIX}/deploy/${AGENT}.service"        "${UNIT_DIR}/"
install -m 0644 "${PREFIX}/deploy/${AGENT}-backup.service" "${UNIT_DIR}/"
install -m 0644 "${PREFIX}/deploy/${AGENT}-backup.timer"   "${UNIT_DIR}/"
systemctl daemon-reload
systemctl enable "${AGENT}.service" "${AGENT}-backup.timer" >/dev/null
say "enabled at boot: ${AGENT}.service, ${AGENT}-backup.timer"

if [ "$START" -eq 1 ]; then
  systemctl restart "${AGENT}.service"
  systemctl start "${AGENT}-backup.timer"
  say "started"
fi

step "Done"
cat <<EOF
  state      ${STATE}/live.db  (backups in ${STATE}/backups)
  follow     journalctl -u ${AGENT} -f
  check      systemctl status ${AGENT}
  health     sudo -u ${AGENT} ${PREFIX}/.venv/bin/python -m trader \\
               --db ${STATE}/live.db health

  It is paper trading. There are no credentials anywhere in this install and
  no code path that could place a real order.
EOF
