#!/usr/bin/env bash
set -euo pipefail

# Run on the VDS from /opt/aviapars after `git pull`.

cd "$(dirname "$0")/.."

uv venv --python 3.12 .venv 2>/dev/null || python3 -m venv .venv
source .venv/bin/activate

uv pip install -e . 2>/dev/null || pip install -e .

python -m aviapars.db.migrate

sudo systemctl restart aviapars
sudo systemctl status aviapars --no-pager
