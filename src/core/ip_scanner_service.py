"""Shared client for the PiBook IP Scanner v2."""

from __future__ import annotations

import ipaddress
import json
import subprocess
from pathlib import Path
from typing import Any

SUDO = "/usr/bin/sudo"
IP_SCANCTL = "/usr/local/sbin/pibook-ip-scanctl"
IP = "/usr/sbin/ip"
RESULTS_PATH = Path("/var/lib/pibook-ip-scanner/results.json")


class IPScannerError(RuntimeError):
    """Raised when the IP Scanner helper fails."""


class IPScannerService:
    """Small, UI-independent client for the privileged scanner helper."""

    def _command(
        self,
        command: str,
        *,
        timeout: int = 20,
    ) -> dict[str, Any]:
        try:
            result = subprocess.run(
                [SUDO, "-n", IP_SCANCTL, command],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise IPScannerError(
                "A operação do IP Scanner excedeu o tempo permitido."
            ) from exc

        if result.returncode != 0:
            details = (
                result.stderr.strip()
                or result.stdout.strip()
                or f"código {result.returncode}"
            )
            raise IPScannerError(details)

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise IPScannerError(
                "O IP Scanner devolveu uma resposta inválida."
            ) from exc

        if not isinstance(payload, dict):
            raise IPScannerError(
                "O IP Scanner não devolveu um objeto JSON."
            )

        return payload

    def _read_results(self) -> dict[str, Any]:
        try:
            with RESULTS_PATH.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return {
                "status": "never",
                "progress": 0,
                "started_at": "",
                "generated_at": "",
                "duration_seconds": 0,
                "interface": "",
                "local_ip": "",
                "network": "",
                "count": 0,
                "devices": [],
                "error": "",
            }
        except (OSError, json.JSONDecodeError) as exc:
            raise IPScannerError(
                "Não foi possível ler o estado do IP Scanner."
            ) from exc

        if not isinstance(payload, dict):
            raise IPScannerError(
                "O estado do IP Scanner é inválido."
            )

        return payload

    def _current_network(
        self,
        interface: str,
    ) -> tuple[str, str]:
        """Return the interface's current IPv4 address and subnet."""
        try:
            result = subprocess.run(
                [IP, "-4", "-o", "address", "show", "dev", interface],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "", ""

        if result.returncode != 0:
            return "", ""

        for line in result.stdout.splitlines():
            parts = line.split()
            if "inet" not in parts:
                continue

            index = parts.index("inet")
            if index + 1 >= len(parts):
                continue

            try:
                address = ipaddress.ip_interface(parts[index + 1])
            except ValueError:
                continue

            return str(address.ip), str(address.network)

        return "", ""

    def _validated_results(self) -> dict[str, Any]:
        """Never expose devices discovered on a different network."""
        stored = self._read_results()
        interface = str(stored.get("interface") or "wlan0")
        local_ip, network = self._current_network(interface)

        # If there is no current IPv4 network, old scan data must not
        # be presented as if it were still valid.
        if not local_ip or not network:
            return {
                "status": "unavailable",
                "progress": 0,
                "started_at": "",
                "generated_at": "",
                "duration_seconds": 0,
                "interface": interface,
                "local_ip": "",
                "network": "",
                "count": 0,
                "devices": [],
                "error": "",
            }

        stored_network = str(stored.get("network") or "")
        status = str(stored.get("status") or "").lower()

        if (
            stored_network
            and stored_network != network
            and status not in {"queued", "running"}
        ):
            return {
                "status": "stale",
                "progress": 0,
                "started_at": "",
                "generated_at": "",
                "duration_seconds": 0,
                "interface": interface,
                "local_ip": local_ip,
                "network": network,
                "count": 0,
                "devices": [],
                "error": "",
                "previous_network": stored_network,
            }

        payload = dict(stored)
        payload["interface"] = interface
        payload["local_ip"] = local_ip
        payload["network"] = network
        return payload

    def status(self) -> dict[str, Any]:
        payload = self._validated_results()
        payload["scanning"] = (
            str(payload.get("status", "")).lower()
            in {"queued", "running"}
        )
        return payload

    def results(self) -> dict[str, Any]:
        return self._validated_results()

    def start(self) -> dict[str, Any]:
        return self._command("start", timeout=15)
