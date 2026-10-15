#!/bin/bash
set -e

# Create required directories inside the mounted volume
mkdir -p /data/downloads /data/incomplete /data/slskd

# slskd does not expand ${VAR} placeholders in YAML itself. Render the
# secrets from Railway's environment before starting it, with YAML-safe quoting.
python - <<'PY'
import json
import os
import re
from pathlib import Path

template = Path("/app/slskd.yml").read_text()

def replace_env(match):
    name = match.group(1)
    value = os.environ.get(name)
    if value is None or value == "":
        raise SystemExit(f"Required Railway variable {name} is missing")
    return json.dumps(value)

rendered = re.sub(r"\$\{([A-Z0-9_]+)\}", replace_env, template)
Path("/tmp/slskd.yml").write_text(rendered)
PY

/app/slskd/slskd --config /tmp/slskd.yml --app-dir /data/slskd &
SLSKD_PID=$!

sleep 8

python /app/main.py &
BOT_PID=$!

wait -n $SLSKD_PID $BOT_PID
