#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

REPO_URL="https://github.com/waveshareteam/e-Paper.git"

COMMIT_FILE="${PROJECT_DIR}/patches/waveshare/VENDOR_COMMIT"
PATCH_FILE="${PROJECT_DIR}/patches/waveshare/epdconfig-spi-descriptor-reuse.patch"

TARGET="${PROJECT_DIR}/lib/waveshare_epd"

EXPECTED_EPD7IN5_SHA="fafc41091c47d2ea367c3e85d84dca437e42370451cae5f5424b72b4614b55f2"
EXPECTED_EPDConfig_SHA="693dce075d4a9b3e7937ab7ffa9a2e8099d9112710546fb5fbfc0bddfdd71f4a"

if [ ! -f "$COMMIT_FILE" ]; then
    echo "ERRO: falta $COMMIT_FILE"
    exit 1
fi

if [ ! -f "$PATCH_FILE" ]; then
    echo "ERRO: falta $PATCH_FILE"
    exit 1
fi

VENDOR_COMMIT="$(tr -d '[:space:]' < "$COMMIT_FILE")"

if [ -z "$VENDOR_COMMIT" ]; then
    echo "ERRO: VENDOR_COMMIT vazio."
    exit 1
fi

for cmd in git patch sha256sum mktemp; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "ERRO: comando obrigatório não encontrado: $cmd"
        exit 1
    fi
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

VENDOR="${TMP}/e-Paper"
PACKAGE="${TMP}/waveshare_epd"

echo "=== PiBook: Waveshare e-Paper ==="
echo "Commit: $VENDOR_COMMIT"

git init -q "$VENDOR"
git -C "$VENDOR" remote add origin "$REPO_URL"

git -C "$VENDOR" fetch \
    --quiet \
    --depth 1 \
    origin "$VENDOR_COMMIT"

git -C "$VENDOR" checkout \
    --quiet \
    --detach \
    FETCH_HEAD

ACTUAL_COMMIT="$(git -C "$VENDOR" rev-parse HEAD)"

if [ "$ACTUAL_COMMIT" != "$VENDOR_COMMIT" ]; then
    echo "ERRO: commit Waveshare inesperado:"
    echo "  esperado: $VENDOR_COMMIT"
    echo "  atual:    $ACTUAL_COMMIT"
    exit 1
fi

SOURCE="${VENDOR}/RaspberryPi_JetsonNano/python/lib/waveshare_epd"

if [ ! -d "$SOURCE" ]; then
    echo "ERRO: waveshare_epd não encontrado no commit vendor."
    exit 1
fi

mkdir -p "$PACKAGE"
cp -a "$SOURCE/." "$PACKAGE/"

rm -rf "$PACKAGE/__pycache__"
find "$PACKAGE" -type f -name '*.pyc' -delete

echo
echo "=== Validar driver epd7in5_V2 original ==="

ACTUAL_DRIVER_SHA="$(
    sha256sum "$PACKAGE/epd7in5_V2.py" | awk '{print $1}'
)"

if [ "$ACTUAL_DRIVER_SHA" != "$EXPECTED_EPD7IN5_SHA" ]; then
    echo "ERRO: hash inesperado para epd7in5_V2.py"
    echo "  esperado: $EXPECTED_EPD7IN5_SHA"
    echo "  atual:    $ACTUAL_DRIVER_SHA"
    exit 1
fi

echo "epd7in5_V2.py: OK"

echo
echo "=== Aplicar patch PiBook ao epdconfig.py ==="

patch \
    --dry-run \
    --batch \
    --forward \
    "$PACKAGE/epdconfig.py" \
    < "$PATCH_FILE"

patch \
    --batch \
    --forward \
    --no-backup-if-mismatch \
    "$PACKAGE/epdconfig.py" \
    < "$PATCH_FILE"

ACTUAL_CONFIG_SHA="$(
    sha256sum "$PACKAGE/epdconfig.py" | awk '{print $1}'
)"

if [ "$ACTUAL_CONFIG_SHA" != "$EXPECTED_EPDConfig_SHA" ]; then
    echo "ERRO: patch Waveshare não produziu o epdconfig esperado."
    echo "  esperado: $EXPECTED_EPDConfig_SHA"
    echo "  atual:    $ACTUAL_CONFIG_SHA"
    exit 1
fi

echo "epdconfig.py patched: OK"

echo
echo "=== Instalar runtime Waveshare ==="

mkdir -p "${PROJECT_DIR}/lib"
rm -rf "$TARGET"
mv "$PACKAGE" "$TARGET"

if [ "$EUID" -eq 0 ] && id pi >/dev/null 2>&1; then
    chown -R pi:pi "$TARGET"
fi

echo
echo "Waveshare instalado em:"
echo "  $TARGET"

echo
echo "Commit vendor:"
echo "  $VENDOR_COMMIT"

echo
echo "epdconfig.py:"
echo "  $ACTUAL_CONFIG_SHA"

echo
echo "install-waveshare.sh: OK"
