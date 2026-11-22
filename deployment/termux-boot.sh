#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd "$HOME/argus-master" || exit 1
exec .venv/bin/python server.py >> "$HOME/argus-startup.log" 2>&1
