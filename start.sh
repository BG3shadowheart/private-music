#!/bin/bash
set -e

/app/slskd/slskd --config /app/slskd.yml --app-dir /data/slskd &
SLSKD_PID=$!

sleep 8

python /app/main.py &
BOT_PID=$!

wait -n $SLSKD_PID $BOT_PID
