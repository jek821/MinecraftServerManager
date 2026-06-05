#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

# Create virtualenv if needed
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

# Set password via environment — change this or export MC_PASSWORD before running
export MC_PASSWORD="${MC_PASSWORD:-admin}"
export SECRET_KEY="${SECRET_KEY:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"

exec .venv/bin/python app.py
