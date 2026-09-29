#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PIBOOK_USER="${PIBOOK_USER:-pi}"
EXPECTED_DIR="/home/pi/PiBook"

fail() {
    echo "ERRO: $*" >&2
    exit 1
}

if [ "$EUID" -ne 0 ]; then
    fail "executar como root: sudo $0"
fi

if [ "$PROJECT_DIR" != "$EXPECTED_DIR" ]; then
    fail "este baseline suporta apenas $EXPECTED_DIR (atual: $PROJECT_DIR)"
fi

id "$PIBOOK_USER" >/dev/null 2>&1 \
    || fail "utilizador '$PIBOOK_USER' não existe"

echo "=== PiBook: preflight ==="

if ! runuser -u "$PIBOOK_USER" -- sudo -n true 2>/dev/null; then
    fail "o utilizador '$PIBOOK_USER' precisa de sudo não-interativo"
fi

echo "sudo -n para $PIBOOK_USER: OK"

for group in gpio spi i2c netdev input; do
    if getent group "$group" >/dev/null 2>&1; then
        usermod -a -G "$group" "$PIBOOK_USER"
    fi
done


echo
echo "=== PiBook: helpers /usr/local/sbin ==="

install -d -o root -g root -m 0755 /usr/local/sbin

for src in "$PROJECT_DIR"/scripts/system/pibook-*; do
    [ -f "$src" ] || continue

    name="$(basename "$src")"

    install \
        -o root \
        -g root \
        -m 0750 \
        "$src" \
        "/usr/local/sbin/$name"

    echo "INSTALLED /usr/local/sbin/$name"
done


echo
echo "=== PiBook: systemd ==="

while IFS= read -r -d '' src; do
    rel="${src#"$PROJECT_DIR/scripts/systemd/"}"
    dst="/etc/systemd/system/$rel"

    install -d \
        -o root \
        -g root \
        -m 0755 \
        "$(dirname "$dst")"

    install \
        -o root \
        -g root \
        -m 0644 \
        "$src" \
        "$dst"

    echo "INSTALLED $dst"
done < <(
    find "$PROJECT_DIR/scripts/systemd" \
        -type f \
        -print0
)


echo
echo "=== PiBook: modprobe/modules/tmpfiles ==="

for src in "$PROJECT_DIR"/system/modprobe.d/*; do
    [ -f "$src" ] || continue

    install \
        -D \
        -o root \
        -g root \
        -m 0644 \
        "$src" \
        "/etc/modprobe.d/$(basename "$src")"
done

for src in "$PROJECT_DIR"/system/modules-load.d/*; do
    [ -f "$src" ] || continue

    install \
        -D \
        -o root \
        -g root \
        -m 0644 \
        "$src" \
        "/etc/modules-load.d/$(basename "$src")"
done

for src in "$PROJECT_DIR"/system/tmpfiles.d/*; do
    [ -f "$src" ] || continue

    install \
        -D \
        -o root \
        -g root \
        -m 0644 \
        "$src" \
        "/etc/tmpfiles.d/$(basename "$src")"
done

systemd-tmpfiles --create \
    /etc/tmpfiles.d/pibook-network-web.conf


echo
echo "=== PiBook: network baseline ==="

install -d \
    -o root \
    -g root \
    -m 0755 \
    /etc/pibook-network

install \
    -o root \
    -g root \
    -m 0600 \
    "$PROJECT_DIR/system/network/watchdog.json" \
    /etc/pibook-network/watchdog.json

install \
    -o root \
    -g root \
    -m 0644 \
    "$PROJECT_DIR/system/network/dnsmasq.conf" \
    /etc/pibook-network/dnsmasq.conf

install \
    -o root \
    -g root \
    -m 0600 \
    "$PROJECT_DIR/system/network/config.example.json" \
    /etc/pibook-network/config.example.json

install \
    -o root \
    -g root \
    -m 0600 \
    "$PROJECT_DIR/system/network/startup.example.json" \
    /etc/pibook-network/startup.example.json

echo "Templates de rede instalados."
echo "Credenciais e UUIDs reais não foram criados."


echo
echo "=== PiBook: systemd daemon-reload ==="

systemctl daemon-reload


echo
echo "=== PiBook: estados das units ==="

# Baseline normal: aplicação principal.
systemctl enable pibook-zero.service
systemctl enable pibook-boot-history.service
systemctl enable pibook-boot-guard.timer

# Estes serviços existem mas são deliberadamente controlados
# manualmente ou por outras units.
for unit in \
    pibook-battery-logger.service \
    pibook-bluetooth-default-off.service \
    pibook-network-captive.service \
    pibook-network-dnsmasq.service \
    pibook-network-hostapd.service \
    pibook-network-startup.service \
    pibook-wifi-connect.service \
    pibook-wifi-scan.service
do
    systemctl disable "$unit" >/dev/null 2>&1 || true
done

# O watchdog só deve ser ativado depois de existirem config.json
# e startup.json reais.
if \
    [ -f /etc/pibook-network/config.json ] &&
    [ -f /etc/pibook-network/startup.json ]
then
    echo "Configuração de rede real encontrada."

    if /usr/local/sbin/pibook-network-startup validate \
        >/dev/null 2>&1
    then
        systemctl enable pibook-network-watchdog.timer
        echo "pibook-network-watchdog.timer: enabled"
    else
        systemctl disable pibook-network-watchdog.timer \
            >/dev/null 2>&1 || true

        echo "pibook-network-watchdog.timer: disabled"
        echo "Motivo: configuração de startup ainda não é válida."
    fi
else
    systemctl disable pibook-network-watchdog.timer \
        >/dev/null 2>&1 || true

    echo "pibook-network-watchdog.timer: disabled"
    echo "Configure primeiro o perfil Wi-Fi fallback."
fi


echo
echo "=== PiBook: validação dos ficheiros instalados ==="

for helper in \
    pibook-networkctl \
    pibook-network-watchdog \
    pibook-power-profile \
    pibook-boot-guard
do
    test -x "/usr/local/sbin/$helper" \
        || fail "helper ausente: $helper"
done

systemctl cat pibook-zero.service >/dev/null
systemctl cat pibook-boot-guard.timer >/dev/null

echo
echo "=== Estado configurado ==="

systemctl is-enabled pibook-zero.service || true
systemctl is-enabled pibook-boot-history.service || true
systemctl is-enabled pibook-boot-guard.timer || true
systemctl is-enabled pibook-network-watchdog.timer || true

echo
echo "install-system.sh: OK"
echo
echo "NOTA:"
echo "  Este script instala a integração do sistema,"
echo "  mas não inicia/reinicia serviços automaticamente."
