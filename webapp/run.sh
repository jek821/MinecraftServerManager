#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

# Create virtualenv if needed
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

# Set these via environment or export before running
export MC_PASSWORD="${MC_PASSWORD:-admin}"
export SERVER_NAME="${SERVER_NAME:-MC}"
# SECRET_KEY is persisted to .secret_key file by app.py — no need to generate here

LOCAL_IP=$(.venv/bin/python3 -c "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); print(s.getsockname()[0]); s.close()" 2>/dev/null || echo "localhost")
echo "Minecraft Server Manager running at:"
echo "  Local:   http://localhost:5000"
echo "  Network: http://$LOCAL_IP:5000"

exec .venv/bin/python app.py
