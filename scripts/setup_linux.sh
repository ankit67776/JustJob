#!/usr/bin/env bash
# Sets up Just Job on a Linux server with systemd (Ubuntu 24.04 / Debian 12 or newer):
#   - the dashboard as an always-on service, listening on this machine only (127.0.0.1:8765)
#   - the full run every 4 hours, and a refresh of companies with recent matches every hour
#   - optionally HTTPS on your domain through Caddy, which gets the certificate by itself
#
# From the project folder, after putting DASHBOARD_PASSWORD (and your keys) in .env:
#   bash scripts/setup_linux.sh                              # reach the dashboard through an SSH tunnel
#   DOMAIN=jobs.example.com bash scripts/setup_linux.sh      # or at https://jobs.example.com
# Safe to run again, e.g. after changing the domain.
set -euo pipefail

cd "$(dirname "$0")/.."
DIR="$(pwd)"
RUN_USER="$(id -un)"
PY="$(command -v python3 || true)"
PORT=8765

fail() { echo "setup: $*" >&2; exit 1; }

command -v systemctl >/dev/null || fail "this needs a Linux server with systemd"
[ -n "$PY" ] || fail "python3 not found; install it with: sudo apt-get install -y python3"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || fail "Python 3.11 or newer is needed (found $("$PY" -V)); Ubuntu 24.04 and Debian 12 have it"
if [ ! -f .env ]; then
    cp .env.example .env
    fail "created .env from .env.example; set DASHBOARD_PASSWORD (and your API keys) in it, then run this again"
fi
"$PY" -c 'import os, sys; from justjob.config import load_config; load_config(); sys.exit(not os.environ.get("DASHBOARD_PASSWORD"))' \
    || fail "set DASHBOARD_PASSWORD in .env first: the dashboard will be reachable from outside this machine"
mkdir -p data

echo "Writing systemd units for $RUN_USER in $DIR"
unit() { sudo tee "/etc/systemd/system/$1" >/dev/null; }

unit justjob-web.service <<EOF
[Unit]
Description=Just Job dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY -m justjob serve --no-browser --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=5
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF

unit justjob-run.service <<EOF
[Unit]
Description=Just Job full run (fetch, filter, AI, digest)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY -m justjob run
TimeoutStartSec=3h
Nice=5
NoNewPrivileges=yes
EOF

unit justjob-run.timer <<EOF
[Unit]
Description=Just Job full run every 4 hours

[Timer]
OnCalendar=*-*-* 00/4:00:00
Persistent=true
RandomizedDelaySec=2min

[Install]
WantedBy=timers.target
EOF

unit justjob-hot.service <<EOF
[Unit]
Description=Just Job refresh of companies with recent matches
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY -m justjob run --hot
TimeoutStartSec=45min
Nice=5
NoNewPrivileges=yes
EOF

# Half past every hour, away from the full runs; if they still meet, the refresh skips its turn.
unit justjob-hot.timer <<EOF
[Unit]
Description=Just Job refresh every hour

[Timer]
OnCalendar=*-*-* *:30:00

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now justjob-web.service justjob-run.timer justjob-hot.timer
sudo systemctl restart justjob-web.service

if [ -n "${DOMAIN:-}" ]; then
    if ! command -v caddy >/dev/null; then
        echo "Installing Caddy"
        sudo apt-get update -y
        if ! sudo apt-get install -y caddy; then   # older releases: Caddy's own package repository
            sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
            curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
                | sudo gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
            curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
                | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
            sudo apt-get update -y
            sudo apt-get install -y caddy
        fi
    fi
    sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:$PORT
}
EOF
    # Oracle Cloud's Ubuntu images block incoming web traffic in iptables, not just in the cloud firewall.
    reject_at="$(sudo iptables -L INPUT -n --line-numbers 2>/dev/null | awk '$2 == "REJECT" {print $1; exit}')"
    if [ -n "$reject_at" ]; then
        for p in 80 443; do   # ahead of the REJECT rule, or they'd never be reached
            sudo iptables -C INPUT -p tcp --dport "$p" -m state --state NEW -j ACCEPT 2>/dev/null \
                || sudo iptables -I INPUT "$reject_at" -p tcp --dport "$p" -m state --state NEW -j ACCEPT
        done
        command -v netfilter-persistent >/dev/null && sudo netfilter-persistent save >/dev/null || true
    fi
    sudo systemctl enable caddy >/dev/null 2>&1 || true
    sudo systemctl reload caddy 2>/dev/null || sudo systemctl restart caddy
fi

echo
echo "Done."
systemctl list-timers 'justjob*' --no-pager || true
echo
if [ -n "${DOMAIN:-}" ]; then
    echo "Dashboard: https://$DOMAIN  (the certificate can take a minute on first visit)"
    echo "The domain must point at this server, and ports 80 and 443 must be open in your cloud firewall."
else
    echo "Dashboard: from your computer run   ssh -L $PORT:127.0.0.1:$PORT $RUN_USER@<this-server>"
    echo "           then open http://127.0.0.1:$PORT"
fi
echo "Logs:      journalctl -u justjob-run -n 50     (full runs)"
echo "           journalctl -u justjob-hot -n 50     (hourly refresh)"
echo "Run now:   sudo systemctl start justjob-run    (first full run takes about 40 minutes)"
