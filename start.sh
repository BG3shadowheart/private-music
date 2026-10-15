#!/bin/bash
set -e

mkdir -p /data/downloads /data/incomplete /data/slskd

/app/slskd/slskd --config /app/slskd.yml --app-dir /data/slskd > /tmp/slskd.log 2>&1 &
SLSKD_PID=$!

sleep 8

python /app/main.py &
BOT_PID=$!

wait -n $SLSKD_PID $BOT_PID
