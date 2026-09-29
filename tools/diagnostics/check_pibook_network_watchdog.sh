#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SERVICE="pibook-network-watchdog.service"
TIMER="pibook-network-watchdog.timer"
HELPER="/usr/local/sbin/pibook-network-watchdog"
RESULT="/var/lib/pibook-network/watchdog_result.json"
PIBOOK_SERVICE="pibook-zero.service"
VENV_PYTHON="/home/pi/PiBook/.venv/bin/python"

failures=0

pass() {
    printf '[OK] %s\n' "$*"
}

fail() {
    printf '[ERRO] %s\n' "$*" >&2
    failures=$((failures + 1))
}

printf '============================================================\n'
printf ' PiBook — verificação do watchdog de rede\n'
printf '============================================================\n'

if systemctl is-active --quiet "$TIMER"; then
    pass "$TIMER ativo"
else
    fail "$TIMER não está ativo"
fi

if systemctl is-enabled --quiet "$TIMER"; then
    pass "$TIMER ativado no arranque"
else
    fail "$TIMER não está ativado no arranque"
fi

service_enabled="$(
    systemctl is-enabled "$SERVICE" 2>/dev/null || true
)"

if [[ "$service_enabled" == "static" ||
      "$service_enabled" == "disabled" ]]
then
    pass "$SERVICE não está ativado diretamente"
else
    fail "$SERVICE tem estado inesperado: $service_enabled"
fi

if sudo "$HELPER" validate; then
    pass "Validação interna do watchdog"
else
    fail "Validação interna do watchdog falhou"
fi

if sudo "$HELPER" check; then
    pass "Verificação manual executada"
else
    fail "Verificação manual terminou com erro"
fi

RESULT_TMP="$(mktemp)"
STATUS_TMP="$(mktemp)"
trap 'rm -f "$RESULT_TMP" "$STATUS_TMP"' EXIT

if sudo cat "$RESULT" >"$RESULT_TMP"; then
    cat "$RESULT_TMP"
else
    fail "Resultado do watchdog em falta"
fi

if sudo pibook-networkctl status --json >"$STATUS_TMP"; then
    cat "$STATUS_TMP"
else
    fail "Estado da rede indisponível"
fi

if "$VENV_PYTHON" - "$RESULT_TMP" "$STATUS_TMP" <<'PY'
import json
import sys
from pathlib import Path

result = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
status = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

state = status.get("state", {})
actual = status.get("actual", {})

checks = {
    "watchdog_status": result.get("status") == "healthy",
    "watchdog_action": result.get("action") == "none",
    "operation": state.get("operation") == "idle",
    "mode_match": state.get("desired_mode") == actual.get("mode"),
}

failed = [name for name, value in checks.items() if not value]

if failed:
    raise SystemExit("Validações falhadas: " + ", ".join(failed))
PY
then
    pass "Watchdog confirmou rede saudável sem recuperar"
else
    fail "Resultado do watchdog não corresponde ao esperado"
fi

PID="$(
    systemctl show -p MainPID --value "$PIBOOK_SERVICE" 2>/dev/null || true
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

FAILED_UNITS="$(
    systemctl --failed --no-legend --plain 2>/dev/null |
        awk 'NF {print $1}'
)"

if [[ -z "$FAILED_UNITS" ]]; then
    pass "Nenhuma unidade systemd falhada"
else
    fail "Unidades falhadas: $FAILED_UNITS"
fi

echo
systemctl list-timers "$TIMER" --no-pager
echo
journalctl -u "$SERVICE" --no-pager -n 80

echo
if [[ "$failures" -eq 0 ]]; then
    printf '[OK] WATCHDOG DE REDE VALIDADO EM ESTADO SAUDÁVEL\n'
    exit 0
fi

printf '[ERRO] Verificação terminou com %s problema(s)\n' "$failures"
exit 1
