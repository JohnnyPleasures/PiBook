"""Low/critical battery screen for PiBook."""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont


class BatteryAlertScreen:
    def __init__(self, width: int, height: int):
        self.width = int(width)
        self.height = int(height)

        try:
            self.title_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                40,
            )
            self.body_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                24,
            )
            self.small_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                17,
            )
        except OSError:
            self.title_font = ImageFont.load_default()
            self.body_font = ImageFont.load_default()
            self.small_font = ImageFont.load_default()

    @staticmethod
    def _center_x(draw, text: str, font, width: int) -> int:
        box = draw.textbbox((0, 0), text, font=font)
        text_width = box[2] - box[0]
        return max(10, (width - text_width) // 2)

    def render(
        self,
        *,
        critical: bool,
        voltage: float,
        percentage: int,
        shutdown_enabled: bool,
    ) -> Image.Image:
        image = Image.new("1", (self.width, self.height), 1)
        draw = ImageDraw.Draw(image)

        title = "BATERIA CRÍTICA" if critical else "BATERIA FRACA"

        if critical and shutdown_enabled:
            body = "O PiBook vai desligar em segurança."
        elif critical:
            body = "Liga o PiBook ao carregador agora."
        else:
            body = "Liga o PiBook ao carregador."

        draw.rectangle(
            [(20, 20), (self.width - 20, self.height - 20)],
            outline=0,
            width=4,
        )

        y = 105
        draw.text(
            (self._center_x(draw, title, self.title_font, self.width), y),
            title,
            font=self.title_font,
            fill=0,
        )

        y += 90
        draw.text(
            (self._center_x(draw, body, self.body_font, self.width), y),
            body,
            font=self.body_font,
            fill=0,
        )

        detail = f"{float(voltage):.2f} V   ·   {int(percentage)}%"
        y += 70
        draw.text(
            (self._center_x(draw, detail, self.body_font, self.width), y),
            detail,
            font=self.body_font,
            fill=0,
        )

        hint = (
            "Proteção automática ainda em validação."
            if critical and not shutdown_enabled
            else "O aviso é rearmado depois de carregar."
        )
        y += 75
        draw.text(
            (self._center_x(draw, hint, self.small_font, self.width), y),
            hint,
            font=self.small_font,
            fill=0,
        )

        return image
