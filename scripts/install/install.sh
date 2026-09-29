#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${INSTALL_DIR}/../.." && pwd)"

PIBOOK_USER="${PIBOOK_USER:-pi}"
EXPECTED_DIR="/home/pi/PiBook"

ACTION="${1:-all}"

fail() {
    echo "ERRO: $*" >&2
    exit 1
}

phase() {
    echo
    echo "============================================================"
    echo "$1"
    echo "============================================================"
}

if [ "$EUID" -ne 0 ]; then
    fail "executar como root: sudo $0"
fi

if [ "$PROJECT_DIR" != "$EXPECTED_DIR" ]; then
    fail "este baseline suporta apenas $EXPECTED_DIR (atual: $PROJECT_DIR)"
fi

id "$PIBOOK_USER" >/dev/null 2>&1 \
    || fail "utilizador '$PIBOOK_USER' não existe"

for script in \
    install-packages.sh \
    install-waveshare.sh \
    install-system.sh \
    install-early-splash.sh
do
    [ -x "${INSTALL_DIR}/${script}" ] \
        || fail "instalador ausente ou não executável: ${script}"

    bash -n "${INSTALL_DIR}/${script}" \
        || fail "erro de sintaxe em ${script}"
done

run_packages() {
    phase "1/4 — Pacotes e ambiente Python"

    "${INSTALL_DIR}/install-packages.sh"
}

run_waveshare() {
    phase "2/4 — Driver Waveshare"

    "${INSTALL_DIR}/install-waveshare.sh"
}

run_system() {
    phase "3/4 — Integração de sistema"

    "${INSTALL_DIR}/install-system.sh"
}

run_early_splash() {
    phase "4/4 — Construir early splash"

    runuser \
        -u "$PIBOOK_USER" \
        -- \
        "${INSTALL_DIR}/install-early-splash.sh" \
        build
}

show_summary() {
    echo
    echo "============================================================"
    echo "PiBook — instalação concluída"
    echo "============================================================"

    echo
    echo "Concluído:"
    echo "  - pacotes de sistema"
    echo "  - ambiente Python"
    echo "  - driver Waveshare validado + patch PiBook"
    echo "  - helpers e units systemd"
    echo "  - configuração base de sistema"
    echo "  - candidato early-splash construído"

    echo
    echo "Não efetuado automaticamente:"
    echo "  - nenhuma credencial Wi-Fi foi criada"
    echo "  - nenhum UUID NetworkManager foi inventado"
    echo "  - o early splash NÃO foi promovido para o boot ativo"
    echo "  - nenhum reboot foi executado"

    echo
    echo "Candidato early splash:"
    echo "  ${PROJECT_DIR}/build/early-splash/initramfs-pibook-candidate"

    echo
    echo "Próximos passos:"
    echo "  1. configurar /etc/pibook-network/config.json"
    echo "  2. configurar /etc/pibook-network/startup.json"
    echo "  3. validar a rede"
    echo "  4. testar/promover o early splash separadamente"
    echo "  5. reiniciar apenas quando decidido pelo utilizador"
}

case "$ACTION" in
    all)
        run_packages
        run_waveshare
        run_system
        run_early_splash
        show_summary
        ;;

    packages)
        run_packages
        ;;

    waveshare)
        run_waveshare
        ;;

    system)
        run_system
        ;;

    early-splash)
        run_early_splash
        ;;

    *)
        echo "Uso:"
        echo "  sudo $0"
        echo "  sudo $0 all"
        echo "  sudo $0 packages"
        echo "  sudo $0 waveshare"
        echo "  sudo $0 system"
        echo "  sudo $0 early-splash"
        exit 2
        ;;
esac
