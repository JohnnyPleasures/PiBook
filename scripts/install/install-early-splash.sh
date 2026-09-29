#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

SOURCE_DIR="${PROJECT_DIR}/system/initramfs/early-splash"
BUILD_ROOT="${PROJECT_DIR}/build/early-splash"

SOURCE_C="${SOURCE_DIR}/pibook-early-splash.c"
HOOK="${SOURCE_DIR}/pibook-early-splash-run"
PARAM="${SOURCE_DIR}/param.conf"

KERNEL="${PIBOOK_KERNEL:-$(uname -r)}"
BASE="${PIBOOK_INITRAMFS_BASE:-/boot/initrd.img-${KERNEL}}"

CANDIDATE="${BUILD_ROOT}/initramfs-pibook-candidate"
BOOT_CANDIDATE="/boot/firmware/initramfs-pibook-candidate"

EXPECTED_SOURCE_SHA="b7b1fba0ce7d532f4eb46ac20af564bf709c40567b90979de7799a731cf21465"
EXPECTED_HOOK_SHA="bedfb6cdcc17aea03fd00bffc36415c07ff56053fc30250e1420c65d5ad04417"
EXPECTED_PARAM_SHA="272954167206d57a690233e9d45236320017255913a0bbd77fc50e84662c41d5"
EXPECTED_BINARY_SHA="d88324b10745e5a77393b6af52b1402a040427bf5f8d08466d8a4690691c834c"

BUILD_ID="fcaa567d31d7a0956fe0f81044d16d9685434ab2"

ACTION="${1:-build}"

fail() {
    echo "ERRO: $*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 \
        || fail "comando obrigatório não encontrado: $1"
}

sha_of() {
    sha256sum "$1" | awk '{print $1}'
}

validate_sources() {
    echo "=== Validar fontes early splash ==="

    [ -f "$SOURCE_C" ] || fail "falta $SOURCE_C"
    [ -f "$HOOK" ] || fail "falta $HOOK"
    [ -f "$PARAM" ] || fail "falta $PARAM"

    [ "$(sha_of "$SOURCE_C")" = "$EXPECTED_SOURCE_SHA" ] \
        || fail "SHA inesperado em pibook-early-splash.c"

    [ "$(sha_of "$HOOK")" = "$EXPECTED_HOOK_SHA" ] \
        || fail "SHA inesperado em pibook-early-splash-run"

    [ "$(sha_of "$PARAM")" = "$EXPECTED_PARAM_SHA" ] \
        || fail "SHA inesperado em param.conf"

    echo "Fontes validadas: OK"
}

build_candidate() {
    for cmd in \
        gcc \
        strip \
        cpio \
        python3 \
        sha256sum \
        lsinitramfs \
        unmkinitramfs
    do
        require_command "$cmd"
    done

    validate_sources

    [ -f "$BASE" ] \
        || fail "initramfs base não encontrado: $BASE"

    echo
    echo "=== Base initramfs ==="
    echo "Kernel: $KERNEL"
    echo "Base:   $BASE"
    sha256sum "$BASE"

    rm -rf "$BUILD_ROOT"
    mkdir -p "$BUILD_ROOT"

    WORK="${BUILD_ROOT}/work"
    OVERLAY_ROOT="${WORK}/overlay"

    mkdir -p \
        "${OVERLAY_ROOT}/usr/bin" \
        "${OVERLAY_ROOT}/conf" \
        "${OVERLAY_ROOT}/scripts/init-top"

    echo
    echo "=== Compilar helper ==="

    gcc \
        -Os \
        -std=c11 \
        -Wall \
        -Wextra \
        -Wformat=2 \
        -fno-strict-aliasing \
        "-Wl,--build-id=0x${BUILD_ID}" \
        -o "${OVERLAY_ROOT}/usr/bin/pibook-early-splash" \
        "$SOURCE_C"

    strip \
        --strip-unneeded \
        "${OVERLAY_ROOT}/usr/bin/pibook-early-splash"

    BINARY_SHA="$(
        sha_of "${OVERLAY_ROOT}/usr/bin/pibook-early-splash"
    )"

    echo "Helper SHA: $BINARY_SHA"

    if [ "$BINARY_SHA" != "$EXPECTED_BINARY_SHA" ]; then
        fail "helper compilado não corresponde ao binário validado"
    fi

    echo "Helper byte-reproducível: OK"

    install \
        -m 0755 \
        "$HOOK" \
        "${OVERLAY_ROOT}/scripts/init-top/pibook-early-splash-run"

    install \
        -m 0644 \
        "$PARAM" \
        "${OVERLAY_ROOT}/conf/param.conf"

    echo
    echo "=== Criar overlay CPIO determinístico ==="

    # O histórico validado usou um arquivo newc colocado imediatamente
    # antes da primeira stream ZSTD do initramfs normal.
    #
    # Não tentamos reproduzir os metadados históricos do arquivo
    # (inode/mtime de 2026-08-19). Em vez disso geramos um arquivo
    # determinístico com conteúdo funcional idêntico.

    find "$OVERLAY_ROOT" \
        -exec touch -h -d '@0' {} +

    (
        cd "$OVERLAY_ROOT"

        printf '%s\n' \
            . \
            usr \
            usr/bin \
            usr/bin/pibook-early-splash \
            conf \
            conf/param.conf \
            scripts \
            scripts/init-top \
            scripts/init-top/pibook-early-splash-run \
        | cpio \
            --quiet \
            --create \
            --format=newc \
            --owner=0:0 \
            --reproducible \
            > "${WORK}/overlay.cpio"
    )

    echo "Overlay:"
    stat -c '  tamanho=%s bytes' "${WORK}/overlay.cpio"
    sha256sum "${WORK}/overlay.cpio"

    echo
    echo "=== Inserir overlay antes da primeira stream ZSTD ==="

    python3 - \
        "$BASE" \
        "${WORK}/overlay.cpio" \
        "$CANDIDATE" <<'PY'
