#!/bin/bash
# SMM Bot start script for AWS EC2

cd "$(dirname "$0")"

# Kill existing screen session if running
screen -S smmbot -X quit 2>/dev/null
sleep 1

# Install deps if needed
pip install -r requirements.txt --break-system-packages -q

# Start in screen session
screen -dmS smmbot bash -c "python bot.py 2>&1 | tee -a bot.log"

echo "✅ SMM Bot started in screen session 'smmbot'"
echo "Use: screen -r smmbot  to attach"
