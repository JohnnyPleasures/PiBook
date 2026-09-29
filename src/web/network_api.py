"""HTTP API for PiBook Wi-Fi and network management.

This module exposes the already-tested root network helpers through a narrow
Flask API. It never invokes a shell and never places a Wi-Fi password in the
command line or application logs.
"""

from __future__ import annotations

import copy
import json
import logging
import subprocess
import threading
import time
from typing import Any, Callable, Optional

from flask import jsonify, request

from src.core.network_status_service import NetworkStatusService


NETWORKCTL = "/usr/local/sbin/pibook-networkctl"
SCANCTL = "/usr/local/sbin/pibook-wifi-scanctl"
CONNECTCTL = "/usr/local/sbin/pibook-wifi-connectctl"
SUDO = "/usr/bin/sudo"

ALLOWED_COMMANDS = {
    NETWORKCTL,
    SCANCTL,
    CONNECTCTL,
}


class PiBookNetworkAPI:
    """Register and serve the PiBook network API routes."""

    def __init__(self, web_server: Any):
        self.web_server = web_server
        self.app = web_server.flask_app
        self.logger: logging.Logger = web_server.logger
        self._network_status_service = NetworkStatusService()
        self._job_lock = threading.Lock()
        self._job_state: dict[str, Any] = {
            "running": False,
            "action": "idle",
            "started_at": "",
            "completed_at": "",
            "error": "",
        }
        # Status collection invokes several privileged network helpers.
        # Coalesce concurrent requests and cache the result so a Pi Zero
        # never starts overlapping NetworkManager queries.
        self._status_refresh_lock = threading.Lock()
        self._status_cache_lock = threading.Lock()
        self._status_cache: Optional[dict[str, Any]] = None
        self._status_cache_monotonic = 0.0
        self._status_cache_ttl = 60.0
        self._status_cache_stale_max = 600.0
        self._register_routes()

    def _register_routes(self) -> None:
        @self.app.route("/api/network/status")
        def network_status():
            """Return a cached, coalesced network status snapshot."""
            force = request.args.get("fresh", "") == "1"
            try:
                payload, cache_meta = self._network_status_payload(
                    force=force,
                )
                payload["success"] = True
                payload["cache"] = cache_meta
                payload["web_action"] = self._job_snapshot()
                return jsonify(payload)
            except Exception as exc:
                stale, age = self._cached_status(
                    self._status_cache_stale_max,
                )
                if stale is not None:
                    stale["success"] = True
                    stale["stale"] = True
                    stale["warning"] = str(exc)
                    stale["cache"] = {
                        "cached": True,
                        "refreshing": False,
                        "stale": True,
                        "age_seconds": round(age, 3),
                    }
                    stale["web_action"] = self._job_snapshot()
                    self.logger.warning(
                        "Network status refresh failed; "
                        "serving stale cache: %s",
                        exc,
                    )
                    return jsonify(stale)

                self.logger.error(
                    "Network status API failed: %s",
                    exc,
                    exc_info=True,
                )
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": str(exc),
                            "web_action": self._job_snapshot(),
                        }
                    ),
                    503,
                )

        @self.app.route("/api/network/scan/results")
        def network_scan_results():
            """Return the most recent external Wi-Fi scan results."""
            try:
                payload = self._network_status_service.scan_results()
                return jsonify({"success": True, "results": payload})
            except Exception as exc:
                self.logger.error(
                    "Wi-Fi scan results API failed: %s",
                    exc,
                )
                return jsonify({"success": False, "error": str(exc)}), 503

        @self.app.route("/api/network/scan", methods=["POST"])
        def network_scan_start():
            """Start the asynchronous scan and hotspot-restore workflow."""
            try:
                payload = self._json_command(
                    [SCANCTL, "start"],
                    timeout=35,
                )
                self._invalidate_status_cache_later(60.0)
                return (
                    jsonify(
                        {
                            "success": True,
                            "status": "started",
                            "details": payload,
                        }
                    ),
                    202,
                )
            except Exception as exc:
                self.logger.error(
                    "Wi-Fi scan start API failed: %s",
                    exc,
                )
                return jsonify({"success": False, "error": str(exc)}), 409

        @self.app.route("/api/network/connect", methods=["POST"])
        def network_connect():
            """Queue a connection to an external Wi-Fi network."""
            data = request.get_json(silent=True)
            validation_error = self._validate_connect_request(data)
            if validation_error:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": validation_error,
                        }
                    ),
                    400,
                )

            assert isinstance(data, dict)
            ssid = str(data["ssid"])
            password = str(data.get("password", ""))
            security = str(data.get("security", "wpa-psk"))
            hidden = bool(data.get("hidden", False))

            command = [
                CONNECTCTL,
                "start",
                "--ssid",
                ssid,
                "--security",
                security,
            ]
            if hidden:
                command.append("--hidden")
            if security != "open":
                command.append("--password-stdin")

            try:
                payload = self._json_command(
                    command,
                    input_text=password if security != "open" else None,
                    timeout=35,
                )
                self._invalidate_status_cache_later(150.0)
                return (
                    jsonify(
                        {
                            "success": True,
                            "status": "started",
                            "details": payload,
                        }
                    ),
                    202,
                )
            except Exception as exc:
                self.logger.error(
                    "External Wi-Fi connection start API failed: %s",
                    exc,
                )
                return jsonify({"success": False, "error": str(exc)}), 409

        @self.app.route("/api/network/hotspot", methods=["POST"])
        def network_activate_hotspot():
            """Respond first, then activate the PiBook hotspot."""
            scheduled, state = self._schedule(
                "activate_hotspot",
                lambda: self._command(
                    [NETWORKCTL, "hotspot"],
                    timeout=210,
                ),
            )
            if not scheduled:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "Já existe uma ação de rede em curso.",
                            "web_action": state,
                        }
                    ),
                    409,
                )
            return (
                jsonify(
                    {
                        "success": True,
                        "status": "scheduled",
                        "message": (
                            "O hotspot PiBook será ativado. "
                            "A ligação atual poderá terminar."
                        ),
                        "web_action": state,
                    }
                ),
                202,
            )

        @self.app.route("/api/network/wifi", methods=["POST"])
        def network_activate_wifi():
            """Respond first, then restore a saved Wi-Fi profile."""
            data = request.get_json(silent=True)
            if data is None:
                data = {}
            if not isinstance(data, dict):
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "O pedido JSON é inválido.",
                        }
                    ),
                    400,
                )

            requested_uuid = str(data.get("uuid", "")).strip()
            if requested_uuid and not self._valid_uuid(requested_uuid):
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "O UUID do perfil é inválido.",
                        }
                    ),
                    400,
                )

            if requested_uuid and self._profile(requested_uuid) is None:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "O perfil Wi-Fi não existe.",
                        }
                    ),
                    404,
                )

            command = [NETWORKCTL, "wifi"]
            if requested_uuid:
                command.append(requested_uuid)

            scheduled, state = self._schedule(
                "activate_wifi",
                lambda: self._command(command, timeout=430),
            )
            if not scheduled:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "Já existe uma ação de rede em curso.",
                            "web_action": state,
                        }
                    ),
                    409,
                )
            return (
                jsonify(
                    {
                        "success": True,
                        "status": "scheduled",
                        "message": (
                            "O PiBook tentará restaurar o Wi-Fi guardado. "
                            "A ligação ao hotspot poderá terminar."
                        ),
                        "web_action": state,
                    }
                ),
                202,
            )

        @self.app.route("/api/network/profiles")
        def network_profiles():
            """List only Wi-Fi profiles created by PiBook."""
            try:
                snapshot = self._network_status_service.status()
                active_uuid = str(
                    snapshot
                    .get("network", {})
                    .get("actual", {})
                    .get("wifi_uuid", "")
                )
                return jsonify(
                    {
                        "success": True,
                        "profiles": self._profiles(active_uuid),
                        "active_uuid": active_uuid,
                    }
                )
            except Exception as exc:
                self.logger.error(
                    "Wi-Fi profile list API failed: %s",
                    exc,
                )
                return jsonify({"success": False, "error": str(exc)}), 503

        @self.app.route(
            "/api/network/profiles/<profile_uuid>",
            methods=["DELETE"],
        )
        def network_forget_profile(profile_uuid: str):
            """Forget a PiBook-created profile, never a system profile."""
            if not self._valid_uuid(profile_uuid):
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "O UUID do perfil é inválido.",
                        }
                    ),
                    400,
                )

            profile = self._profile(profile_uuid)
            if profile is None:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "O perfil PiBook não existe.",
                        }
                    ),
                    404,
                )

            try:
                snapshot = self._network_status_service.status()
                active_uuid = str(
                    snapshot
                    .get("network", {})
                    .get("actual", {})
                    .get("wifi_uuid", "")
                )
            except Exception as exc:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": (
                                "Não foi possível confirmar a rede ativa: "
                                + str(exc)
                            ),
                        }
                    ),
                    503,
                )

            def forget_worker() -> None:
                if active_uuid == profile_uuid:
                    self._command(
                        [NETWORKCTL, "hotspot"],
                        timeout=210,
                    )
                self._command(
                    [
                        NETWORKCTL,
                        "forget-profile",
                        profile_uuid,
                    ],
                    timeout=30,
                )

            scheduled, state = self._schedule(
                "forget_wifi_profile",
                forget_worker,
            )
            if not scheduled:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "Já existe uma ação de rede em curso.",
                            "web_action": state,
                        }
                    ),
                    409,
                )

            return (
                jsonify(
                    {
                        "success": True,
                        "status": "scheduled",
                        "profile": profile,
                        "switching_to_hotspot": active_uuid == profile_uuid,
                        "web_action": state,
                    }
                ),
                202,
            )

    def _cached_status(
        self,
        max_age: float,
    ) -> tuple[Optional[dict[str, Any]], float]:
        """Return a defensive copy of the cached status when usable."""
        with self._status_cache_lock:
            if self._status_cache is None:
                return None, 0.0
            age = max(
                0.0,
                time.monotonic() - self._status_cache_monotonic,
            )
            if age > max_age:
                return None, age
            return copy.deepcopy(self._status_cache), age

    def _store_status_cache(self, payload: dict[str, Any]) -> None:
        with self._status_cache_lock:
            self._status_cache = copy.deepcopy(payload)
            self._status_cache_monotonic = time.monotonic()

    def _invalidate_status_cache(self) -> None:
        """Mark the snapshot stale while retaining it as a fallback."""
        with self._status_cache_lock:
            self._status_cache_monotonic = 0.0

    def _invalidate_status_cache_later(self, delay: float) -> None:
        """Invalidate after an async scan or connection has had time to end."""
        def worker() -> None:
            time.sleep(max(0.0, delay))
            self._invalidate_status_cache()

        threading.Thread(target=worker, daemon=True).start()

    def _collect_network_status(self) -> dict[str, Any]:
        """Collect a live snapshot without privileged status helpers."""
        return self._network_status_service.status()

    def _network_status_payload(
        self,
        *,
        force: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Coalesce expensive status collection across all web clients."""
        cached, age = self._cached_status(self._status_cache_ttl)
        if cached is not None and not force:
            return cached, {
                "cached": True,
                "refreshing": False,
                "stale": False,
                "age_seconds": round(age, 3),
            }

        # During a disruptive scheduled action, never start extra
        # NetworkManager queries; serve the last safe snapshot.
        if self._job_snapshot().get("running"):
            cached, age = self._cached_status(
                self._status_cache_stale_max,
            )
            if cached is not None:
                return cached, {
                    "cached": True,
                    "refreshing": True,
                    "stale": True,
                    "age_seconds": round(age, 3),
                }

        acquired = self._status_refresh_lock.acquire(blocking=False)
        if not acquired:
            cached, age = self._cached_status(
                self._status_cache_stale_max,
            )
            if cached is not None:
                return cached, {
                    "cached": True,
                    "refreshing": True,
                    "stale": age > self._status_cache_ttl,
                    "age_seconds": round(age, 3),
                }

            acquired = self._status_refresh_lock.acquire(timeout=45.0)
            if not acquired:
                raise RuntimeError(
                    "O estado da rede está ocupado; tenta novamente."
                )

            cached, age = self._cached_status(
                self._status_cache_stale_max,
            )
            if cached is not None:
                self._status_refresh_lock.release()
                return cached, {
                    "cached": True,
                    "refreshing": False,
                    "stale": age > self._status_cache_ttl,
                    "age_seconds": round(age, 3),
                }

        try:
            # Another request may have refreshed while this one waited.
            if not force:
                cached, age = self._cached_status(
                    self._status_cache_ttl,
                )
                if cached is not None:
                    return cached, {
                        "cached": True,
                        "refreshing": False,
                        "stale": False,
                        "age_seconds": round(age, 3),
                    }

            payload = self._collect_network_status()
            self._store_status_cache(payload)
            return copy.deepcopy(payload), {
                "cached": False,
                "refreshing": False,
                "stale": False,
                "age_seconds": 0.0,
            }
        finally:
            self._status_refresh_lock.release()

    def _command(
        self,
        command: list[str],
        *,
        input_text: Optional[str] = None,
        timeout: int = 30,
    ) -> str:
        """Run one allow-listed privileged command without a shell."""
        if not command or command[0] not in ALLOWED_COMMANDS:
            raise RuntimeError("Comando de rede não autorizado.")

        try:
            result = subprocess.run(
                [SUDO, "-n", *command],
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "A operação de rede excedeu o tempo permitido."
            ) from exc

        if result.returncode != 0:
            details = (
                result.stderr.strip()
                or result.stdout.strip()
                or f"código {result.returncode}"
            )
            raise RuntimeError(details)

        return result.stdout.strip()

    def _json_command(
        self,
        command: list[str],
        *,
        input_text: Optional[str] = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        output = self._command(
            command,
            input_text=input_text,
            timeout=timeout,
        )
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "O auxiliar de rede devolveu uma resposta inválida."
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                "O auxiliar de rede não devolveu um objeto JSON."
            )
        return payload

    def _job_snapshot(self) -> dict[str, Any]:
        with self._job_lock:
            return dict(self._job_state)

    def _schedule(
        self,
        action: str,
        task: Callable[[], Any],
        *,
        delay: float = 0.8,
    ) -> tuple[bool, dict[str, Any]]:
        """Schedule a disruptive action after the HTTP response is sent."""
        with self._job_lock:
            if self._job_state.get("running"):
                return False, dict(self._job_state)
            self._job_state = {
                "running": True,
                "action": action,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "completed_at": "",
                "error": "",
            }
            initial = dict(self._job_state)

        self._invalidate_status_cache()

        def worker() -> None:
            error = ""
            try:
                time.sleep(delay)
                task()
            except Exception as exc:
                error = str(exc)
                self.logger.error(
                    "Scheduled network action %s failed: %s",
                    action,
                    exc,
                    exc_info=True,
                )
            finally:
                with self._job_lock:
                    self._job_state = {
                        "running": False,
                        "action": action,
                        "started_at": initial["started_at"],
                        "completed_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%S%z"
                        ),
                        "error": error,
                    }
                self._invalidate_status_cache()

        threading.Thread(target=worker, daemon=True).start()
        return True, initial

    @staticmethod
    def _valid_uuid(value: Any) -> bool:
        if not isinstance(value, str) or len(value) != 36:
            return False
        if value.count("-") != 4:
            return False
        return all(
            character in "0123456789abcdefABCDEF-"
            for character in value
        )

    def _profiles(self, active_uuid: str = "") -> list[dict[str, Any]]:
        """Read the sanitized PiBook profile cache directly."""
        return self._network_status_service.profiles(active_uuid)

    def _profile(self, profile_uuid: str) -> Optional[dict[str, Any]]:
        if not self._valid_uuid(profile_uuid):
            return None
        for profile in self._profiles():
            if profile["uuid"] == profile_uuid:
                return profile
        return None

    @staticmethod
    def _validate_connect_request(data: Any) -> str:
        if not isinstance(data, dict):
            return "É necessário enviar um objeto JSON."

        ssid = data.get("ssid", "")
        password = data.get("password", "")
        security = data.get("security", "wpa-psk")
        hidden = data.get("hidden", False)

        if not isinstance(ssid, str):
            return "O nome da rede é inválido."
        if not ssid or "\x00" in ssid:
            return "O nome da rede não pode estar vazio."
        if len(ssid.encode("utf-8")) > 32:
            return "O nome da rede excede 32 bytes."
        if security not in {"wpa-psk", "sae", "open"}:
            return "O tipo de segurança não é suportado."
        if not isinstance(hidden, bool):
            return "O campo hidden deve ser verdadeiro ou falso."
        if not isinstance(password, str):
            return "A palavra-passe é inválida."

        if security == "open":
            if password:
                return "Uma rede aberta não utiliza palavra-passe."
            return ""

        if 8 <= len(password) <= 63:
            return ""
        if len(password) == 64 and all(
            character in "0123456789abcdefABCDEF"
            for character in password
        ):
            return ""

        return (
            "A palavra-passe WPA deve ter 8–63 caracteres "
            "ou 64 dígitos hexadecimais."
        )


def register_network_routes(web_server: Any) -> PiBookNetworkAPI:
    """Create the API object and retain it for the server lifetime."""
    api = PiBookNetworkAPI(web_server)
    web_server.network_api = api
    return api
