"""
UI screen implementations for Library and Reader.
Uses Pillow for library menu, PyMuPDF for reader pages.
PORTABILITY: 100% portable between Pi 3B+ and Pi Zero 2 W
"""

from PIL import Image, ImageDraw, ImageFont
from typing import List, Optional, Dict
import os
import logging
import socket
import subprocess
import threading

from src.utils.epub_metadata import get_epub_title


def get_ip_address():
    """Get the Pi's local IP address"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "No Network"


def get_wifi_status():
    """Check if WiFi is enabled and connected"""
    try:
        # Check if wlan0 interface exists and is up
        result = subprocess.run(['ip', 'link', 'show', 'wlan0'],
                              capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            # Check if interface is UP
            if 'state UP' in result.stdout or 'UP' in result.stdout:
                return True
        return False
    except Exception:
        return False


def get_bluetooth_status():
    """Check if Bluetooth is enabled"""
    try:
        # Check if bluetooth service is active
        result = subprocess.run(['systemctl', 'is-active', 'bluetooth'],
                              capture_output=True, text=True, timeout=2)
        if result.returncode == 0 and result.stdout.strip() == 'active':
            # Also check if hci0 is up
            hci_result = subprocess.run(['hciconfig', 'hci0'],
                                      capture_output=True, text=True, timeout=2)
            if hci_result.returncode == 0 and 'UP RUNNING' in hci_result.stdout:
                return True
        return False
    except Exception:
        return False


class MainMenuScreen:
    """
    Main menu screen showing available apps
    Users can navigate with single button: press=next app, hold=select app
    """

    def __init__(self, width: int = 800, height: int = 480, font_size: int = 24, battery_monitor=None, web_port: int = 5000, version: str = "v1.0"):
        """
        Initialize main menu screen

        Args:
            width: Screen width
            height: Screen height
            font_size: Base font size
            battery_monitor: Optional BatteryMonitor instance
            web_port: Web server port number
            version: PiBook version string
        """
        self.width = width
        self.height = height
        self.font_size = font_size
        self.battery_monitor = battery_monitor
        self.web_port = web_port
        self.version = version
        self.logger = logging.getLogger(__name__)

        # Load fonts
        try:
            self.font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", font_size)
            self.title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", 36)
            self.version_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", 11)
            self.app_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", 20)
            self.small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", 16)
        except:
            self.font = ImageFont.load_default()
            self.title_font = ImageFont.load_default()
            self.version_font = ImageFont.load_default()
            self.app_font = ImageFont.load_default()
            self.small_font = ImageFont.load_default()

        # Define available apps
        self.apps = [
            {
                'name': 'Livros',
                'icon_filename': 'ereader.png',
                'description': 'Biblioteca EPUB',
                'screen': 'library'
            },
            {
                'name': 'Continuar',
                'icon_filename': 'ereader.png',
                'description': 'Retomar última leitura',
                'screen': 'continue'
            },
            {
                'name': 'Wi-Fi',
                'icon_filename': 'wifi.png',
                'description': 'Rede, Klipper e IP Scanner',
                'screen': 'wifi'
            },
            {
                'name': 'To Do',
                'icon_filename': 'todo.png',
                'description': 'Manage tasks',
                'screen': 'todo'
            },
            {
                'name': 'Terminal',
                'icon_filename': 'terminal.png',
                'description': 'Terminal console',
                'screen': 'typewriter'
            },
            {
                'name': 'Shutdown',
                'icon_filename': 'shutdown.png',
                'description': 'Power off device',
                'screen': 'shutdown'
            }
        ]

        # Pre-load icons. Prefer boot-ready 120x120/1-bit versions so the
        # Pi Zero does not resize and quantize large PNGs on every startup.
        # Originals remain the automatic fallback.
        self.icons = {}
        icon_size = (120, 120)

        # __file__ is src/ui/screens.py -> project root is two levels above src.
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        assets_dir = os.path.join(project_root, 'assets', 'icons')
        boot_cache_dir = os.path.join(assets_dir, 'boot_cache')

        self.logger.info(f"Looking for icons in: {assets_dir}")

        # Livros and Continuar share ereader.png; decode each filename once.
        loaded_by_filename = {}

        for app in self.apps:
            filename = app['icon_filename']

            try:
                if filename in loaded_by_filename:
                    self.icons[app['name']] = loaded_by_filename[filename]
                    self.logger.info(
                        f"Reused icon for {app['name']}: {filename}"
                    )
                    continue

                cached_path = os.path.join(boot_cache_dir, filename)
                original_path = os.path.join(assets_dir, filename)
                icon_path = (
                    cached_path
                    if os.path.exists(cached_path)
                    else original_path
                )

                if not os.path.exists(icon_path):
                    self.logger.warning(f"Icon not found: {original_path}")
                    continue

                with Image.open(icon_path) as source:
                    img = source.copy()

                # Fallback originals still get exactly the old transformation.
                if img.size != icon_size:
                    img = img.resize(icon_size, Image.Resampling.LANCZOS)

                if img.mode != '1':
                    img = img.convert('L')
                    img = img.point(
                        lambda x: 0 if x < 128 else 255,
                        '1',
                    )

                loaded_by_filename[filename] = img
                self.icons[app['name']] = img

                source_kind = (
                    "boot cache"
                    if icon_path == cached_path
                    else "original"
                )
                self.logger.info(
                    f"Loaded icon for {app['name']} from {source_kind}"
                )

            except Exception as e:
                self.logger.error(
                    f"Failed to load icon for {app['name']}: {e}"
                )

        self.current_index = 0  # Currently selected app

    def next_app(self):
        """Move to next app in menu"""
        self.current_index = (self.current_index + 1) % len(self.apps)
        self.logger.info(f"Main menu: selected {self.apps[self.current_index]['name']}")

    def prev_app(self):
        """Move to previous app in menu"""
        self.current_index = (self.current_index - 1) % len(self.apps)
        self.logger.info(
            f"Main menu: selected {self.apps[self.current_index]['name']}"
        )

    def get_selected_app(self):
        """Get currently selected app"""
        return self.apps[self.current_index]

    def _draw_battery_icon(self, draw: ImageDraw.Draw, x: int, y: int, percentage: int, is_charging: bool = False):
        """Draw battery icon (same as LibraryScreen)"""
        battery_width = 30
        battery_height = 14
        terminal_width = 2
        terminal_height = 6

        battery_x = x - battery_width
        draw.rectangle(
            [(battery_x, y), (battery_x + battery_width, y + battery_height)],
            outline=0,
            width=1
        )

        terminal_x = battery_x + battery_width
        terminal_y = y + (battery_height - terminal_height) // 2
        draw.rectangle(
            [(terminal_x, terminal_y), (terminal_x + terminal_width, terminal_y + terminal_height)],
            fill=0
        )

        fill_width = int((battery_width - 4) * (percentage / 100))
        if fill_width > 0:
            draw.rectangle(
                [(battery_x + 2, y + 2), (battery_x + 2 + fill_width, y + battery_height - 2)],
                fill=0
            )

        if is_charging:
            bolt_center_x = battery_x + battery_width // 2
            bolt_center_y = y + battery_height // 2
            # Larger, more visible lightning bolt
            bolt_points = [
                (bolt_center_x + 2, bolt_center_y - 6),    # Top tip
                (bolt_center_x - 2, bolt_center_y - 1),    # Upper left
                (bolt_center_x + 3, bolt_center_y - 1),    # Upper right
                (bolt_center_x - 2, bolt_center_y + 6),    # Bottom tip
                (bolt_center_x + 2, bolt_center_y + 1),    # Lower right
                (bolt_center_x - 3, bolt_center_y + 1),    # Lower left
            ]
            # Draw white bolt with black outline for visibility
            draw.polygon(bolt_points, fill=1, outline=0)

        percentage_text = f"{percentage}%"
        font = ImageFont.load_default()
        try:
            bbox = draw.textbbox((0, 0), percentage_text, font=font)
            text_width = bbox[2] - bbox[0]
        except:
            text_width = len(percentage_text) * 8

        text_x = battery_x - text_width - 5
        draw.text((text_x, y), percentage_text, font=font, fill=0)

    def render(self) -> Image.Image:
        """Render main menu screen"""
        # Create white background
        image = Image.new('1', (self.width, self.height), 1)
        draw = ImageDraw.Draw(image)

        # Draw battery status in top-right corner
        if self.battery_monitor:
            battery_percentage = self.battery_monitor.get_percentage()
            is_charging = self.battery_monitor.is_charging()
            self._draw_battery_icon(draw, self.width - 10, 5, battery_percentage, is_charging)

        # Draw date/time in top-left corner
        try:
            if self.battery_monitor:
                now = self.battery_monitor.get_time()
            else:
                from datetime import datetime
                now = datetime.now()
            time_str = now.strftime("%-I:%M %p")   # e.g. "3:07 PM"  (no leading zero)
            date_str = now.strftime("%b %-d, %Y")   # e.g. "Mar 6, 2026"
        except Exception:
            from datetime import datetime
            now = datetime.now()
            time_str = now.strftime("%I:%M %p").lstrip("0")
            date_str = now.strftime("%b %d, %Y")

        clock_font = ImageFont.load_default()
        try:
            t_bbox = draw.textbbox((0, 0), time_str, font=clock_font)
            time_h = t_bbox[3] - t_bbox[1]
        except Exception:
            time_h = 10
        draw.text((10, 5), time_str, font=clock_font, fill=0)
        draw.text((10, 5 + time_h + 3), date_str, font=clock_font, fill=0)


        title_text = "PiBook"
        version_text = f" {self.version}"
        try:
            bbox = draw.textbbox((0, 0), title_text, font=self.title_font)
            title_width = bbox[2] - bbox[0]
            v_bbox = draw.textbbox((0, 0), version_text, font=self.version_font)
            version_width = v_bbox[2] - v_bbox[0]
        except:
            title_width = len(title_text) * 20
            version_width = len(version_text) * 8

        total_width = title_width + version_width
        start_x = (self.width - total_width) // 2

        draw.text((start_x, 30), title_text, font=self.title_font, fill=0)
        draw.text((start_x + title_width, 50), version_text, font=self.version_font, fill=0)

        # Calculate app icon layout (centered, grid style)
        icon_size = 120
        icon_spacing = 40
        start_y = 120

        # Draw apps in a grid
        for idx, app in enumerate(self.apps):
            # Calculate position (2 apps per row)
            col = idx % 2
            row = idx // 2

            x = self.width // 4 + col * (self.width // 2)
            y = start_y + row * (icon_size + icon_spacing + 60)

            # Highlight selected app with border
            if idx == self.current_index:
                # Draw selection box
                box_size = icon_size + 20
                box_x = x - box_size // 2
                box_y = y - 10
                draw.rectangle(
                    [(box_x, box_y), (box_x + box_size, box_y + box_size + 50)],
                    outline=0,
                    width=3
                )

            # Draw app icon
            if app['name'] in self.icons:
                # Center the 120x120 icon
                icon_x = x - 60
                icon_y = y
                image.paste(self.icons[app['name']], (icon_x, icon_y))
            else:
                # Fallback: draw placeholder box
                icon_x = x - 60
                icon_y = y
                draw.rectangle([(icon_x, icon_y), (icon_x + 120, icon_y + 120)], outline=0, width=2)
                draw.text((icon_x + 45, icon_y + 40), app['name'][0], font=self.title_font, fill=0)

            # Draw app name below icon
            name_text = app['name']
            try:
                bbox = draw.textbbox((0, 0), name_text, font=self.app_font)
                name_width = bbox[2] - bbox[0]
            except:
                name_width = len(name_text) * 10

            name_x = x - name_width // 2
            name_y = y + 130  # Increased from 90 to add more spacing
            draw.text((name_x, name_y), name_text, font=self.app_font, fill=0)

        # Draw instruction text at bottom
        instruction = "Press: Next App  |  Hold: Select App"
        try:
            bbox = draw.textbbox((0, 0), instruction, font=self.app_font)
            instr_width = bbox[2] - bbox[0]
        except:
            instr_width = len(instruction) * 8

        instr_x = (self.width - instr_width) // 2
        draw.text((instr_x, self.height - 55), instruction, font=self.app_font, fill=0)

        # Draw web interface IP address at very bottom
        ip_address = get_ip_address()
        ip_text = f"Web: {ip_address}:{self.web_port}"
        try:
            bbox = draw.textbbox((0, 0), ip_text, font=self.small_font)
            ip_width = bbox[2] - bbox[0]
        except:
            ip_width = len(ip_text) * 8

        ip_x = (self.width - ip_width) // 2
        draw.text((ip_x, self.height - 25), ip_text, font=self.small_font, fill=0)

        return image



class LibraryScreen:
    """
    Book library/selection screen
    Renders a list of available EPUB files using Pillow
    """

    def __init__(self, width: int = 800, height: int = 480, items_per_page: int = 8, font_size: int = 20, web_port: int = 5000, battery_monitor=None):
        """
        Initialize library screen

        Args:
            width: Screen width
            height: Screen height
            items_per_page: Number of books to show per page
            font_size: Font size for menu text
            web_port: Web server port number
            battery_monitor: Optional BatteryMonitor instance
        """
        self.logger = logging.getLogger(__name__)
        self.width = width
        self.height = height
        self.items_per_page = items_per_page
        self.font_size = font_size
        self.web_port = web_port

        self.current_index = 0
        self.current_page = 0
        self.books: List[Dict[str, str]] = []
        self.battery_monitor = battery_monitor
        self.sleep_enabled = True  # Will be updated from main app

        # Try to load fonts
        try:
            self.font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
            self.title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
            self.footer_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18
            )
        except Exception:
            self.logger.warning("TrueType fonts not found, using default")
            self.font = ImageFont.load_default()
            self.title_font = ImageFont.load_default()
            self.footer_font = ImageFont.load_default()
        # Initialize cover extractor
        from src.utils.cover_extractor import CoverExtractor
        self.cover_extractor = CoverExtractor()
        self.cover_size = (100, 150)  # Larger for better detail on e-ink
        
        # Cache for WiFi/BT status (avoid expensive subprocess calls)
        self._wifi_status = None
        self._wifi_check_time = 0
        self._status_cache_duration = 5  # seconds

    def load_books(self, books_dir: str):
        """
        Load list of EPUB files from directory

        Args:
            books_dir: Path to books directory
        """
        self.books = []

        if not os.path.exists(books_dir):
            self.logger.warning(f"Books directory not found: {books_dir}")
            return

        for filename in os.listdir(books_dir):
            if filename.lower().endswith('.epub'):
                filepath = os.path.join(books_dir, filename)
                fallback_title = filename[:-5].replace('_', ' ')
                title = get_epub_title(
                    filepath,
                    fallback=fallback_title,
                )
                self.books.append({
                    'filename': filename,
                    'path': filepath,
                    'title': title
                })

        self.books.sort(key=lambda x: x['title'].lower())

        # Home foi removido da lista: o botão físico Back regressa
        # ao Menu Principal.
        if self.books:
            self.current_index = min(
                self.current_index,
                len(self.books) - 1,
            )
            self.current_page = (
                self.current_index // self.items_per_page
            )
        else:
            self.current_index = 0
            self.current_page = 0

        self.logger.info(f"Loaded {len(self.books)} books")
    
    def _get_cached_wifi_status(self) -> bool:
        """Get WiFi status with caching to avoid expensive subprocess calls"""
        import time
        now = time.time()
        if now - self._wifi_check_time > self._status_cache_duration:
            self._wifi_status = get_wifi_status()
            self._wifi_check_time = now
        return self._wifi_status if self._wifi_status is not None else False
    
    def _wrap_text(self, text: str, max_width: int, draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont) -> list:
        """
        Wrap text to fit within max_width pixels
        
        Args:
            text: Text to wrap
            max_width: Maximum width in pixels
            draw: ImageDraw object for measuring
            font: Font to use for measuring
            
        Returns:
            List of text lines
        """
        words = text.split()
        lines = []
        current_line = []
        
        for word in words:
            test_line = ' '.join(current_line + [word])
            try:
                # Use draw.textbbox for accurate measurement
                bbox = draw.textbbox((0, 0), test_line, font=font)
                width = bbox[2] - bbox[0]
            except:
                # Fallback
                width = len(test_line) * 10
            
            if width <= max_width:
                current_line.append(word)
            else:
                if current_line:
                    lines.append(' '.join(current_line))
                    current_line = [word]
                else:
                    # Single word too long, add anyway
                    lines.append(word)
                    current_line = []
        
        if current_line:
            lines.append(' '.join(current_line))
        
        return lines if lines else [text]

    def next_item(self):
        """Move selection to next book (with wrap-around)"""
        if len(self.books) == 0:
            return
        
        self.current_index = (self.current_index + 1) % len(self.books)
        self.current_page = self.current_index // self.items_per_page

    def prev_item(self):
        """Move selection to previous book (with wrap-around)"""
        if len(self.books) == 0:
            return
        
        self.current_index = (self.current_index - 1 + len(self.books)) % len(self.books)
        self.current_page = self.current_index // self.items_per_page

    def get_selected_book(self) -> Optional[Dict[str, str]]:
        """
        Get currently selected book

        Returns:
            Book dictionary or None if no books
        """
        if 0 <= self.current_index < len(self.books):
            return self.books[self.current_index]
        return None

    def _draw_battery_icon(self, draw: ImageDraw.Draw, x: int, y: int, percentage: int, is_charging: bool = False):
        """
        Draw battery icon with percentage and charging indicator

        Args:
            draw: ImageDraw object
            x: X position (top-right corner)
            y: Y position
            percentage: Battery percentage (0-100)
            is_charging: Whether battery is currently charging
        """
        # Battery dimensions
        battery_width = 30
        battery_height = 14
        terminal_width = 2
        terminal_height = 6

        # Clear area before drawing to prevent ghosting/overlap
        # Calculate area needed for "100%" text
        try:
            bbox = draw.textbbox((0, 0), "100%", font=self.font)
            max_text_width = bbox[2] - bbox[0]
            max_text_height = bbox[3] - bbox[1]
        except:
            max_text_width = 40
            max_text_height = 20

        # Define clear area (text + battery + terminal)
        clear_x = x - battery_width - max_text_width - 10
        clear_width = max_text_width + battery_width + terminal_width + 15
        clear_height = max(battery_height, max_text_height) + 4
        
        draw.rectangle(
            [(clear_x, y - 2), (clear_x + clear_width, y + clear_height)],
            fill=1,  # White
            outline=None
        )

        # Draw battery outline
        battery_x = x - battery_width
        draw.rectangle(
            [(battery_x, y), (battery_x + battery_width, y + battery_height)],
            outline=0,
            width=1
        )

        # Draw battery terminal (positive end)
        terminal_x = battery_x + battery_width
        terminal_y = y + (battery_height - terminal_height) // 2
        draw.rectangle(
            [(terminal_x, terminal_y), (terminal_x + terminal_width, terminal_y + terminal_height)],
            fill=0
        )

        # Draw battery fill based on percentage
        fill_width = int((battery_width - 4) * (percentage / 100))
        if fill_width > 0:
            draw.rectangle(
                [(battery_x + 2, y + 2), (battery_x + 2 + fill_width, y + battery_height - 2)],
                fill=0
            )

        # Draw charging indicator (lightning bolt) if charging
        if is_charging:
            # Lightning bolt as a filled polygon (more visible)
            bolt_center_x = battery_x + battery_width // 2
            bolt_center_y = y + battery_height // 2
            # Larger, more visible lightning bolt shape
            bolt_points = [
                (bolt_center_x + 1, bolt_center_y - 5),    # Top tip
                (bolt_center_x - 1, bolt_center_y - 1),     # Upper left
                (bolt_center_x + 2, bolt_center_y - 1),     # Upper right
                (bolt_center_x - 1, bolt_center_y + 5),     # Bottom tip
                (bolt_center_x + 1, bolt_center_y + 1),     # Lower right
                (bolt_center_x - 2, bolt_center_y + 1),     # Lower left
            ]
            # Draw as white (inverted) if battery is very full, black otherwise
            bolt_color = 1 if fill_width > battery_width - 6 else 0
            draw.polygon(bolt_points, fill=bolt_color)

        # Draw percentage text
        percentage_text = f"{percentage}%"
        font = ImageFont.load_default()
        try:
            bbox = draw.textbbox((0, 0), percentage_text, font=font)
            text_width = bbox[2] - bbox[0]
        except:
            text_width = len(percentage_text) * 8

        text_x = battery_x - text_width - 5
        draw.text((text_x, y), percentage_text, font=font, fill=0)

    def render(self) -> Image.Image:
        """
        Render library screen to PIL Image

        Returns:
            PIL Image (1-bit, for e-ink display)
        """
        # Create white background
        image = Image.new('1', (self.width, self.height), 1)
        draw = ImageDraw.Draw(image)

        # Draw battery status in top-right corner
        if self.battery_monitor:
            battery_percentage = self.battery_monitor.get_percentage()
            is_charging = self.battery_monitor.is_charging()
            self._draw_battery_icon(draw, self.width - 10, 5, battery_percentage, is_charging)

        # Draw title
        draw.text((40, 30), "Library", font=self.title_font, fill=0)
        draw.line([(40, 65), (self.width - 40, 65)], fill=0, width=2)

        if not self.books:
            # No books available
            draw.text((40, 100), "No EPUB files found in books directory", font=self.font, fill=0)
            draw.text((40, 140), "Add .epub files to:", font=self.font, fill=0)
            draw.text((40, 170), "/home/pi/PiBook/books/", font=self.font, fill=0)
            return image

        # Calculate visible range
        start_idx = self.current_page * self.items_per_page
        end_idx = min(start_idx + self.items_per_page, len(self.books))

        # Draw book list with covers
        y = 85
        line_height = 160  # Increased for larger covers (100x150) + 4 lines of text

        for i in range(start_idx, end_idx):
            book = self.books[i]
            is_selected = (i == self.current_index)

            # Get or create cover (skip for __home__ sentinel entry)
            if book['path'] == '__home__':
                cover = self.cover_extractor.create_fallback_cover(self.cover_size)
            else:
                cover = self.cover_extractor.get_cover(book['path'], self.cover_size)
                if not cover:
                    cover = self.cover_extractor.create_fallback_cover(self.cover_size)
            
            # Draw cover
            cover_x = 40
            cover_y = y
            image.paste(cover, (cover_x, cover_y))
            
            # Draw border around cover
            draw.rectangle(
                [(cover_x, cover_y), (cover_x + self.cover_size[0], cover_y + self.cover_size[1])],
                outline=0,
                width=1
            )

            # Draw selection box around entire item
            if is_selected:
                draw.rectangle(
                    [(30, y - 5), (self.width - 30, y + line_height - 10)],
                    outline=0,
                    width=2
                )

            # Draw book title with wrapping
            title = book['title']
            text_x = cover_x + self.cover_size[0] + 15
            text_y = y + 5
            max_text_width = self.width - text_x - 40
            
            # Wrap text to max 4 lines using draw object
            lines = self._wrap_text(title, max_text_width, draw, self.font)
            if len(lines) > 4:
                # Truncate to 4 lines with ellipsis
                lines = lines[:4]
                if len(lines[3]) > 3:
                    lines[3] = lines[3][:-3] + "..."
            
            # Draw wrapped lines
            for line in lines:
                draw.text((text_x, text_y), line, font=self.font, fill=0)
                text_y += max(22, self.font_size + 2)  # Title line spacing
            
            y += line_height

        # Draw footer with page info
        if len(self.books) > 0:
            footer_text = f"Book {self.current_index + 1} of {len(self.books)}"
            draw.text((40, self.height - 40), footer_text, font=self.footer_font, fill=0)

        # Draw sleep status in bottom-right corner
        sleep_text = "Sleep: ON" if self.sleep_enabled else "Sleep: OFF"
        try:
            bbox = draw.textbbox((0, 0), sleep_text, font=self.footer_font)
            sleep_width = bbox[2] - bbox[0]
            sleep_x = self.width - sleep_width - 40
        except:
            sleep_x = self.width - 140
        draw.text((sleep_x, self.height - 40), sleep_text, font=self.footer_font, fill=0)

        return image


class ReaderScreen:
    """
    Book reading screen
    Uses EPUBRenderer (PyMuPDF) to display pages
    """

    def __init__(self, width: int = 800, height: int = 480, zoom_factor: float = 1.0, cache_size: int = 5, show_page_numbers: bool = True, battery_monitor=None):
        """
        Initialize reader screen

        Args:
            width: Screen width
            height: Screen height
            zoom_factor: Zoom multiplier for content
            cache_size: Number of pages to cache
            show_page_numbers: Whether to show page numbers
            battery_monitor: Optional BatteryMonitor instance
        """
        self.logger = logging.getLogger(__name__)
        self.width = width
        self.height = height
        self.zoom_factor = zoom_factor
        self.show_page_numbers = show_page_numbers

        self.current_page = 0
        self.completion_mode = False
        self.completion_details = None
        self.completion_menu_index = 0
        self.completion_rating_editing = False
        self.completion_rating_value = 0
        self.renderer = None
        self.page_cache = None
        self.epub_path = None
        self.current_book_path = None  # Track current book for progress saving
        self.renderer_type = None
        self.battery_monitor = battery_monitor

        # Helper for page caching
        from src.reader.page_cache import PageCache
        self.PageCache = PageCache
        self.cache_size = cache_size

        # Serialize Pillow/FreeType rendering between foreground and prefetch.
        self._page_render_lock = threading.RLock()
        self._prefetch_generation = 0
        self._prefetch_target = None

    def load_epub(self, epub_path: str, zoom_factor: float = None, progress_callback = None):
        """
        Load an EPUB file

        Args:
            epub_path: Path to EPUB file
            zoom_factor: Optional zoom override (uses self.zoom_factor if not provided)
            progress_callback: Optional callback fn(percent, message) for loading updates
        """
        try:
            # Close previous book if open
            if self.renderer:
                self.renderer.close()

            # Use provided settings or defaults
            if zoom_factor is not None:
                self.zoom_factor = zoom_factor

            # Initialize PillowTextRenderer
            from src.reader.pillow_text_renderer import PillowTextRenderer
            self.renderer = PillowTextRenderer(
                epub_path,
                width=self.width,
                height=self.height,
                zoom_factor=self.zoom_factor,
                progress_callback=progress_callback
            )
            self.renderer_type = 'pillow'
            self.logger.info(f"Using PillowTextRenderer for: {epub_path}")

            self.epub_path = epub_path
            self.current_book_path = os.path.abspath(epub_path)  # Store absolute path for progress tracking

            self.page_cache = self.PageCache(self.cache_size)
            self.current_page = 0
            self.completion_mode = False
            self.completion_details = None
            self.completion_menu_index = 0
            self.completion_rating_editing = False
            self.completion_rating_value = 0
            self._prefetch_generation += 1
            self._prefetch_target = None

            # Do not render page 0 before saved progress is restored.
            self.page_cache.reset_stats()

            self.logger.info(f"Loaded EPUB: {epub_path} ({self.renderer.get_page_count()} pages, renderer={self.renderer_type}, zoom={self.zoom_factor})")

        except Exception as e:
            self.logger.error(f"Failed to load EPUB: {e}")
            raise

    def next_page(self) -> bool:
        """
        Navigate to next page

        Returns:
            True if navigation occurred, False if on last page
        """
        if not self.renderer:
            return False

        if self.current_page < self.renderer.get_page_count() - 1:
            self._prefetch_target = None
            self.current_page += 1
            self.logger.debug(f"Next page: {self.current_page}")
            return True

        self.logger.debug("Already on last page")
        return False

    def prev_page(self) -> bool:
        """
        Navigate to previous page

        Returns:
            True if navigation occurred, False if on first page
        """
        if not self.renderer:
            return False

        if self.current_page > 0:
            self._prefetch_target = None
            self.current_page -= 1
            self.logger.debug(f"Previous page: {self.current_page + 1}/{self.renderer.get_page_count()}")
            return True
        return False

    def go_to_page(self, page_number: int):
        """
        Jump to specific page number

        Args:
            page_number: Page number to jump to (0-indexed)
        """
        if not self.renderer:
            return

        total_pages = self.renderer.get_page_count()
        if 0 <= page_number < total_pages:
            self._prefetch_target = None
            self.current_page = page_number
            self.logger.info(f"Jumped to page {page_number + 1}/{total_pages}")

    def cache_page(self, page_number: int):
        """
        Pre-cache a specific page

        Args:
            page_number: Page number to cache (0-indexed)
        """
        if not self.renderer:
            return

        total_pages = self.renderer.get_page_count()
        if 0 <= page_number < total_pages:
            with self._page_render_lock:
                if (
                    not self.renderer
                    or self.page_cache is None
                ):
                    return

                # Render and cache the page
                img = self.renderer.render_page(
                    page_number,
                    show_page_number=self.show_page_numbers,
                )
                self.page_cache.put(page_number, img)

            self.logger.debug(
                f"Cached page {page_number + 1}/{total_pages}"
            )

    def prefetch_around_async(self, radius: int):
        """Pre-render nearby pages in RAM without delaying the visible refresh."""
        try:
            radius = int(radius)
        except (TypeError, ValueError):
            radius = 0

        radius = max(0, min(20, radius))

        with self._page_render_lock:
            if not self.renderer or self.page_cache is None:
                return

            center_page = self.current_page
            total_pages = self.renderer.get_page_count()

            window_start = max(0, center_page - radius)
            window_end = min(total_pages - 1, center_page + radius)
            wanted_pages = set(range(window_start, window_end + 1))

            # Reserve the cache for exactly the active Reader window.
            # Stale pages outside it must not evict pages we still need.
            self.page_cache.resize(max(1, len(wanted_pages)))
            self.page_cache.retain_only(wanted_pages)

            if radius == 0:
                self._prefetch_target = None
                return

            generation = self._prefetch_generation
            book_path = self.current_book_path
            target = (generation, center_page, radius)

            if self._prefetch_target == target:
                return

            # Changing this token cancels any older worker at its next page.
            self._prefetch_target = target

            page_numbers = []
            for distance in range(1, radius + 1):
                forward = center_page + distance
                backward = center_page - distance

                # Prefer the next page first, then the previous page.
                if forward < total_pages:
                    page_numbers.append(forward)
                if backward >= 0:
                    page_numbers.append(backward)

            if not any(
                not self.page_cache.contains(page_number)
                for page_number in page_numbers
            ):
                self._prefetch_target = None
                return

        def worker():
            import time as _time

            started = _time.perf_counter()
            rendered = 0
            cancelled = False

            try:
                for page_number in page_numbers:
                    # One page per lock acquisition. Foreground navigation
                    # therefore never waits for an entire prefetch window.
                    with self._page_render_lock:
                        if (
                            self._prefetch_target != target
                            or generation != self._prefetch_generation
                            or book_path != self.current_book_path
                            or not self.renderer
                            or self.page_cache is None
                        ):
                            cancelled = True
                            break

                        if self.page_cache.contains(page_number):
                            continue

                        image = self.renderer.render_page(
                            page_number,
                            show_page_number=self.show_page_numbers,
                        )

                        if (
                            self._prefetch_target != target
                            or generation != self._prefetch_generation
                            or book_path != self.current_book_path
                            or self.page_cache is None
                        ):
                            cancelled = True
                            break

                        self.page_cache.put(page_number, image)
                        rendered += 1

                    # Yield between pages so foreground work gets priority.
                    _time.sleep(0.01)

            except Exception:
                self.logger.exception(
                    "Reader adaptive prefetch failed around page %d",
                    center_page + 1,
                )

            finally:
                elapsed = _time.perf_counter() - started

                with self._page_render_lock:
                    if self._prefetch_target == target:
                        self._prefetch_target = None

                if rendered:
                    self.logger.info(
                        "Adaptive prefetch %s around page %d/%d: "
                        "radius=%d rendered=%d cache=%d in %.3f s",
                        "cancelled" if cancelled else "ready",
                        center_page + 1,
                        total_pages,
                        radius,
                        rendered,
                        len(self.page_cache) if self.page_cache is not None else 0,
                        elapsed,
                    )

        threading.Thread(
            target=worker,
            name="pibook-reader-prefetch",
            daemon=True,
        ).start()

    def show_loading_progress(self, percentage: int, message: str = "Loading..."):
        """
        Display loading progress bar on screen

        Args:
            percentage: Progress percentage (0-100)
            message: Loading message to display

        Returns:
            PIL Image with progress bar
        """
        image = Image.new('1', (self.width, self.height), 255)
        draw = ImageDraw.Draw(image)

        # Load font
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
            small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
        except:
            font = ImageFont.load_default()
            small_font = ImageFont.load_default()

        # Draw title
        try:
            bbox = draw.textbbox((0, 0), message, font=font)
            text_width = bbox[2] - bbox[0]
            text_x = (self.width - text_width) // 2
        except:
            text_x = self.width // 2 - 50

        draw.text((text_x, self.height // 2 - 60), message, font=font, fill=0)

        # Draw progress bar
        bar_width = 400
        bar_height = 30
        bar_x = (self.width - bar_width) // 2
        bar_y = self.height // 2

        # Outline
        draw.rectangle([bar_x, bar_y, bar_x + bar_width, bar_y + bar_height],
                       outline=0, width=2)

        # Fill based on percentage
        fill_width = int((bar_width - 4) * (percentage / 100))
        if fill_width > 0:
            draw.rectangle([bar_x + 2, bar_y + 2,
                           bar_x + 2 + fill_width, bar_y + bar_height - 2],
                           fill=0)

        # Percentage text
        pct_text = f"{int(percentage)}%"
        try:
            bbox = draw.textbbox((0, 0), pct_text, font=small_font)
            pct_width = bbox[2] - bbox[0]
            pct_x = (self.width - pct_width) // 2
        except:
            pct_x = self.width // 2 - 20

        draw.text((pct_x, bar_y + bar_height + 20), pct_text, font=small_font, fill=0)

        return image
    def _draw_battery_icon(self, draw: ImageDraw.Draw, x: int, y: int, percentage: int, is_charging: bool = False):
        """
        Draw battery icon with percentage and charging indicator

        Args:
            draw: ImageDraw object
            x: X position (top-right corner)
            y: Y position
            percentage: Battery percentage (0-100)
            is_charging: Whether battery is currently charging
        """
        # Battery dimensions
        battery_width = 30
        battery_height = 14
        terminal_width = 2
        terminal_height = 6

        # Clear area before drawing to prevent ghosting/overlap
        # Use default font as used in this method
        try:
            # We need to load the default font here for measurement
            # (It is also loaded later for drawing, which is fine)
            font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), "100%", font=font)
            max_text_width = bbox[2] - bbox[0]
            max_text_height = bbox[3] - bbox[1]
        except:
            max_text_width = 40
            max_text_height = 20

        # Define clear area (text + battery + terminal)
        clear_x = x - battery_width - max_text_width - 10
        clear_width = max_text_width + battery_width + terminal_width + 15
        clear_height = max(battery_height, max_text_height) + 4
        
        draw.rectangle(
            [(clear_x, y - 2), (clear_x + clear_width, y + clear_height)],
            fill=1,  # White
            outline=None
        )

        # Draw battery outline
        battery_x = x - battery_width
        draw.rectangle(
            [(battery_x, y), (battery_x + battery_width, y + battery_height)],
            outline=0,
            width=1
        )

        # Draw battery terminal (positive end)
        terminal_x = battery_x + battery_width
        terminal_y = y + (battery_height - terminal_height) // 2
        draw.rectangle(
            [(terminal_x, terminal_y), (terminal_x + terminal_width, terminal_y + terminal_height)],
            fill=0
        )

        # Draw battery fill based on percentage
        fill_width = int((battery_width - 4) * (percentage / 100))
        if fill_width > 0:
            draw.rectangle(
                [(battery_x + 2, y + 2), (battery_x + 2 + fill_width, y + battery_height - 2)],
                fill=0
            )

        # Draw charging indicator (lightning bolt) if charging
        if is_charging:
            # Lightning bolt as a filled polygon (more visible)
            bolt_center_x = battery_x + battery_width // 2
            bolt_center_y = y + battery_height // 2
            # Larger, more visible lightning bolt shape
            bolt_points = [
                (bolt_center_x + 1, bolt_center_y - 5),    # Top tip
                (bolt_center_x - 1, bolt_center_y - 1),     # Upper left
                (bolt_center_x + 2, bolt_center_y - 1),     # Upper right
                (bolt_center_x - 1, bolt_center_y + 5),     # Bottom tip
                (bolt_center_x + 1, bolt_center_y + 1),     # Lower right
                (bolt_center_x - 2, bolt_center_y + 1),     # Lower left
            ]
            # Draw as white (inverted) if battery is very full, black otherwise
            bolt_color = 1 if fill_width > battery_width - 6 else 0
            draw.polygon(bolt_points, fill=bolt_color)

        # Draw percentage text
        percentage_text = f"{percentage}%"
        # Use default font for battery percentage
        font = ImageFont.load_default()
        try:
            bbox = draw.textbbox((0, 0), percentage_text, font=font)
            text_width = bbox[2] - bbox[0]
        except:
            text_width = len(percentage_text) * 8

        text_x = battery_x - text_width - 5
        draw.text((text_x, y), percentage_text, font=font, fill=0)

    def _render_completion_page(self) -> Image.Image:
        """Render the PiBook-generated end-of-book page."""
        img = Image.new('1', (self.width, self.height), 1)
        draw = ImageDraw.Draw(img)

        try:
            title_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                34,
            )
            book_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                24,
            )
            text_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                18,
            )
        except Exception:
            title_font = ImageFont.load_default()
            book_font = ImageFont.load_default()
            text_font = ImageFont.load_default()

        book_title = (
            get_epub_title(self.current_book_path)
            if self.current_book_path
            else "Livro"
        )

        def centered(text, y, font):
            bbox = draw.textbbox((0, 0), text, font=font)
            width = bbox[2] - bbox[0]
            draw.text(
                ((self.width - width) // 2, y),
                text,
                font=font,
                fill=0,
            )

        centered("Fim do livro", 95, title_font)

        draw.line(
            (55, 155, self.width - 55, 155),
            fill=0,
            width=2,
        )

        # Wrap the book title to fit the screen.
        words = str(book_title).split()
        lines = []
        line = ""

        for word in words:
            candidate = f"{line} {word}".strip()
            bbox = draw.textbbox((0, 0), candidate, font=book_font)

            if bbox[2] - bbox[0] <= self.width - 70:
                line = candidate
            else:
                if line:
                    lines.append(line)
                line = word

        if line:
            lines.append(line)

        y = 205
        for line in lines[:3]:
            centered(line, y, book_font)
            y += 34

        centered("Leitura concluída", y + 55, text_font)

        details = self.completion_details or {}
        history = details.get("reading_history") or []
        last_read = history[-1] if history else {}

        def format_date(value):
            if not value:
                return "Desconhecido"
            try:
                from datetime import datetime
                return datetime.fromisoformat(value).strftime("%d/%m/%Y")
            except Exception:
                return "Desconhecido"

        def format_duration(value):
            if value is None:
                return "Desconhecido"

            try:
                seconds = max(0, int(round(float(value))))
            except (TypeError, ValueError):
                return "Desconhecido"

            hours, remainder = divmod(seconds, 3600)
            minutes, seconds = divmod(remainder, 60)

            if hours:
                return f"{hours} h {minutes} min"
            if minutes:
                return f"{minutes} min"
            return f"{seconds} s"

        started = format_date(last_read.get("started_at"))
        finished = format_date(last_read.get("finished_at"))
        duration = format_duration(last_read.get("reading_seconds"))

        times_finished = int(
            details.get("times_finished", 0) or 0
        )

        rating = details.get("rating")

        rows = [
            f"Início: {started}",
            f"Fim: {finished}",
            f"Tempo efetivo: {duration}",
            f"Leituras concluídas: {times_finished}",
        ]

        # Keep the reading information lower and give each line more room.
        row_y = max(y + 120, 370)
        for row in rows:
            centered(row, row_y, text_font)
            row_y += 46

        def get_rating_value():
            if self.completion_rating_editing:
                return max(
                    0,
                    min(10, int(self.completion_rating_value)),
                )

            try:
                value = int(rating)
            except (TypeError, ValueError):
                return None

            return max(0, min(10, value))

        def draw_star(x, y, state):
            """Draw one 5-point star: empty, half or full."""
            import math

            size = 24
            cx = size / 2
            cy = size / 2
            outer = 10
            inner = 4.4

            points = []
            for i in range(10):
                angle = -math.pi / 2 + i * math.pi / 5
                radius = outer if i % 2 == 0 else inner
                points.append(
                    (
                        int(round(cx + math.cos(angle) * radius)),
                        int(round(cy + math.sin(angle) * radius)),
                    )
                )

            star = Image.new('1', (size, size), 1)
            star_draw = ImageDraw.Draw(star)

            if state == "full":
                star_draw.polygon(points, fill=0)
            elif state == "half":
                star_draw.polygon(points, fill=0)
                star_draw.rectangle(
                    (size // 2, 0, size, size),
                    fill=1,
                )
                star_draw.polygon(points, outline=0)
            else:
                star_draw.polygon(points, outline=0)

            img.paste(star, (x, y))

        def draw_rating_item(top, selected):
            value = get_rating_value()

            label = "Avaliação"
            label_bbox = draw.textbbox(
                (0, 0),
                label,
                font=text_font,
            )
            label_width = label_bbox[2] - label_bbox[0]

            star_size = 24
            star_gap = 3
            stars_width = 5 * star_size + 4 * star_gap

            if self.completion_rating_editing:
                value_text = f"< {value}/10 >"
            elif value is None:
                value_text = "—"
            else:
                value_text = f"{value}/10"

            value_bbox = draw.textbbox(
                (0, 0),
                value_text,
                font=text_font,
            )
            value_width = value_bbox[2] - value_bbox[0]

            gap1 = 12
            gap2 = 10
            total_width = (
                label_width
                + gap1
                + stars_width
                + gap2
                + value_width
            )

            x = (self.width - total_width) // 2

            text_y = (
                top
                + (item_height - (label_bbox[3] - label_bbox[1])) // 2
                - label_bbox[1]
            )

            draw.text(
                (x, text_y),
                label,
                font=text_font,
                fill=0,
            )

            stars_x = x + label_width + gap1
            stars_y = top + (item_height - star_size) // 2

            units = value if value is not None else 0

            for i in range(5):
                threshold = i * 2

                if units >= threshold + 2:
                    state = "full"
                elif units == threshold + 1:
                    state = "half"
                else:
                    state = "empty"

                draw_star(
                    stars_x + i * (star_size + star_gap),
                    stars_y,
                    state,
                )

            value_x = stars_x + stars_width + gap2
            value_y = (
                top
                + (item_height - (value_bbox[3] - value_bbox[1])) // 2
                - value_bbox[1]
            )

            draw.text(
                (value_x, value_y),
                value_text,
                font=text_font,
                fill=0,
            )

        menu_items = [
            "Avaliação",
            "Reler",
            "Biblioteca",
        ]

        menu_y = max(595, row_y + 20)
        item_height = 44
        item_gap = 8

        for index, label in enumerate(menu_items):
            selected = index == self.completion_menu_index
            top = menu_y + index * (item_height + item_gap)
            bottom = top + item_height

            if selected:
                draw.rounded_rectangle(
                    (55, top, self.width - 55, bottom),
                    radius=10,
                    outline=0,
                    width=2,
                )

            if index == 0:
                draw_rating_item(top, selected)
                continue

            bbox = draw.textbbox(
                (0, 0),
                label,
                font=text_font,
            )
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]

            draw.text(
                (
                    (self.width - text_width) // 2,
                    top + (item_height - text_height) // 2 - bbox[1],
                ),
                label,
                font=text_font,
                fill=0,
            )

        return img

    def get_current_image(self) -> Image.Image:
        """
        Get current page as PIL Image (with caching)

        Returns:
            PIL Image (1-bit, for e-ink display)
        """
        if not self.renderer:
            # Return blank page if no book loaded
            return Image.new('1', (self.width, self.height), 1)

        if self.completion_mode:
            img = self._render_completion_page()

            if self.battery_monitor:
                draw = ImageDraw.Draw(img)
                self._draw_battery_icon(
                    draw,
                    self.width - 10,
                    5,
                    self.battery_monitor.get_percentage(),
                    self.battery_monitor.is_charging(),
                )

            return img

        # Cache only immutable book content. Dynamic overlays are applied
        # afterwards on every render, including cache hits.
        with self._page_render_lock:
            if not self.renderer or self.page_cache is None:
                return Image.new('1', (self.width, self.height), 1)

            base_img = self.page_cache.get(self.current_page)
            if base_img is None:
                import time as _time
                render_started = _time.perf_counter()
                base_img = self.renderer.render_page(
                    self.current_page,
                    show_page_number=self.show_page_numbers,
                )
                render_elapsed = _time.perf_counter() - render_started
                self.page_cache.put(self.current_page, base_img)
                self.logger.info(
                    "Rendered page %d/%d in %.3f s (cache miss)",
                    self.current_page + 1,
                    self.renderer.get_page_count(),
                    render_elapsed,
                )

        if not self.battery_monitor:
            return base_img

        img = base_img.copy()
        draw = ImageDraw.Draw(img)
        battery_percentage = self.battery_monitor.get_percentage()
        is_charging = self.battery_monitor.is_charging()
        self._draw_battery_icon(
            draw,
            self.width - 10,
            5,
            battery_percentage,
            is_charging,
        )
        return img

    def get_page_info(self) -> Dict[str, any]:
        """
        Get information about current page

        Returns:
            Dictionary with page number, total pages, etc.
        """
        if not self.renderer:
            return {'current': 0, 'total': 0}

        return {
            'current': self.current_page + 1,  # 1-indexed for display
            'total': self.renderer.get_page_count(),
            'cache_stats': self.page_cache.get_stats() if self.page_cache else {}
        }

    def close(self):
        """Close current book and clean up"""
        with self._page_render_lock:
            self._prefetch_generation += 1
            self._prefetch_target = None

            if self.renderer:
                self.renderer.close()
                self.renderer = None

            if self.page_cache is not None:
                self.page_cache.clear()
                self.page_cache = None

        self.logger.info("Reader closed")


