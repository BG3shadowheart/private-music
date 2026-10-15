#!/bin/bash
set -e

# Create required directories inside the mounted volume
mkdir -p /data/downloads /data/incomplete /data/slskd

/app/slskd/slskd --config /app/slskd.yml --app-dir /data/slskd &
SLSKD_PID=$!

sleep 8

python /app/main.py &
BOT_PID=$!

wait -n $SLSKD_PID $BOT_PID
