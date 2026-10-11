#!/bin/sh
# Options come from /data/options.json (written by Supervisor from the add-on configuration).
set -e
cd /app/bridge
exec python server.py