from pathlib import Path
import sys

base_path = Path(sys.argv[1])
overlay_path = Path(sys.argv[2])
output_path = Path(sys.argv[3])

base = base_path.read_bytes()
overlay = overlay_path.read_bytes()

zstd_magic = bytes.fromhex("28 b5 2f fd")

offset = base.find(zstd_magic)

if offset < 0:
    raise SystemExit(
        "ERRO: primeira stream ZSTD não encontrada no initramfs base"
    )

prefix = base[:offset]
stream = base[offset:]

output_path.write_bytes(
    prefix + overlay + stream
)

print(f"offset_zstd_base={offset}")
print(f"overlay_size={len(overlay)}")
print(f"candidate_size={output_path.stat().st_size}")
PY

    chmod 0644 "$CANDIDATE"

    echo
    echo "=== Validar arquitetura do candidato ==="

    python3 - \
        "$BASE" \
        "$CANDIDATE" \
        "${WORK}/overlay.cpio" <<'PY'
from pathlib import Path
import sys

base = Path(sys.argv[1]).read_bytes()
candidate = Path(sys.argv[2]).read_bytes()
overlay = Path(sys.argv[3]).read_bytes()

magic = bytes.fromhex("28 b5 2f fd")

base_zstd = base.find(magic)
candidate_zstd = candidate.find(magic)

if base_zstd < 0 or candidate_zstd < 0:
    raise SystemExit("ERRO: stream ZSTD não encontrada")

if candidate[:base_zstd] != base[:base_zstd]:
    raise SystemExit("ERRO: prefixo do initramfs foi alterado")

if candidate[
    base_zstd:candidate_zstd
] != overlay:
    raise SystemExit("ERRO: overlay inserido não corresponde ao gerado")

if candidate[candidate_zstd:] != base[base_zstd:]:
    raise SystemExit("ERRO: stream ZSTD original foi alterada")

print("prefixo_original=OK")
print("overlay=OK")
print("stream_zstd_original=OK")
PY

    echo
    echo "=== Validar conteúdo via lsinitramfs ==="

    LIST="${WORK}/candidate.list"
    lsinitramfs "$CANDIDATE" > "$LIST"

    for required in \
        usr/bin/pibook-early-splash \
        conf/param.conf \
        scripts/init-top/pibook-early-splash-run
    do
        grep -Fxq "$required" "$LIST" \
            || fail "ficheiro ausente no candidato: $required"

        echo "OK $required"
    done

    echo
    echo "=== Validar conteúdo extraído ==="

    EXTRACTED="${WORK}/extracted"
    mkdir -p "$EXTRACTED"

    unmkinitramfs \
        "$CANDIDATE" \
        "$EXTRACTED"

    EXTRACTED_BIN="$(
        find "$EXTRACTED" \
            -type f \
            -path '*/usr/bin/pibook-early-splash' \
            | head -1
    )"

    [ -n "$EXTRACTED_BIN" ] \
        || fail "helper não encontrado após extração"

    [ "$(sha_of "$EXTRACTED_BIN")" = "$EXPECTED_BINARY_SHA" ] \
        || fail "helper extraído tem SHA inesperado"

    echo "Helper extraído: OK"

    echo
    echo "=== Resultado ==="
    echo "Candidato:"
    echo "  $CANDIDATE"
    echo
    sha256sum "$CANDIDATE"

    echo
    echo "IMPORTANTE:"
    echo "  /boot NÃO foi alterado."
    echo "  O initramfs ativo NÃO foi substituído."
}

install_candidate() {
    if [ "$EUID" -ne 0 ]; then
        fail "install-candidate requer root"
    fi

    if [ ! -f "$CANDIDATE" ]; then
        echo "Candidato ainda não existe; a construir primeiro."
        build_candidate
    fi

    echo
    echo "=== Instalar candidato de teste ==="

    install \
        -o root \
        -g root \
        -m 0644 \
        "$CANDIDATE" \
        "$BOOT_CANDIDATE"

    sync

    echo
    echo "Candidato copiado para:"
    echo "  $BOOT_CANDIDATE"
    echo
    echo "O initramfs ativo continua inalterado."
    echo "Não foi configurado tryboot automaticamente."
}

case "$ACTION" in
    build)
        build_candidate
        ;;

    install-candidate)
        install_candidate
        ;;

    *)
        echo "Uso:"
        echo "  $0 build"
        echo "  sudo $0 install-candidate"
        exit 2
        ;;
esac
