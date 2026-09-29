#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

PROJECT="/home/pi/PiBook"
SERVICE="pibook-zero.service"
STARTUP_SERVICE="pibook-network-startup.service"
RESULT="/var/lib/pibook-network/startup_result.json"
VENV_PYTHON="$PROJECT/.venv/bin/python"

failures=0

pass() {
    printf '[OK] %s\n' "$*"
}

fail() {
    printf '[ERRO] %s\n' "$*" >&2
    failures=$((failures + 1))
}

check_active() {
    if systemctl is-active --quiet "$1"; then
        pass "$1 ativo"
    else
        fail "$1 não está ativo"
    fi
}

check_enabled() {
    if systemctl is-enabled --quiet "$1"; then
        pass "$1 ativado no arranque"
    else
        fail "$1 não está ativado no arranque"
    fi
}

printf '============================================================\n'
printf ' PiBook — verificação do primeiro arranque automático\n'
printf '============================================================\n'

check_active NetworkManager.service
check_active "$STARTUP_SERVICE"
check_active "$SERVICE"
check_active pibook-network-hostapd.service
check_active pibook-network-dnsmasq.service
check_active pibook-network-captive.service

check_enabled "$STARTUP_SERVICE"
check_enabled "$SERVICE"

for unit in \
    pibook-network-hostapd.service \
    pibook-network-dnsmasq.service \
    pibook-network-captive.service \
    pibook-wifi-scan.service \
    pibook-wifi-connect.service
do
    state="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
    if [[ "$state" == "disabled" ]]; then
        pass "$unit permanece disabled"
    else
        fail "$unit devia permanecer disabled; estado=$state"
    fi
done

STATUS_JSON="$(mktemp)"
RESULT_JSON="$(mktemp)"
trap 'rm -f "$STATUS_JSON" "$RESULT_JSON"' EXIT

if sudo test -r "$RESULT"; then
    sudo cat "$RESULT" >"$RESULT_JSON"
    cat "$RESULT_JSON"
else
    fail "Resultado do coordenador em falta"
    printf '{}\n' >"$RESULT_JSON"
fi

if sudo pibook-networkctl status --json >"$STATUS_JSON"; then
    cat "$STATUS_JSON"
else
    fail "Não foi possível ler o estado de rede"
fi

if "$VENV_PYTHON" - "$STATUS_JSON" "$RESULT_JSON" <<'PY'
import json
import sys
from pathlib import Path

status = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
result = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

actual = status.get("actual", {})
state = status.get("state", {})
addresses = actual.get("addresses", [])

if isinstance(addresses, str):
    addresses = [addresses]

current_boot_id = (
    Path("/proc/sys/kernel/random/boot_id")
    .read_text(encoding="utf-8")
    .strip()
)

checks = {
    "startup_boot_id": result.get("boot_id") == current_boot_id,
    "startup_status": result.get("status") == "success",
    "startup_mode": result.get("mode") == "hotspot",
    "actual_mode": actual.get("mode") == "hotspot",
    "operation": state.get("operation") == "idle",
    "address": "10.42.0.1/24" in addresses,
    "hostapd": actual.get("hostapd_active") is True,
    "dnsmasq": actual.get("dnsmasq_active") is True,
}

failed = [name for name, value in checks.items() if not value]

if failed:
    raise SystemExit("Validações falhadas: " + ", ".join(failed))
PY
then
    pass "Estado lógico do hotspot validado"
else
    fail "O estado lógico do hotspot não corresponde ao esperado"
fi

PID="$(
    systemctl show -p MainPID --value "$SERVICE" 2>/dev/null || true
)"

SPI_COUNT="$(
    sudo ls -l "/proc/$PID/fd" 2>/dev/null |
        grep -cE ' -> /dev/spidev0\.0$' || true
)"

if [[ "$SPI_COUNT" -eq 1 ]]; then
    pass "Descritor SPI único"
else
    fail "Descritores SPI inesperados: $SPI_COUNT"
fi

if ss -H -lnt 2>/dev/null |
    awk '{print $4}' |
    grep -Eq '(^|:|\])5000$'
then
    pass "Servidor PiBook na porta 5000"
else
    fail "Porta 5000 indisponível"
fi

if ss -H -lnt 2>/dev/null |
    awk '{print $4}' |
    grep -Eq '(^|:|\])80$'
then
    pass "Portal cativo na porta 80"
else
    fail "Porta 80 do portal cativo indisponível"
fi

AVAHI_NAME="$(
    ps -C avahi-daemon -o args= 2>/dev/null |
        sed -n 's/.*running \[\([^]]*\.local\)\].*/\1/p' |
        head -n 1
)"

if [[ "$AVAHI_NAME" == "pibook.local" ]]; then
    pass "Avahi anuncia pibook.local"
else
    fail "Nome mDNS inesperado: ${AVAHI_NAME:-indefinido}"
fi

FAILED_UNITS="$(
    systemctl --failed --no-legend --plain 2>/dev/null |
        awk 'NF {print $1}'
)"

if [[ -z "$FAILED_UNITS" ]]; then
    pass "Nenhuma unidade systemd falhada"
else
    fail "Unidades falhadas: $FAILED_UNITS"
fi

while IFS=: read -r uuid type autoconnect; do
    [[ "$type" == "802-11-wireless" ]] || continue

    if [[ "$autoconnect" == "no" ]]; then
        pass "Perfil $uuid sem autoconnect"
    else
        fail "Perfil $uuid ainda com autoconnect=$autoconnect"
    fi
done < <(
    nmcli -t -f UUID,TYPE,AUTOCONNECT connection show
)

STARTUP_TIME="$(
    systemctl show \
        -p ActiveEnterTimestampMonotonic \
        --value "$STARTUP_SERVICE"
)"
PIBOOK_TIME="$(
    systemctl show \
        -p ActiveEnterTimestampMonotonic \
        --value "$SERVICE"
)"

if [[ "$STARTUP_TIME" =~ ^[0-9]+$ &&
      "$PIBOOK_TIME" =~ ^[0-9]+$ &&
      "$PIBOOK_TIME" -ge "$STARTUP_TIME" ]]
then
    pass "PiBook iniciou depois do coordenador de rede"
else
    fail "A ordem temporal dos serviços não foi confirmada"
fi

echo
journalctl -b -u "$STARTUP_SERVICE" --no-pager -n 120
echo
journalctl -b -u "$SERVICE" --no-pager -n 80

echo
if [[ "$failures" -eq 0 ]]; then
    printf '[OK] PRIMEIRO ARRANQUE AUTOMÁTICO VALIDADO\n'
    exit 0
fi

printf '[ERRO] Verificação terminou com %s problema(s)\n' "$failures"
exit 1
