#!/usr/bin/env bash
set -Eeuo pipefail
cd "/home/pi/PiBook"

echo "Modelo:"
tr -d '\0' </proc/device-tree/model 2>/dev/null || true
echo

echo "SPI:"
ls -l /dev/spidev* 2>/dev/null || true
echo

echo "I2C:"
ls -l /dev/i2c-* 2>/dev/null || true
sudo i2cdetect -y 1 || true
echo

echo "Importações:"
"/home/pi/PiBook/.venv/bin/python" - <<'PY'
from PIL import Image
from ebooklib import epub
from bs4 import BeautifulSoup
from flask import Flask
import yaml
import spidev
import gpiozero
import smbus

import sys
sys.path.insert(0, "lib")
from waveshare_epd import epd7in5_V2

epd = epd7in5_V2.EPD()
print("Dependências principais: OK")
print(f"Driver Waveshare: OK ({epd.width}x{epd.height})")
PY
