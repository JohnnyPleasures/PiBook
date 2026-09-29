"""PiBook IP Scanner v2 e-paper view.

A descoberta de rede pertence a IPScannerService.
Este módulo contém apenas apresentação, paginação e um cache curto de estado.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from src.core.ip_scanner_service import IPScannerError, IPScannerService


class IPScannerScreen:
    """E-paper view for the shared IP Scanner v2 service."""

    STATUS_CACHE_SECONDS = 1.5

    def __init__(
        self,
        width: int = 800,
        height: int = 480,
        font_size: int = 18,
        battery_monitor=None,
        service: IPScannerService | None = None,
    ):
        self.width = width
        self.height = height
        self.font_size = font_size
        self.battery_monitor = battery_monitor
        self.service = service or IPScannerService()
        self.logger = logging.getLogger(__name__)

        try:
            font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            bold_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

            self.font = ImageFont.truetype(font_path, font_size)
            self.small_font = ImageFont.truetype(font_path, 14)
            self.title_font = ImageFont.truetype(bold_path, 24)
            self.heading_font = ImageFont.truetype(bold_path, 18)
        except Exception:
            self.font = ImageFont.load_default()
            self.small_font = ImageFont.load_default()
            self.title_font = ImageFont.load_default()
            self.heading_font = ImageFont.load_default()

        self.current_page = 0
        self.items_per_page = 8

        self._status: dict[str, Any] = {
            "status": "never",
            "scanning": False,
            "progress": 0,
            "local_ip": "",
            "network": "",
            "count": 0,
            "devices": [],
            "error": "",
        }
        self._last_status_refresh = 0.0

    def _refresh_status(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()

        if (
            not force
            and now - self._last_status_refresh < self.STATUS_CACHE_SECONDS
        ):
            return self._status

        try:
            payload = self.service.status()
            if isinstance(payload, dict):
                self._status = payload
        except IPScannerError as exc:
            self.logger.warning("IP Scanner status failed: %s", exc)
            self._status = dict(self._status)
            self._status["error"] = str(exc)

        self._last_status_refresh = now
        return self._status

    def refresh_status(self, *, force: bool = False) -> dict[str, Any]:
        """Refresh shared scanner state explicitly."""
        return self._refresh_status(force=force)

    @property
    def scanning(self) -> bool:
        return bool(self._status.get("scanning"))

    @property
    def scan_progress(self) -> int:
        return int(self._status.get("progress") or 0)

    @property
    def devices(self) -> list[dict[str, Any]]:
        value = self._status.get("devices", [])
        return value if isinstance(value, list) else []

    def start_scan(self) -> None:
        """Request one scan through the shared service."""
        try:
            result = self.service.start()

            if result.get("started") or result.get("status") == "already_scanning":
                self.current_page = 0
                self._status = dict(self._status)
                self._status.update({
                    "status": "queued",
                    "scanning": True,
                    "progress": 0,
                    "count": 0,
                    "devices": [],
                    "error": "",
                })
                self._last_status_refresh = time.monotonic()
        except IPScannerError as exc:
            self.logger.error("Failed to start IP Scanner v2: %s", exc)
            self._status = dict(self._status)
            self._status["error"] = str(exc)
            self._status["scanning"] = False

    def next_page(self) -> None:
        devices = self.devices
        total_pages = max(
            1,
            (len(devices) + self.items_per_page - 1)
            // self.items_per_page,
        )
        if self.current_page < total_pages - 1:
            self.current_page += 1

    def prev_page(self) -> None:
        if self.current_page > 0:
            self.current_page -= 1

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

        draw.rectangle(
            (left, y, x, y + height),
            outline=0,
            width=2,
        )
        draw.rectangle(
            (x, y + 5, x + 3, y + 11),
            fill=0,
        )

        fill_width = max(
            0,
            min(
                width - 6,
                int((width - 6) * percentage / 100),
            ),
        )
        if fill_width:
            draw.rectangle(
                (
                    left + 3,
                    y + 3,
                    left + 3 + fill_width,
                    y + height - 3,
                ),
                fill=0,
            )

        label = f"{percentage}%"
        bbox = draw.textbbox(
            (0, 0),
            label,
            font=self.small_font,
        )
        draw.text(
            (
                left - (bbox[2] - bbox[0]) - 7,
                y,
            ),
            label,
            font=self.small_font,
            fill=0,
        )

        if charging:
            draw.text(
                (left + 12, y - 1),
                "+",
                font=self.small_font,
                fill=1,
            )

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
        status = dict(self._refresh_status())
        devices = status.get("devices", [])
        if not isinstance(devices, list):
            devices = []

        scanning = bool(status.get("scanning"))
        error = str(status.get("error") or "")
        local_ip = str(status.get("local_ip") or "—")
        network = str(status.get("network") or "—")

        total_pages = max(
            1,
            (len(devices) + self.items_per_page - 1)
            // self.items_per_page,
        )
        self.current_page = min(
            self.current_page,
            total_pages - 1,
        )

        image = Image.new(
            "1",
            (self.width, self.height),
            1,
        )
        draw = ImageDraw.Draw(image)

        # Cabeçalho
        draw.text(
            (20, 12),
            "IP Scanner",
            font=self.title_font,
            fill=0,
        )
        self._draw_battery(draw)
        draw.line(
            (20, 54, self.width - 20, 54),
            fill=0,
            width=2,
        )

        # Estado
        draw.text(
            (24, 72),
            "Rede local",
            font=self.heading_font,
            fill=0,
        )
        draw.text(
            (24, 100),
            f"PiBook: {local_ip}",
            font=self.font,
            fill=0,
        )
        draw.text(
            (24, 126),
            f"Rede: {network}",
            font=self.font,
            fill=0,
        )

        if scanning:
            draw.text(
                (24, 164),
                "A pesquisar dispositivos…",
                font=self.heading_font,
                fill=0,
            )
            draw.text(
                (24, 194),
                "A pesquisa decorre em segundo plano.",
                font=self.font,
                fill=0,
            )

        elif error:
            draw.text(
                (24, 164),
                "Não foi possível concluir a pesquisa.",
                font=self.heading_font,
                fill=0,
            )
            message = self._fit_text(
                draw,
                error,
                self.font,
                self.width - 48,
            )
            draw.text(
                (24, 194),
                message,
                font=self.font,
                fill=0,
            )

        elif not devices:
            draw.text(
                (24, 164),
                "Nenhum dispositivo encontrado.",
                font=self.heading_font,
                fill=0,
            )
            draw.text(
                (24, 194),
                "Mantém SELECT premido para pesquisar.",
                font=self.font,
                fill=0,
            )

        else:
            draw.text(
                (24, 164),
                f"Dispositivos encontrados: {len(devices)}",
                font=self.heading_font,
                fill=0,
            )

            start = self.current_page * self.items_per_page
            end = min(
                start + self.items_per_page,
                len(devices),
            )

            y = 196
            for device in devices[start:end]:
                ip = str(device.get("ip") or "—")
                name = str(device.get("name") or "").strip()

                if name.lower().startswith("(unknown"):
                    name = ""

                line = ip
                if name:
                    line += f" · {name}"

                line = self._fit_text(
                    draw,
                    line,
                    self.font,
                    self.width - 70,
                )

                draw.text(
                    (34, y),
                    line,
                    font=self.font,
                    fill=0,
                )
                y += 28

            if total_pages > 1:
                page = (
                    f"Página {self.current_page + 1}"
                    f"/{total_pages}"
                )
                bbox = draw.textbbox(
                    (0, 0),
                    page,
                    font=self.small_font,
                )
                draw.text(
                    (
                        self.width
                        - 24
                        - (bbox[2] - bbox[0]),
                        self.height - 32,
                    ),
                    page,
                    font=self.small_font,
                    fill=0,
                )

        # Rodapé
        draw.line(
            (
                20,
                self.height - 52,
                self.width - 20,
                self.height - 52,
            ),
            fill=0,
            width=1,
        )

        if scanning:
            footer = "A pesquisar… · HOLD BACK: voltar"
        elif total_pages > 1:
            footer = "NEXT/PREV: páginas · SELECT: pesquisar"
        else:
            footer = "SELECT: pesquisar · HOLD BACK: voltar"

        footer = self._fit_text(
            draw,
            footer,
            self.small_font,
            self.width - 40,
        )
        draw.text(
            (20, self.height - 38),
            footer,
            font=self.small_font,
            fill=0,
        )

        return image
