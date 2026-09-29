"""Lightweight live network status for PiBook.

Read-only status collection for the Web UI. Persistent PiBook state/results
are read directly from sanitized JSON files while a small set of cheap,
unprivileged system queries confirms the real wlan0 state.

No NetworkManager query is issued from hotspot mode and no root helper is
started merely to render network status.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


STATE_PATH = Path("/var/lib/pibook-network-web/state.json")
SCAN_RESULTS_PATH = Path("/var/lib/pibook-network-web/scan_results.json")
CONNECT_RESULT_PATH = Path("/var/lib/pibook-network-web/connect_result.json")
PROFILES_CACHE_PATH = Path("/var/lib/pibook-network-web/profiles.json")

HOSTAPD_SERVICE = "pibook-network-hostapd.service"
DNSMASQ_SERVICE = "pibook-network-dnsmasq.service"
NETWORKMANAGER_SERVICE = "NetworkManager.service"

SYSTEMCTL = shutil.which("systemctl") or "/usr/bin/systemctl"
IW = shutil.which("iw") or "/usr/sbin/iw"
IP = shutil.which("ip") or "/usr/sbin/ip"


class NetworkStatusError(RuntimeError):
    """Raised when the lightweight network status cannot be collected."""


class NetworkStatusService:
    """Build the Web network snapshot without privileged Python helpers."""

    def __init__(self, interface: str = "wlan0"):
        self.interface = interface

    @staticmethod
    def _read_json(
        path: Path,
        default: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return copy.deepcopy(default)
        except (OSError, json.JSONDecodeError) as exc:
            raise NetworkStatusError(
                f"Não foi possível ler {path.name}."
            ) from exc

        if not isinstance(payload, dict):
            raise NetworkStatusError(
                f"O conteúdo de {path.name} é inválido."
            )

        return payload

    @staticmethod
    def _run(
        command: list[str],
        timeout: float = 3.0,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise NetworkStatusError(
                f"A consulta {command[0]} excedeu o tempo permitido."
            ) from exc

    def _service_active(self, service: str) -> bool:
        result = self._run(
            [SYSTEMCTL, "is-active", service],
            timeout=2.0,
        )
        return result.returncode == 0

    def _interface_info(self) -> dict[str, str]:
        result = self._run(
            [IW, "dev", self.interface, "info"],
            timeout=2.0,
        )

        if result.returncode != 0:
            return {
                "type": "",
                "ssid": "",
            }

        interface_type = ""
        ssid = ""

        for raw in result.stdout.splitlines():
            line = raw.strip()

            if line.startswith("type "):
                interface_type = line.split(None, 1)[1].strip()
            elif line.startswith("ssid "):
                ssid = line.split(None, 1)[1].strip()

        return {
            "type": interface_type,
            "ssid": ssid,
        }

    def _wifi_link_ssid(self) -> str:
        result = self._run(
            [IW, "dev", self.interface, "link"],
            timeout=2.0,
        )

        if result.returncode != 0:
            return ""

        if "Not connected." in result.stdout:
            return ""

        for raw in result.stdout.splitlines():
            line = raw.strip()
            if line.startswith("SSID:"):
                return line.split(":", 1)[1].strip()

        return ""

    def _addresses(self) -> list[str]:
        result = self._run(
            [
                IP,
                "-4",
                "-o",
                "address",
                "show",
                "dev",
                self.interface,
            ],
            timeout=2.0,
        )

        if result.returncode != 0:
            return []

        addresses: list[str] = []

        for line in result.stdout.splitlines():
            match = re.search(r"\binet\s+(\S+)", line)
            if match:
                addresses.append(match.group(1))

        return addresses

    def state(self) -> dict[str, Any]:
        return self._read_json(
            STATE_PATH,
            {
                "desired_mode": "unknown",
                "actual_mode": "unknown",
                "operation": "idle",
                "last_wifi_uuid": "",
                "last_wifi_name": "",
                "last_error": "",
            },
        )

    def scan_results(self) -> dict[str, Any]:
        return self._read_json(
            SCAN_RESULTS_PATH,
            {
                "status": "never",
                "count": 0,
                "networks": [],
                "error": "",
            },
        )

    def connect_result(self) -> dict[str, Any]:
        return self._read_json(
            CONNECT_RESULT_PATH,
            {
                "status": "never",
                "ssid": "",
                "profile_uuid": "",
                "addresses": [],
                "error": "",
            },
        )

    def profiles(
        self,
        active_uuid: str = "",
    ) -> list[dict[str, Any]]:
        payload = self._read_json(
            PROFILES_CACHE_PATH,
            {"profiles": []},
        )

        raw_profiles = payload.get("profiles", [])
        if not isinstance(raw_profiles, list):
            raw_profiles = []

        profiles: list[dict[str, Any]] = []

        for item in raw_profiles:
            if not isinstance(item, dict):
                continue

            profile = {
                "name": str(item.get("name", "")),
                "ssid": str(item.get("ssid", "")),
                "uuid": str(item.get("uuid", "")),
                "type": str(item.get("type", "wifi")),
                "autoconnect": bool(item.get("autoconnect", False)),
                "active": (
                    bool(active_uuid)
                    and str(item.get("uuid", "")) == active_uuid
                ),
            }

            if profile["uuid"]:
                profiles.append(profile)

        profiles.sort(
            key=lambda profile: (
                not profile["active"],
                (
                    profile.get("ssid")
                    or profile.get("name")
                    or ""
                ).lower(),
            )
        )

        return profiles

    def status(self) -> dict[str, Any]:
        state = self.state()
        scan_results = self.scan_results()
        connect_result = self.connect_result()

        operation = str(state.get("operation", "idle") or "idle")

        hostapd_active = self._service_active(HOSTAPD_SERVICE)
        dnsmasq_active = self._service_active(DNSMASQ_SERVICE)
        networkmanager_active = self._service_active(
            NETWORKMANAGER_SERVICE
        )

        interface_info = self._interface_info()
        interface_type = interface_info.get("type", "")
        addresses = self._addresses()

        wifi_ssid = ""
        if networkmanager_active and interface_type == "managed":
            wifi_ssid = self._wifi_link_ssid()

        if (
            hostapd_active
            and dnsmasq_active
            and interface_type == "AP"
            and addresses
        ):
            mode = "hotspot"

        elif (
            networkmanager_active
            and interface_type == "managed"
            and wifi_ssid
            and addresses
        ):
            mode = "wifi"

        elif operation != "idle":
            mode = "transition"

        elif not addresses:
            mode = "disconnected"

        else:
            mode = "transition"

        active_uuid = ""

        if mode == "wifi":
            cached_profiles = self.profiles()

            for profile in cached_profiles:
                if (
                    wifi_ssid
                    and profile.get("ssid") == wifi_ssid
                ):
                    active_uuid = str(profile.get("uuid", ""))
                    break

            if not active_uuid:
                result_uuid = str(
                    connect_result.get("profile_uuid", "")
                )
                result_ssid = str(
                    connect_result.get("ssid", "")
                )

                if (
                    result_uuid
                    and result_ssid
                    and result_ssid == wifi_ssid
                ):
                    active_uuid = result_uuid

            if not active_uuid:
                active_uuid = str(
                    state.get("last_wifi_uuid", "")
                )

        profiles = self.profiles(active_uuid)

        state_view = copy.deepcopy(state)
        state_view["actual_mode"] = mode

        actual = {
            "mode": mode,
            "interface": self.interface,
            "interface_type": interface_type,
            "addresses": addresses,
            "wifi_uuid": active_uuid,
            "wifi_name": wifi_ssid,
            "hostapd_active": hostapd_active,
            "dnsmasq_active": dnsmasq_active,
            "hotspot_ssid": (
                interface_info.get("ssid", "")
                if mode == "hotspot"
                else "PiBook"
            ),
            "hotspot_url": (
                "http://"
                + addresses[0].split("/", 1)[0]
                + ":5000"
                if mode == "hotspot" and addresses
                else "http://10.42.0.1:5000"
            ),
        }

        scan_status = str(
            scan_results.get("status", "")
        ).lower()

        scan_active = (
            scan_status in {"queued", "running"}
            or operation
            in {
                "scan_queued",
                "scanning",
                "scanning_wifi",
            }
        )

        scan_recovery = operation in {
            "restoring_hotspot",
            "scan_recovery_pending",
        }

        connection_status = str(
            connect_result.get("status", "")
        ).lower()

        connection_active = (
            connection_status in {"queued", "running"}
            or operation
            in {
                "connect_queued",
                "connecting_wifi",
            }
        )

        connection_recovery = operation in {
            "connect_recovery_pending",
        }

        return {
            "network": {
                "state": state_view,
                "actual": actual,
                "safety_timer_active": (
                    scan_recovery or connection_recovery
                ),
            },
            "scan": {
                "scan_service_active": scan_active,
                "recovery_timer_active": scan_recovery,
                "hotspot_active": mode == "hotspot",
                "results": scan_results,
            },
            "connection": {
                "connect_service_active": connection_active,
                "recovery_timer_active": connection_recovery,
                "hotspot_active": mode == "hotspot",
                "wifi_uuid": active_uuid,
                "wifi_name": wifi_ssid,
                "addresses": addresses if mode == "wifi" else [],
                "result": connect_result,
            },
            "profiles": profiles,
        }
