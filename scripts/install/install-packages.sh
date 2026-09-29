#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PIBOOK_USER="${PIBOOK_USER:-pi}"
VENV="${PROJECT_DIR}/.venv"

if [ "$EUID" -ne 0 ]; then
    echo "ERRO: executar como root:"
    echo "  sudo $0"
    exit 1
fi

if ! id "$PIBOOK_USER" >/dev/null 2>&1; then
    echo "ERRO: utilizador '$PIBOOK_USER' não existe."
    exit 1
fi

echo "=== PiBook: pacotes de sistema ==="

PACKAGES=(
    python3
    python3-venv
    python3-dev

    python3-pil
    python3-gpiozero
    python3-spidev
    python3-yaml
    python3-flask
    python3-ebooklib
    python3-bs4
    python3-cairosvg
    python3-evdev
    python3-smbus
    python3-smbus2
    python3-libgpiod
    python3-lgpio
    python3-rpi.gpio

    git
    gcc
    build-essential
    patch
    binutils

    initramfs-tools
    initramfs-tools-core
    cpio
    zstd

    network-manager
    hostapd
    dnsmasq-base
    iw
    rfkill
    iproute2
    arp-scan
    nmap
    avahi-utils

    i2c-tools

    libcairo2
    fonts-dejavu
    fonts-dejavu-core
)

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y "${PACKAGES[@]}"

echo
echo "=== PiBook: virtual environment ==="

if [ -e "$VENV" ]; then
    if grep -q '^include-system-site-packages = true$' \
        "$VENV/pyvenv.cfg" 2>/dev/null
    then
        echo "Venv existente usa system-site-packages: OK"
    else
        echo "ERRO: $VENV já existe mas não usa system-site-packages."
        echo "Não será substituída automaticamente."
        exit 1
    fi
else
    sudo -u "$PIBOOK_USER" \
        python3 -m venv --system-site-packages "$VENV"

    echo "Venv criada: $VENV"
fi

echo
echo "=== PiBook: validar módulos Python ==="

sudo -u "$PIBOOK_USER" "$VENV/bin/python" - <<'PY'
import importlib

modules = [
    "PIL",
    "gpiozero",
    "spidev",
    "yaml",
    "flask",
    "ebooklib",
    "bs4",
    "cairosvg",
    "evdev",
    "smbus",
    "smbus2",
    "gpiod",
    "lgpio",
    "RPi.GPIO",
]

failed = []

for module in modules:
    try:
        importlib.import_module(module)
        print(f"OK   {module}")
    except Exception as exc:
        failed.append((module, str(exc)))
        print(f"FAIL {module}: {exc}")

if failed:
    raise SystemExit(
        f"{len(failed)} módulo(s) Python não ficaram disponíveis."
    )

print("Todos os módulos obrigatórios estão disponíveis.")
PY

echo
echo "install-packages.sh: OK"
