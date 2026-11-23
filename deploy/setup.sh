#!/usr/bin/env bash
# Installs or updates this app INSIDE the LXC (run as root). Safe to run again and again.
# Also installs Piper (local speech) once, shared by all *-oefenen apps.
set -euo pipefail
export LANG=C.UTF-8 LC_ALL=C.UTF-8 DEBIAN_FRONTEND=noninteractive

APP=nederlands-oefenen
APP_USER=nederlands
PORT=8001
VOICE=nl_BE-nathalie-medium
VOICE_PATH=nl/nl_BE/nathalie/medium

DIR=/opt/$APP
DATA=/var/lib/$APP
say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

# ---- packages ----
missing=()
for p in python3 python3-venv git ca-certificates curl; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if [ ${#missing[@]} -gt 0 ]; then
  say "Installing ${missing[*]}"
  apt-get update -qq && apt-get install -y -qq "${missing[@]}" >/dev/null
fi

# ---- user + data ----
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$DATA" && chown -R "$APP_USER": "$DATA"

# ---- Piper (local, free speech) ----
if ! /opt/piper/bin/python -c "import piper" >/dev/null 2>&1; then
  say "Installing Piper speech engine (one time, ~1-2 minutes)"
  python3 -m venv /opt/piper
  if /opt/piper/bin/pip install -q --upgrade pip && /opt/piper/bin/pip install -q piper-tts; then
    say "Piper installed"
  else
    warn "Piper could not be installed. The app still works and uses the browser voice."
  fi
fi
mkdir -p /opt/piper/voices
for ext in onnx onnx.json; do
  f=/opt/piper/voices/$VOICE.$ext
  if [ ! -s "$f" ]; then
    say "Downloading voice $VOICE.$ext"
    curl -fsSL -o "$f.tmp" "https://huggingface.co/rhasspy/piper-voices/resolve/main/$VOICE_PATH/$VOICE.$ext" \
      && mv "$f.tmp" "$f" || { rm -f "$f.tmp"; warn "Voice download failed; the browser voice will be used."; }
  fi
done

# ---- shared speech settings (Azure is optional) ----
if [ ! -f /etc/oefenen-tts.env ]; then
  cat > /etc/oefenen-tts.env <<'ENV'
# Speech settings for all *-oefenen apps.
# Optional: Microsoft Azure neural voices (most natural). Free tier F0 = 500,000 characters/month;
# the apps cache every sound, so they use only a few thousand.
# Fill in both lines, then run:  update
#AZURE_TTS_KEY=paste-your-key-here
#AZURE_TTS_REGION=westeurope
ENV
  chmod 600 /etc/oefenen-tts.env
fi

# ---- service ----
cat > /etc/systemd/system/$APP.service <<UNIT
[Unit]
Description=$APP
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$DIR/app
Environment=DATA_DIR=$DATA PORT=$PORT QUIET=1 PIPER_MODEL=/opt/piper/voices/$VOICE.onnx
EnvironmentFile=-/etc/oefenen-tts.env
ExecStart=/usr/bin/python3 server.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT

# ---- helper commands (shared by all apps in this container) ----
cat > /usr/local/bin/update <<'UPD'
#!/usr/bin/env bash
# Updates every *-oefenen app in this container from GitHub. Scores and accounts are kept.
set -e
export LANG=C.UTF-8 LC_ALL=C.UTF-8
for d in /opt/*-oefenen; do
  [ -d "$d/.git" ] || continue
  echo "== $(basename "$d")"
  git -C "$d" pull --ff-only
  bash "$d/deploy/setup.sh"
done
UPD
cat > /usr/local/bin/backup <<'BAK'
#!/usr/bin/env bash
# Copies every app's database to /root (safe while running).
set -e
for db in /var/lib/*-oefenen/*.db; do
  out=/root/$(basename "$(dirname "$db")")-$(date +%F).db
  python3 -c "import sqlite3,sys;s=sqlite3.connect(sys.argv[1]);d=sqlite3.connect(sys.argv[2]);s.backup(d)" "$db" "$out"
  echo "Backup: $out"
done
BAK
chmod +x /usr/local/bin/update /usr/local/bin/backup

systemctl daemon-reload
systemctl enable "$APP" >/dev/null 2>&1
systemctl restart "$APP"
sleep 2
if systemctl is-active --quiet "$APP"; then
  say "$APP is running on port $PORT ($(journalctl -u "$APP" -n 20 --no-pager -o cat | grep -m1 '^Spraak' || echo 'speech: starting'))"
else
  warn "$APP did not start. See: journalctl -u $APP -n 50"
  exit 1
fi
mem=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
[ "$mem" -ge 900 ] || warn "This container has ${mem} MB RAM. For Piper speech give it 1024 MB (on the Proxmox host: pct set <ID> --memory 1024)."
