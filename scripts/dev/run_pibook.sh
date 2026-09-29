#!/usr/bin/env bash
set -Eeuo pipefail
cd "/home/pi/PiBook"
export PIBOOK_CONFIG="/home/pi/PiBook/config/config.yaml"
exec "/home/pi/PiBook/.venv/bin/python" "/home/pi/PiBook/src/main.py"
