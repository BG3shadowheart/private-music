#!/bin/bash
set -e

# Launch slskd in the background
/app/slskd/slskd --config /app/slskd.yml --app-dir /data/slskd &
SLSKD_PID=$!

# Give slskd time to connect to Soulseek
sleep 8

# Launch the bot in the foreground
python /app/main.py &
BOT_PID=$!

# If either dies, kill the container
wait -n $SLSKD_PID $BOT_PID
