"""Local e-paper Wi-Fi status and control screen for PiBook."""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont


class WiFiScreen:
    """Show network state and switch between hotspot and saved Wi-Fi."""

    ACTIONS = (
        ("Atualizar estado", "refresh"),
        ("Ativar hotspot PiBook", "hotspot"),
        ("Ligar ao Wi-Fi guardado", "wifi"),
        ("Klipper", "klipper"),
        ("IP Scanner", "ip_scanner"),
    )

    def __init__(
        self,
        width: int = 800,
        height: int = 480,
        font_size: int = 18,
        battery_monitor=None,
        web_port: int = 5000,
    ):
        self.width = width
        self.height = height
        self.font_size = font_size
        self.battery_monitor = battery_monitor
        self.web_port = web_port
        self.logger = logging.getLogger(__name__)

        try:
            self.title_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                30,
            )
            self.heading_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                20,
            )
            self.font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                font_size,
            )
            self.small_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                14,
            )
        except OSError:
            self.title_font = ImageFont.load_default()
            self.heading_font = ImageFont.load_default()
            self.font = ImageFont.load_default()
            self.small_font = ImageFont.load_default()

        self.current_index = 0
        self._lock = threading.RLock()
        self._dirty = True
        self._busy = False
        self._active_action = ""
        self._message = ""
        self._message_is_error = False
        self._status: dict[str, Any] = {
            "mode": "unknown",
            "mode_label": "A verificar",
            "ssid": "—",
            "address": "—",
            "operation": "Sem operação",
            "web_address": "—",
            "updated_at": "",
        }

    @staticmethod
    def _run(
        command: list[str],
        *,
        timeout: float = 5.0,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(
                command,
                124,
                stdout="",
                stderr=str(exc),
            )

    @classmethod
    def _service_active(cls, service: str) -> bool:
        result = cls._run(
            ["systemctl", "is-active", "--quiet", service],
            timeout=3.0,
        )
        return result.returncode == 0

    @classmethod
    def _ipv4_addresses(cls) -> list[str]:
        result = cls._run(
            ["ip", "-4", "-o", "address", "show", "dev", "wlan0"],
            timeout=4.0,
        )
        addresses: list[str] = []
        for line in result.stdout.splitlines():
            match = re.search(r"\binet\s+(\S+)", line)
            if match:
                addresses.append(match.group(1))
        return addresses

    @classmethod
    def _connected_ssid(cls) -> str:
        result = cls._run(
            ["iw", "dev", "wlan0", "link"],
            timeout=4.0,
        )
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("SSID:"):
                return stripped.split(":", 1)[1].strip()

        # Fallback for systems where iw does not expose the SSID.
        result = cls._run(["iwgetid", "--raw"], timeout=3.0)
        if result.returncode == 0:
            return result.stdout.strip()
        return ""

    def _read_status(self) -> dict[str, Any]:
        hostapd_active = self._service_active(
            "pibook-network-hostapd.service"
        )
        dnsmasq_active = self._service_active(
            "pibook-network-dnsmasq.service"
        )
        scan_active = self._service_active("pibook-wifi-scan.service")
        connect_active = self._service_active(
            "pibook-wifi-connect.service"
        )

        addresses = self._ipv4_addresses()
        address = addresses[0] if addresses else "—"
        hotspot_address = next(
            (item for item in addresses if item.startswith("10.42.0.1/")),
            "",
        )

        ssid = ""
        if hostapd_active and dnsmasq_active and hotspot_address:
            mode = "hotspot"
            mode_label = "Hotspot"
            ssid = "PiBook"
            address = hotspot_address
            web_address = f"10.42.0.1:{self.web_port}"
        else:
            ssid = self._connected_ssid()
            if ssid:
                mode = "wifi"
                mode_label = "Wi-Fi"
                web_address = f"pibook.local:{self.web_port}"
            elif addresses:
                mode = "transition"
                mode_label = "Em transição"
                web_address = f"{addresses[0].split('/', 1)[0]}:{self.web_port}"
            else:
                mode = "disconnected"
                mode_label = "Sem ligação"
                web_address = "—"

        with self._lock:
            local_busy = self._busy
            local_action = self._active_action

        if scan_active:
            operation = "A pesquisar redes"
        elif connect_active:
            operation = "A ligar ao Wi-Fi"
        elif local_busy and local_action == "hotspot":
            operation = "A ativar hotspot"
        elif local_busy and local_action == "wifi":
            operation = "A ligar ao Wi-Fi"
        elif local_busy:
            operation = "A atualizar estado"
        else:
            operation = "Sem operação"

        return {
            "mode": mode,
            "mode_label": mode_label,
            "ssid": ssid or "—",
            "address": address,
            "operation": operation,
            "web_address": web_address,
            "updated_at": time.strftime("%H:%M:%S"),
        }

    def refresh_status(self) -> None:
        status = self._read_status()
        with self._lock:
            self._status = status
            self._dirty = True

    def on_enter(self) -> None:
        """Refresh status immediately before the screen is displayed."""
        self.refresh_status()

    def next_action(self) -> None:
        with self._lock:
            if self._busy:
                return
            self.current_index = (self.current_index + 1) % len(self.ACTIONS)
            self._dirty = True

    def prev_action(self) -> None:
        with self._lock:
            if self._busy:
                return
            self.current_index = (self.current_index - 1) % len(self.ACTIONS)
            self._dirty = True

    def activate_selected(self):
        # Execute a network action or return a local navigation action.
        with self._lock:
            if self._busy:
                return False

            _, action = self.ACTIONS[self.current_index]

            if action in {"klipper", "ip_scanner"}:
                self._message = ""
                self._message_is_error = False
                self.logger.info("Wi-Fi menu navigation selected: %s", action)
                return action

            mode = str(self._status.get("mode", "unknown"))

            if action == "hotspot" and mode == "hotspot":
                self._message = "O hotspot PiBook já está ativo."
                self._message_is_error = False
                self._dirty = True
                return True

            if action == "wifi" and mode == "wifi":
                self._message = "O PiBook já está ligado ao Wi-Fi."
                self._message_is_error = False
                self._dirty = True
                return True

            self._busy = True
            self._active_action = action
            self._message_is_error = False
            self._message = {
                "refresh": "A atualizar o estado da rede…",
                "hotspot": "A ativar o hotspot PiBook…",
                "wifi": "A ligar ao Wi-Fi guardado…",
            }[action]
            self._dirty = True

        threading.Thread(
            target=self._action_worker,
            args=(action,),
            daemon=True,
            name=f"pibook-wifi-{action}",
        ).start()
        return True


    def _action_worker(self, action: str) -> None:
        error = ""
        try:
            if action == "hotspot":
                result = self._run(
                    [
                        "sudo",
                        "-n",
                        "/usr/local/sbin/pibook-networkctl",
                        "hotspot",
                    ],
                    timeout=150.0,
                )
                if result.returncode != 0:
                    error = (
                        result.stderr.strip()
                        or result.stdout.strip()
                        or f"código {result.returncode}"
                    )
            elif action == "wifi":
                result = self._run(
                    [
                        "sudo",
                        "-n",
                        "/usr/local/sbin/pibook-networkctl",
                        "wifi",
                    ],
                    timeout=150.0,
                )
                if result.returncode != 0:
                    error = (
                        result.stderr.strip()
                        or result.stdout.strip()
                        or f"código {result.returncode}"
                    )

            if action != "refresh":
                time.sleep(1.0)

        except Exception as exc:  # Defensive: keep the UI responsive.
            error = str(exc)
            self.logger.exception("Wi-Fi e-paper action failed")

        with self._lock:
            self._busy = False
            self._active_action = ""

        self.refresh_status()

        with self._lock:
            if error:
                compact = " ".join(error.split())
                self._message = "Erro: " + compact[:105]
                self._message_is_error = True
            elif action == "hotspot":
                self._message = "Hotspot PiBook ativo."
                self._message_is_error = False
            elif action == "wifi":
                self._message = "Ligação ao Wi-Fi concluída."
                self._message_is_error = False
            else:
                self._message = "Estado atualizado."
                self._message_is_error = False
            self._dirty = True

    def needs_render(self) -> bool:
        with self._lock:
            return self._dirty

    def is_busy(self) -> bool:
        with self._lock:
            return self._busy

    def _draw_battery(self, draw: ImageDraw.ImageDraw) -> None:
        if not self.battery_monitor:
            return

        try:
            percentage = int(self.battery_monitor.get_percentage())
            charging = bool(self.battery_monitor.is_charging())
        except Exception:
            return

        x = self.width - 12
        y = 13
        width = 34
        height = 16
        left = x - width
        draw.rectangle((left, y, x, y + height), outline=0, width=2)
        draw.rectangle((x, y + 5, x + 3, y + 11), fill=0)
        fill = max(0, min(width - 6, int((width - 6) * percentage / 100)))
        if fill:
            draw.rectangle((left + 3, y + 3, left + 3 + fill, y + height - 3), fill=0)

        label = f"{percentage}%"
        bbox = draw.textbbox((0, 0), label, font=self.small_font)
        draw.text(
            (left - (bbox[2] - bbox[0]) - 7, y),
            label,
            font=self.small_font,
            fill=0,
        )
        if charging:
            draw.text((left + 12, y - 1), "+", font=self.small_font, fill=1)

    @staticmethod
    def _fit_text(
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.ImageFont,
        max_width: int,
    ) -> str:
        candidate = text
        while candidate:
            bbox = draw.textbbox((0, 0), candidate, font=font)
            if bbox[2] - bbox[0] <= max_width:
                return candidate
            candidate = candidate[:-1]
        return ""

    def render(self) -> Image.Image:
        with self._lock:
            status = dict(self._status)
            selected = self.current_index
            busy = self._busy
            message = self._message
            message_is_error = self._message_is_error
            self._dirty = False

        image = Image.new("1", (self.width, self.height), 1)
        draw = ImageDraw.Draw(image)

        draw.text((20, 12), "Wi-Fi", font=self.title_font, fill=0)
        self._draw_battery(draw)
        draw.line((20, 54, self.width - 20, 54), fill=0, width=2)

        panel = (20, 68, self.width - 20, 210)
        draw.rounded_rectangle(panel, radius=10, outline=0, width=2)
        draw.text((36, 80), "Estado da rede", font=self.heading_font, fill=0)

        rows = (
            ("Modo", status["mode_label"]),
            ("Rede", status["ssid"]),
            ("Endereço", status["address"]),
            ("Operação", status["operation"]),
            ("Página", status["web_address"]),
        )
        y = 102
        for label, value in rows:
            draw.text((38, y), f"{label}:", font=self.small_font, fill=0)
            fitted = self._fit_text(draw, str(value), self.font, 575)
            draw.text((155, y - 3), fitted, font=self.font, fill=0)
            y += 21

        info = (
            "Novas redes e palavras-passe são configuradas na página web."
        )
        draw.text((25, 220), info, font=self.small_font, fill=0)

        action_y = 244
        row_height = 31
        for index, (label, _) in enumerate(self.ACTIONS):
            top = action_y + index * (row_height + 5)
            bottom = top + row_height
            selected_now = index == selected
            fill = 0 if selected_now else 1
            text_fill = 1 if selected_now else 0
            draw.rounded_rectangle(
                (25, top, self.width - 25, bottom),
                radius=8,
                fill=fill,
                outline=0,
                width=2,
            )
            draw.text((45, top + 5), label, font=self.font, fill=text_fill)

            if label.startswith("Ativar hotspot") and status["mode"] == "hotspot":
                state_label = "ATIVO"
            elif label.startswith("Ligar ao Wi-Fi") and status["mode"] == "wifi":
                state_label = "ATIVO"
            else:
                state_label = ""
            if state_label:
                bbox = draw.textbbox((0, 0), state_label, font=self.small_font)
                draw.text(
                    (self.width - 45 - (bbox[2] - bbox[0]), top + 7),
                    state_label,
                    font=self.small_font,
                    fill=text_fill,
                )

        if message:
            prefix = "! " if message_is_error else ""
            display_message = self._fit_text(
                draw,
                prefix + message,
                self.small_font,
                self.width - 50,
            )
            draw.text((25, 427), display_message, font=self.small_font, fill=0)

        instruction = (
            "Toque: escolher   |   Segurar: executar   |   Voltar: sair"
        )
        if busy:
            instruction = "Operação em curso — aguarda a conclusão"
        bbox = draw.textbbox((0, 0), instruction, font=self.small_font)
        draw.text(
            ((self.width - (bbox[2] - bbox[0])) // 2, self.height - 22),
            instruction,
            font=self.small_font,
            fill=0,
        )

        return image
