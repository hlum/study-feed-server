#!/bin/sh
# Start the API bound to this machine's Tailscale IPv4 only (never 0.0.0.0).
# In the Docker stack, TAILSCALE_IP is set and we wait for that address to exist,
# because Docker can start before Tailscale at boot. On the host, we ask tailscale.
set -eu
cd "$(dirname "$0")"
PORT=$(sed -n 's/^PORT=//p' .env | tail -n1)
PORT=${PORT:-8000}

if [ -n "${TAILSCALE_IP:-}" ]; then
  IP=$TAILSCALE_IP
  PY=python
  i=0
  until $PY -c "import socket,sys; s=socket.socket(); s.bind(('$IP', 0))" 2>/dev/null; do
    i=$((i + 1))
    [ "$i" -ge 120 ] && { echo "Tailscale IP $IP never appeared; exiting so Docker restarts us" >&2; exit 1; }
    [ "$i" -eq 1 ] && echo "waiting for Tailscale IP $IP ..." >&2
    sleep 2
  done
else
  IP=$(tailscale ip -4 2>/dev/null | head -n1)
  PY=.venv/bin/python
  case "$IP" in
    100.*) ;;
    *) echo "Tailscale IPv4 not available yet (got '$IP'); exiting so systemd retries" >&2; exit 1 ;;
  esac
fi

exec $PY -m uvicorn app:app --host "$IP" --port "$PORT" --workers 1
