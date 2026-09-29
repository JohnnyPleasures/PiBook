"""
PiBook E-Reader - Main Application
Event-driven e-ink reader for Raspberry Pi
PORTABILITY: 100% portable between Pi 3B+ and Pi Zero 2 W
"""

import time as _boot_probe_time

_BOOT_PROBE_T0 = _boot_probe_time.monotonic()

def _boot_probe(label):
    elapsed = _boot_probe_time.monotonic() - _BOOT_PROBE_T0
    print(f"BOOTPROBE +{elapsed:.6f}s {label}", flush=True)

_boot_probe("python-enter")

import sys
import os
import logging
import signal
import gc
import threading
import time
import subprocess
from pathlib import Path

_boot_probe("stdlib-imports-done")

PIBOOK_VERSION = "v1.0"

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import Config
_boot_probe('src.config-done')
from src.display.display_driver import DisplayDriver
_boot_probe('display_driver-done')
from src.hardware.gpio_handler import GPIOHandler
_boot_probe('gpio_handler-done')
from src.hardware.battery_monitor import BatteryMonitor
_boot_probe('battery_monitor-done')
from src.hardware.battery_safety import (
    BatterySafetyConfig,
    BatterySafetyController,
)
_boot_probe('battery_safety-done')
from src.ui.battery_alert_screen import BatteryAlertScreen
_boot_probe('battery_alert-done')
from src.ui.navigation import NavigationManager, Screen
_boot_probe('navigation-done')
from src.ui.screens import MainMenuScreen, LibraryScreen, ReaderScreen
_boot_probe('ui_screens-done')
from src.utils.progress_manager import ProgressManager
_boot_probe('progress_manager-done')
from src.core.power_manager import PowerManager
_boot_probe('power_manager-done')
from src.core.settings import SettingsManager
from src.core.power_profiles import (
    effective_profile,
    reader_prefetch,
    sleep_policy,
    network_reading_enabled,
    network_sleep_enabled,
)
_boot_probe('settings_manager-done')
_boot_probe('all-top-imports-done')


class PiBookApp:
    """
    Main E-Reader application
    """

    def __init__(self, config_path: str):
        """
        Initialize application

        Args:
            config_path: Path to config.yaml
        """
        # Load configuration
        self.config = Config(config_path)

        # Setup logging
        self._setup_logging()
        self.logger = logging.getLogger(__name__)
        # Serialize rendering across GPIO, web and monitor threads.
        self._render_lock = threading.RLock()
        # One-shot FULL request for physical GPIO long-press actions.
        self._force_full_on_next_render = False
        # Serialize expensive EPUB opening across callbacks.
        self._book_open_lock = threading.Lock()

        # A cold EPUB that is already being prepared must not be opened a
        # second time in foreground. While waiting, the physical screen shows
        # the loading frame and Library navigation is temporarily frozen.
        self._waiting_for_book_path = None
        self._waiting_for_book_title = None
        self._book_loading_visible_path = None
        self.logger.info("=" * 50)
        self.logger.info("PiBook E-Reader starting...")
        self.logger.info("=" * 50)

        # Initialize settings manager
        self.settings_manager = SettingsManager(logger=self.logger)
        self.settings = self.settings_manager.get_all()
        self.logger.info(f"User settings loaded: {self.settings}")

        # Bluetooth boot policy is handled by
        # pibook-bluetooth-default-off.service. Restarting only the PiBook
        # application must preserve a Bluetooth state selected in web settings.

        # Initialize components
        display_width = self.config.get('display.width', 800)
        display_height = self.config.get('display.height', 480)
        display_rotation = self.config.get('display.rotation', 0)
        zoom_factor = self.settings.get('zoom', 1.0)

        # PiBook can reach this point before udev has applied the final
        # permissions to GPIO/SPI.  Overlap Python preparation with kernel/udev
        # startup, but never initialize the e-paper until BOTH interfaces are
        # genuinely usable by the PiBook service user.
        hardware_deadline = time.monotonic() + 12.0
        hardware_wait_started = time.monotonic()

        while True:
            gpio_ready = False
            spi_ready = False

            try:
                fd = os.open(
                    "/dev/gpiochip0",
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
                )
                os.close(fd)
                gpio_ready = True
            except OSError:
                pass

            try:
                fd = os.open(
                    "/dev/spidev0.0",
                    os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
                )
                os.close(fd)
                spi_ready = True
            except OSError:
                pass

            if gpio_ready and spi_ready:
                break

            if time.monotonic() >= hardware_deadline:
                missing = []
                if not gpio_ready:
                    missing.append("GPIO")
                if not spi_ready:
                    missing.append("SPI")
                raise RuntimeError(
                    "E-paper hardware did not become ready within 12 seconds: "
                    + ", ".join(missing)
                )

            time.sleep(0.05)

        hardware_wait_elapsed = time.monotonic() - hardware_wait_started
        self.logger.info(
            "E-paper hardware ready after %.3fs startup wait (GPIO + SPI)",
            hardware_wait_elapsed,
        )

        self.display = DisplayDriver(display_width, display_height, display_rotation)
        # Set full refresh interval from settings
        self.display.set_full_refresh_interval(self.settings.get('full_refresh_interval', 5))

        self.gpio = GPIOHandler(self.config.get('gpio_config', 'config/gpio_mapping.yaml'))
        self.navigation = NavigationManager()  # Defaults to Screen.MAIN_MENU

        # Initialize battery monitor (if enabled)
        self.battery_monitor = None
        if self.config.get('battery.enabled', False):
            try:
                self.battery_monitor = BatteryMonitor(
                    adc_channel=self.config.get('battery.adc_channel', 0),
                    voltage_divider_ratio=self.config.get('battery.voltage_divider_ratio', 2.0),
                    min_voltage=self.config.get('battery.min_voltage', 3.0),
                    max_voltage=self.config.get('battery.max_voltage', 4.2),
                    update_interval=self.config.get('battery.update_interval', 30),
                    pisugar_socket=self.config.get('pisugar.socket_path', '/tmp/pisugar-server.sock'),
                    smoothing_samples=self.config.get('battery.smoothing_samples', 5),
                    ina219_bus=self.config.get('battery.ina219_bus', 1),
                    ina219_address=self.config.get('battery.ina219_address', 0x43),
                    ina219_shunt_ohms=self.config.get('battery.ina219_shunt_ohms', 0.1),
                    battery_capacity_mah=self.config.get('battery.capacity_mah', 4000),
                    charge_current_threshold_ma=self.config.get('battery.charge_current_threshold_ma', 20.0),
                    discharge_current_threshold_ma=self.config.get('battery.discharge_current_threshold_ma', -20.0),
                    soc_state_file=self.config.get('battery.soc_state_file', 'data/battery_state.json'),
                    charge_calibrated_capacity_mah=self.config.get('battery.charge_calibrated_capacity_mah', 4142.5),
                    discharge_calibrated_capacity_mah=self.config.get('battery.discharge_calibrated_capacity_mah', 4563.7),
                    soc_persist_step=self.config.get('battery.soc_persist_step', 0.5),
                    soc_persist_interval=self.config.get('battery.soc_persist_interval', 600),
                    soc_state_stale_age=self.config.get('battery.soc_state_stale_age', 21600),
                    soc_restore_max_delta=self.config.get('battery.soc_restore_max_delta', 30),
                    soc_max_integration_gap=self.config.get('battery.soc_max_integration_gap', 120),
                    low_battery_threshold=self.config.get('battery.low_battery_threshold', 15),
                    backend_preference=self.config.get('battery.backend', 'auto'),
                    backend_retry_interval=self.config.get('battery.backend_retry_interval', 15)
                )
                self.logger.info("Battery monitor initialized")
            except Exception as e:
                self.logger.warning(f"Failed to initialize battery monitor: {e}")
                self.battery_monitor = None

        # Safety decisions use voltage/current, never displayed SOC.
        self.battery_safety = None
        self._battery_shutdown_started = False
        self._battery_warning_visible = False
        self._battery_critical_visible = False

        # Perfil energético efetivo pode mudar temporariamente para
        # powersave quando a bateria desce abaixo do limite configurado.
        self._power_profile_apply_pending = False
        self._effective_power_profile = None
        self._reader_network_policy_state = None
        self._reader_network_lock = threading.Lock()
        self._reader_network_desired = None
        self._reader_network_worker_running = False

        self._sleep_network_lock = threading.Lock()
        self._sleep_network_state = "idle"
        self._sleep_network_resume_requested = False
        if (
            self.battery_monitor
            and self.config.get('battery.safety_enabled', True)
        ):
            self.battery_safety = BatterySafetyController(
                BatterySafetyConfig(
                    warning_voltage=float(
                        self.config.get(
                            'battery.warning_voltage',
                            3.62,
                        )
                    ),
                    critical_voltage=float(
                        self.config.get(
                            'battery.critical_voltage',
                            3.50,
                        )
                    ),
                    emergency_voltage=float(
                        self.config.get(
                            'battery.emergency_voltage',
                            3.35,
                        )
                    ),
                    rearm_voltage=float(
                        self.config.get(
                            'battery.rearm_voltage',
                            3.70,
                        )
                    ),
                    discharge_current_threshold_ma=float(
                        self.config.get(
                            'battery.discharge_current_threshold_ma',
                            -20.0,
                        )
                    ),
                    charge_current_threshold_ma=float(
                        self.config.get(
                            'battery.charge_current_threshold_ma',
                            20.0,
                        )
                    ),
                    warning_consecutive_readings=int(
                        self.config.get(
                            'battery.warning_consecutive_readings',
                            2,
                        )
                    ),
                    critical_consecutive_readings=int(
                        self.config.get(
                            'battery.critical_consecutive_readings',
                            3,
                        )
                    ),
                )
            )
            self.logger.info("Battery safety controller initialized")

        # Initialize PiSugar button handler (if enabled)
        self.pisugar_button = None
        if self.config.get('pisugar.button_enabled', False):
            try:
                from src.hardware.pisugar_button_handler import PiSugarButtonHandler
                self.pisugar_button = PiSugarButtonHandler(
                    socket_path=self.config.get('pisugar.button_socket_path', '/tmp/pibook-button.sock')
                )
                # Register callbacks (will be set up after GPIO callbacks)
                self.logger.info("PiSugar button handler initialized")
            except Exception as e:
                self.logger.warning(f"Failed to initialize PiSugar button handler: {e}")
                self.pisugar_button = None

        # Initialize Bluetooth keyboard handler
        self.keyboard_handler = None
        if self.config.get('keyboard.enabled', True):
            try:
                from src.hardware.keyboard_handler import KeyboardHandler
                self.keyboard_handler = KeyboardHandler(
                    device_pattern=self.config.get('keyboard.device_pattern', None),
                    logger=self.logger
                )
                self.logger.info("Bluetooth keyboard handler initialized")
            except Exception as e:
                self.logger.warning(f"Failed to initialize keyboard handler: {e}")
                self.keyboard_handler = None

        # Initialize reading progress manager
        progress_file = self.config.get('reading_progress.progress_file', 'data/reading_progress.json')
        self.progress_manager = ProgressManager(progress_file)

        # Event-driven effective reading clock.
        # No periodic timer: long idle gaps are capped at 5 minutes.
        self._reading_clock_last = None
        self._reading_idle_cap_seconds = 300.0

        self.logger.info("Reading progress manager initialized")
        
        # Disable HDMI for battery savings (never needed for e-ink display)


        # HDMI is disabled via /boot/config.txt (dtoverlay=vc4-kms-v3d,nohdmi)

        self.logger.info("HDMI disabled via boot config")



        # Initialize screens

        web_port = self.config.get('web.port', 5000)

        self.main_menu_screen = MainMenuScreen(
            width=display_width,
            height=display_height,
            font_size=self.config.get('main_menu.font_size', 24),
            battery_monitor=self.battery_monitor,
            web_port=web_port,
            version=PIBOOK_VERSION
        )

        self.library_screen = LibraryScreen(
            width=display_width,
            height=display_height,
            items_per_page=self.config.get('library.items_per_page', 8),
            font_size=self.config.get('library.font_size', 20),
            web_port=web_port,
            battery_monitor=self.battery_monitor
        )

        self.reader_screen = ReaderScreen(
            width=display_width,
            height=display_height,
            zoom_factor=zoom_factor,
            cache_size=self.config.get('reader.page_cache_size', 5),
            show_page_numbers=self.settings.get('show_page_numbers', True),
            battery_monitor=self.battery_monitor
        )

        # EPUB preparation is deliberately true-lazy/post-UI.
        # It is not required for Menu, Library or Reader functionality.
        self.epub_preparation = None

        # IP Scanner is an optional/manual network tool.
        # Keep it completely out of the e-reader startup path.
        self.ip_scanner_screen = None

        # Wi-Fi e-paper UI is not required for e-reader startup.
        # Network/web services remain independent from this screen instance.
        self.wifi_screen = None

        # To Do e-paper UI is optional during startup.
        # The web ToDoManager is independent and remains unaffected.
        self.todo_screen = None

        # Klipper is relatively expensive to import on Pi Zero.
        # Create it only when the user opens Wi-Fi -> Klipper.
        self.klipper_screen = None

        # Typewriter is optional and not required for e-reader startup.
        # Import and construct it only if the user opens Terminal.
        self.typewriter_screen = None

        # Initialize power manager
        self.power_manager = PowerManager(self.config, self.display, self.logger)

        bootstrap_mode = str(
            self.settings.get('power_mode', 'auto')
        ).strip().lower()
        bootstrap_profile = (
            bootstrap_mode
            if bootstrap_mode in {'mains', 'battery', 'powersave'}
            else 'battery'
        )

        bootstrap_sleep, bootstrap_timeout = sleep_policy(
            self.settings,
            bootstrap_profile,
        )
        self.power_manager.sleep_enabled = bootstrap_sleep
        self.power_manager.sleep_timeout = bootstrap_timeout

        sleep_status = (
            "enabled"
            if self.power_manager.sleep_enabled
            else "disabled"
        )
        self.logger.info(
            "Sleep bootstrap: profile=%s %s timeout=%ss",
            bootstrap_profile,
            sleep_status,
            bootstrap_timeout,
        )

        # State
        self.running = False
        self.page_turn_count = 0
        self.gc_threshold = self.config.get('performance.gc_threshold', 100)

        # Track last screen for full refresh on screen change
        self.last_screen = None

        # Sync sleep status to library screen now that it's defined
        self.library_screen.sleep_enabled = self.power_manager.sleep_enabled

        # Web server
        self.web_server = None


    def _setup_logging(self):
        """Configure logging"""
        log_level = getattr(logging, self.config.get('logging.level', 'INFO'))
        log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

        handlers = []

        # Console handler
        if self.config.get('logging.console', True):
            handlers.append(logging.StreamHandler())

        # File handler
        log_file = self.config.get('logging.file')
        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            handlers.append(logging.FileHandler(log_file))

        logging.basicConfig(
            level=log_level,
            format=log_format,
            handlers=handlers
        )

    def _set_cpu_cores(self, num_cores: int):
        """Delegate to PowerManager"""
        self.power_manager.set_cpu_cores(num_cores)

    def _should_pause_epub_preparation(self) -> bool:
        """Yield background EPUB work to foreground activity and power saving."""
        # Opening/reflowing a book has absolute foreground priority.
        if self._book_open_lock.locked():
            return True

        effective = getattr(self, '_effective_power_profile', None)

        # During startup, wait until the real power source/profile is known.
        if effective is None:
            return True

        # In the explicit low-power profile, background pagination remains
        # suspended completely.
        if effective == 'powersave':
            return True

        # Do not equate "Reader is open" with "the user is active".
        # Reuse PowerManager's existing activity clock, which is reset by the
        # physical controls and remote-control handlers.
        grace_seconds = {
            'mains': 2.0,
            'battery': 10.0,
        }.get(str(effective).strip().lower(), 10.0)

        try:
            idle_seconds = max(
                0.0,
                time.time() - self.power_manager.last_activity_time,
            )
        except Exception:
            # Conservative fallback: if activity state cannot be read,
            # temporarily yield rather than competing with the foreground.
            return True

        return idle_seconds < grace_seconds

    def _enable_single_core_mode(self):
        """Delegate to PowerManager"""
        self.power_manager.enable_single_core_mode()

    def _restore_all_cores(self):
        """Delegate to PowerManager"""
        self.power_manager.restore_all_cores()

    def _ensure_ip_scanner_screen(self):
        """Create the IP Scanner only when explicitly requested."""
        if self.ip_scanner_screen is None:
            self.logger.info("Lazy-loading IP Scanner")
            from src.apps.ipscanner import IPScannerScreen

            self.ip_scanner_screen = IPScannerScreen(
                width=self.display.width,
                height=self.display.height,
                font_size=self.config.get('ip_scanner.font_size', 18),
                battery_monitor=self.battery_monitor,
            )

        return self.ip_scanner_screen

    def _ensure_wifi_screen(self):
        """Create the Wi-Fi e-paper screen only on first use."""
        if self.wifi_screen is None:
            self.logger.info("Lazy-loading Wi-Fi screen")
            from src.apps.wifi import WiFiScreen

            self.wifi_screen = WiFiScreen(
                width=self.display.width,
                height=self.display.height,
                font_size=self.config.get('wifi.font_size', 18),
                battery_monitor=self.battery_monitor,
                web_port=self.config.get('web.port', 5000),
            )

        return self.wifi_screen

    def _ensure_todo_screen(self):
        """Create the To Do e-paper screen only on first use."""
        if self.todo_screen is None:
            self.logger.info("Lazy-loading To Do screen")
            from src.apps.todo import ToDoScreen

            self.todo_screen = ToDoScreen(
                width=self.display.width,
                height=self.display.height,
                font_size=self.config.get('todo.font_size', 18),
                battery_monitor=self.battery_monitor,
            )

        return self.todo_screen

    def _ensure_typewriter_screen(self):
        """Create the Typewriter screen only on first use."""
        if self.typewriter_screen is None:
            self.logger.info("Lazy-loading Typewriter screen")
            from src.apps.typewriter import TypewriterScreen

            self.typewriter_screen = TypewriterScreen(
                width=self.display.width,
                height=self.display.height,
                font_size=self.config.get('typewriter.font_size', 16),
                battery_monitor=self.battery_monitor,
            )

        return self.typewriter_screen

    def _ensure_klipper_screen(self):
        """Create the Klipper screen only on first use."""
        if self.klipper_screen is None:
            self.logger.info("Lazy-loading Klipper screen")
            from src.apps.klipper import KlipperScreen

            self.klipper_screen = KlipperScreen(
                width=self.display.width,
                height=self.display.height,
                font_size=self.config.get('klipper.font_size', 18),
                battery_monitor=self.battery_monitor,
            )

        return self.klipper_screen

    def _start_web_server(self):
        """Start the web interface lazily, after the physical UI is ready."""
        if self.web_server or not self.config.get('web.enabled', True):
            return

        try:
            # Flask/Werkzeug/Jinja are deliberately imported only here.
            # This keeps them out of the critical path to the first e-paper menu.
            from src.web.webserver import PiBookWebServer

            web_port = self.config.get('web.port', 5000)
            books_dir = self.config.get(
                'library.books_directory',
                '/home/pi/PiBook/books',
            )

            self.web_server = PiBookWebServer(
                books_dir,
                self,
                web_port,
                version=PIBOOK_VERSION,
            )
            self.web_server.run()
            self.logger.info(
                f"Web interface available at http://<pi-ip>:{web_port}"
            )
        except Exception as exc:
            self.web_server = None
            self.logger.warning(
                f"Failed to start web server: {exc}",
                exc_info=True,
            )

    def _enable_wifi_after_reader(self):
        """End Reader network context and restore if Reader suspended it."""
        self._request_reader_network_state(
            True,
            "reader-exit",
        )

    def _start_post_ui_services_async(self):
        """Start non-critical system services only after the e-paper UI is usable."""

        def worker():
            try:
                result = subprocess.run(
                    [
                        "/usr/bin/sudo",
                        "-n",
                        "/usr/bin/systemctl",
                        "--no-block",
                        "start",
                        "ssh.service",
                        "avahi-daemon.service",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )

                if result.returncode == 0:
                    self.logger.info(
                        "Post-UI system services queued: SSH, Avahi"
                    )
                else:
                    detail = (
                        result.stderr.strip()
                        or result.stdout.strip()
                        or f"rc={result.returncode}"
                    )
                    self.logger.warning(
                        "Could not queue post-UI system services: %s",
                        detail,
                    )
            except Exception as exc:
                self.logger.warning(
                    "Post-UI system services startup failed: %s",
                    exc,
                )

        threading.Thread(
            target=worker,
            daemon=True,
            name="PiBookPostUIServices",
        ).start()

    def _start_battery_logger_after_web_async(self):
        """Start the battery logger only after user-facing startup has settled."""

        def worker():
            try:
                # Avoid competing with e-paper UI, web startup and hotspot setup
                # on the single-core Pi Zero.
                time.sleep(10)

                result = subprocess.run(
                    [
                        "/usr/bin/sudo",
                        "-n",
                        "/usr/bin/systemctl",
                        "--no-block",
                        "start",
                        "pibook-battery-logger.service",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )

                if result.returncode == 0:
                    self.logger.info(
                        "Battery logger queued after web startup"
                    )
                else:
                    detail = (
                        result.stderr.strip()
                        or result.stdout.strip()
                        or f"rc={result.returncode}"
                    )
                    self.logger.warning(
                        "Could not queue battery logger: %s",
                        detail,
                    )
            except Exception as exc:
                self.logger.warning(
                    "Post-web battery logger startup failed: %s",
                    exc,
                )

        threading.Thread(
            target=worker,
            daemon=True,
            name="PiBookBatteryLoggerAfterWeb",
        ).start()

    def _start_network_after_ui_async(self):
        """Queue the network stack only after the physical UI is usable."""

        def worker():
            try:
                self.logger.info(
                    "Physical UI ready; queueing network startup"
                )

                result = subprocess.run(
                    [
                        "/usr/bin/sudo",
                        "-n",
                        "/usr/bin/systemctl",
                        "--no-block",
                        "start",
                        "pibook-network-startup.service",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )

                if result.returncode == 0:
                    self.logger.info(
                        "Network startup queued after physical UI"
                    )
                else:
                    detail = (
                        result.stderr.strip()
                        or result.stdout.strip()
                        or f"rc={result.returncode}"
                    )
                    self.logger.warning(
                        "Could not queue post-UI network startup: %s",
                        detail,
                    )
            except Exception as exc:
                self.logger.warning(
                    "Post-UI network startup failed: %s",
                    exc,
                )

        threading.Thread(
            target=worker,
            daemon=True,
            name="PiBookNetworkAfterUI",
        ).start()

    def _start_epub_preparation_after_ui_async(
        self,
        books_dir: str,
    ) -> None:
        """Import and start EPUB cache preparation outside the boot path."""

        if self.epub_preparation is not None:
            return

        def worker():
            try:
                # Import here so layout-cache dependencies stay out of the
                # critical path to the first usable e-paper menu.
                from src.reader.epub_preparation import EPUBPreparationManager

                manager = EPUBPreparationManager(
                    width=self.display.width,
                    height=self.display.height,
                    zoom_factor=self.settings.get('zoom', 1.0),
                    should_pause=self._should_pause_epub_preparation,
                    logger=self.logger,
                )

                self.epub_preparation = manager

                for epub_path in sorted(Path(books_dir).glob('*.epub')):
                    manager.enqueue(str(epub_path))

                self.logger.info(
                    "EPUB preparation worker started after physical UI"
                )
            except Exception as exc:
                self.logger.warning(
                    "Post-UI EPUB preparation startup failed: %s",
                    exc,
                )

        threading.Thread(
            target=worker,
            daemon=True,
            name="PiBookEPUBPreparationAfterUI",
        ).start()

    def start(self):
        """Start the application"""
        try:
            # Initialize only the hardware needed for the first visible menu.
            self.logger.info("Initializing hardware...")
            self.display.initialize()
            self._register_gpio_callbacks()

            # Show the initial e-paper menu as early as possible. Button
            # handlers deliberately remain inactive while running == False.
            self._render_current_screen()
            self.logger.info("Initial menu visible")

            # Library must be ready before physical input is enabled, so a
            # fast button press cannot enter an unloaded Library screen.
            books_dir = self.config.get(
                'library.books_directory',
                '/home/pi/PiBook/books',
            )
            self.library_screen.load_books(books_dir)

            # Physical controls and battery/inactivity monitor are now usable.
            self.running = True
            self.monitor_thread = threading.Thread(
                target=self._monitor_inactivity,
                daemon=True,
            )
            self.monitor_thread.start()
            self.logger.info("Physical UI ready")

            # Networking is deliberately post-UI. On the single-core Pi Zero W
            # it must not compete with the critical path to the first usable
            # e-paper menu.
            self._start_network_after_ui_async()
            self._start_post_ui_services_async()

            # Web/Terminal starts afterwards.
            self._start_web_server()

            # EPUB cache preparation is non-essential for immediate reading.
            # Start/import it only after the physical UI and web are available.
            self._start_epub_preparation_after_ui_async(books_dir)

            # Battery logging is useful during normal operation, but its Python
            # startup must not compete with the critical single-core boot path.
            self._start_battery_logger_after_web_async()

            self.logger.info("PiBook started successfully!")
            self.logger.info("Press Ctrl+C to exit")

            # Keep running (wake on button press)
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)

            # Pause indefinitely - wake on GPIO interrupts
            signal.pause()

        except KeyboardInterrupt:
            self.logger.info("Received interrupt signal")
            self.stop()
        except Exception as e:
            self.logger.error(f"Fatal error: {e}", exc_info=True)
            self.stop()

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        self.logger.info(f"Received signal {signum}")
        self.stop()
        sys.exit(0)

    def stop(self):
        """Clean shutdown"""
        self.logger.info("Shutting down...")
        self.running = False

        # Preserve the exact coulomb-counter SOC at every clean shutdown.
        # Normal runtime writes remain rate-limited by soc_persist_step.
        if self.battery_monitor:
            try:
                self.battery_monitor.persist_soc_state(refresh=True)
                battery_status = self.battery_monitor.get_status()
                soc_precise = battery_status.get("soc_precise")

                if soc_precise is not None:
                    self.logger.info(
                        "Battery SOC persisted exactly before shutdown: "
                        "%.3f%%",
                        float(soc_precise),
                    )
            except Exception as exc:
                self.logger.warning(
                    "Failed to persist battery SOC before shutdown: %s",
                    exc,
                )

        # Preserve the final effective-reading interval and page position
        # before closing Reader resources.
        try:
            if (
                self.navigation.is_on_screen(Screen.READER)
                and self.reader_screen
                and self.reader_screen.current_book_path
                and self.reader_screen.renderer
            ):
                self._flush_reading_time(continue_session=False)

                self.progress_manager.save_progress(
                    self.reader_screen.current_book_path,
                    self.reader_screen.current_page,
                    self.reader_screen.renderer.get_page_count(),
                )

                self.logger.info(
                    "Reading time and progress saved before PiBook stop"
                )
        except Exception as exc:
            self.logger.warning(
                "Could not preserve Reader state before stop: %s",
                exc,
            )

        # Stop background EPUB preparation before closing reader resources.
        if getattr(self, 'epub_preparation', None):
            self.epub_preparation.stop()

        # Close reader
        if self.reader_screen:
            self.reader_screen.close()

        # Cleanup hardware
        if self.display:
            self.display.cleanup()

        if self.gpio:
            self.gpio.cleanup()

        if self.pisugar_button:
            self.pisugar_button.stop()

        if self.keyboard_handler:
            self.keyboard_handler.stop()

        self.logger.info("PiBook stopped")

    def _register_gpio_callbacks(self):
        """Register GPIO, PiSugar and keyboard callbacks."""
        # GPIO5: short=next, long=confirm/select
        self.gpio.register_callback(
            'toggle', self._handle_next, long_press=False
        )
        self.gpio.register_callback(
            'toggle', self._handle_confirm_long_press, long_press=True
        )

        # GPIO6: short=previous, long=back
        self.gpio.register_callback(
            'back', self._handle_prev, long_press=False
        )
        self.gpio.register_callback(
            'back', self._handle_back_long_press, long_press=True
        )

        self.logger.info(
            "GPIO callbacks registered: "
            "GPIO5=next/confirm, GPIO6=previous/back"
        )

        # Register PiSugar button callbacks (if available)
        if self.pisugar_button:
            self.pisugar_button.register_callback(
                'next_page', self._handle_next
            )
            self.pisugar_button.register_callback(
                'prev_page', self._handle_prev
            )
            self.pisugar_button.register_callback(
                'select', self._handle_confirm
            )
            self.pisugar_button.register_callback(
                'back', self._handle_back
            )
            self.pisugar_button.register_callback(
                'toggle', self._handle_toggle
            )
            self.pisugar_button.start()
            self.logger.info("PiSugar button callbacks registered")

        # Register Bluetooth keyboard callbacks (if available)
        if self.keyboard_handler:
            self.keyboard_handler.register_callback(
                'next', self._handle_next
            )
            self.keyboard_handler.register_callback(
                'prev', self._handle_prev
            )
            self.keyboard_handler.register_callback(
                'select', self._handle_confirm
            )
            self.keyboard_handler.register_callback(
                'back', self._handle_back
            )
            self.keyboard_handler.register_callback(
                'home', self._handle_go_home
            )
            self.keyboard_handler.start()
            self.logger.info("Bluetooth keyboard callbacks registered")

    def _show_battery_alert(
        self,
        *,
        critical: bool,
        voltage: float,
        percentage: int,
    ) -> None:
        """Render one battery warning without changing navigation state."""
        # Critical always supersedes a previous low-battery warning.
        if critical:
            self._battery_warning_visible = False
            self._battery_critical_visible = True
        else:
            self._battery_critical_visible = False
            self._battery_warning_visible = True

        try:
            shutdown_enabled = bool(
                self.config.get(
                    'battery.auto_shutdown_enabled',
                    False,
                )
            )
            screen = BatteryAlertScreen(
                self.display.width,
                self.display.height,
            )
            image = screen.render(
                critical=critical,
                voltage=voltage,
                percentage=percentage,
                shutdown_enabled=shutdown_enabled,
            )
            with self._render_lock:
                self.display.display_image(
                    image,
                    use_partial=False,
                )
        except Exception as exc:
            self.logger.error(
                "Failed to display battery alert: %s",
                exc,
                exc_info=True,
            )

    def _battery_shutdown_worker(
        self,
        voltage: float,
        percentage: int,
    ) -> None:
        """Preserve reader progress and request a clean system shutdown."""
        try:
            if self.navigation.is_on_screen(Screen.READER):
                self._flush_reading_time(continue_session=False)

            if (
                self.navigation.is_on_screen(Screen.READER)
                and self.reader_screen.current_book_path
                and self.reader_screen.renderer
            ):
                self.progress_manager.save_progress(
                    self.reader_screen.current_book_path,
                    self.reader_screen.current_page,
                    self.reader_screen.renderer.get_page_count(),
                )
                self.logger.info(
                    "Saved reading progress before critical-battery shutdown"
                )
        except Exception as exc:
            self.logger.error(
                "Could not save progress before battery shutdown: %s",
                exc,
                exc_info=True,
            )

        if not self.power_manager.is_sleeping:
            self._show_battery_alert(
                critical=True,
                voltage=voltage,
                percentage=percentage,
            )
            time.sleep(2)

        self.logger.critical(
            "Critical battery: %.3f V / %d%%. Requesting shutdown.",
            voltage,
            percentage,
        )

        try:
            result = subprocess.run(
                ["sudo", "-n", "shutdown", "-h", "now"],
                check=False,
                timeout=15,
            )
            if result.returncode != 0:
                self.logger.critical(
                    "Battery shutdown command returned rc=%d",
                    result.returncode,
                )
                self._battery_shutdown_started = False
        except Exception as exc:
            self.logger.critical(
                "Critical-battery shutdown failed: %s",
                exc,
                exc_info=True,
            )
            self._battery_shutdown_started = False

    def _handle_battery_safety(self, status: dict) -> None:
        """Process one valid battery safety sample by backend capability."""
        if not self.battery_safety:
            return

        if (
            not status.get("hardware_available", False)
            or not status.get("measurement_available", False)
            or status.get("measurement_stale", False)
        ):
            return

        safety_mode = str(
            status.get("safety_mode", "none")
        )

        if safety_mode == "none":
            return

        voltage = float(status.get("voltage", 0.0) or 0.0)
        percentage = int(status.get("percentage", 0) or 0)
        current_ma = None

        if safety_mode == "voltage_current":
            raw_current = status.get("current_ma")
            if raw_current is None:
                return

            current_ma = float(raw_current)
            event = self.battery_safety.update(
                voltage=voltage,
                current_ma=current_ma,
            )

        elif safety_mode == "voltage_external_power":
            external_power = status.get("is_charging")
            if not isinstance(external_power, bool):
                external_power = None

            event = self.battery_safety.update_voltage_only(
                voltage=voltage,
                external_power=external_power,
                allow_critical=(external_power is False),
            )

        elif safety_mode == "voltage_only":
            event = self.battery_safety.update_voltage_only(
                voltage=voltage,
                external_power=None,
                allow_critical=False,
            )

        else:
            self.logger.warning(
                "Unknown battery safety mode: %s",
                safety_mode,
            )
            return

        if event == "warning":
            if current_ma is not None:
                self.logger.warning(
                    "Low battery: %.3f V, %.1f mA, %d%%",
                    voltage,
                    current_ma,
                    percentage,
                )
            else:
                self.logger.warning(
                    "Low battery: %.3f V, %d%% (safety=%s)",
                    voltage,
                    percentage,
                    safety_mode,
                )

            if not self.power_manager.is_sleeping:
                self._show_battery_alert(
                    critical=False,
                    voltage=voltage,
                    percentage=percentage,
                )
            return

        if event == "rearmed":
            alert_was_visible = (
                self._battery_warning_visible
                or self._battery_critical_visible
            )

            self._battery_warning_visible = False
            self._battery_critical_visible = False

            if current_ma is not None:
                self.logger.info(
                    "Battery safety rearmed: %.3f V, %.1f mA",
                    voltage,
                    current_ma,
                )
            else:
                self.logger.info(
                    "Battery safety rearmed: %.3f V (safety=%s)",
                    voltage,
                    safety_mode,
                )

            if (
                alert_was_visible
                and not self.power_manager.is_sleeping
            ):
                self.logger.info(
                    "Battery alert cleared after recharge"
                )
                self._update_battery_display()

            return

        if event != "critical":
            return

        auto_shutdown = bool(
            self.config.get(
                'battery.auto_shutdown_enabled',
                False,
            )
        )

        if not auto_shutdown:
            if current_ma is not None:
                self.logger.critical(
                    "Critical battery detected: %.3f V, %.1f mA, "
                    "%d%%; auto shutdown is disabled.",
                    voltage,
                    current_ma,
                    percentage,
                )
            else:
                self.logger.critical(
                    "Critical battery detected: %.3f V, %d%% "
                    "(safety=%s); auto shutdown is disabled.",
                    voltage,
                    percentage,
                    safety_mode,
                )

            if not self.power_manager.is_sleeping:
                self._show_battery_alert(
                    critical=True,
                    voltage=voltage,
                    percentage=percentage,
                )
            return

        if self._battery_shutdown_started:
            return

        self._battery_shutdown_started = True
        threading.Thread(
            target=self._battery_shutdown_worker,
            args=(voltage, percentage),
            daemon=True,
            name="pibook-battery-shutdown",
        ).start()

    def _monitor_inactivity(self):
        """Background thread to check for inactivity and battery status"""
        last_battery_check = 0
        last_battery_percentage = None
        last_battery_charging = None
        last_battery_safety_check = 0
        battery_safety_interval = max(
            5,
            int(self.config.get('battery.safety_check_interval', 10))
        )
        battery_status_interval = max(
            10,
            int(self.config.get('battery.status_check_interval', 30))
        )
        # Debouncing for charging status to filter PiSugar glitches
        charging_debounce_count = 0
        pending_charging_state = None
        # Track IP scanner state for final refresh
        last_scanning_state = False
        # Track Klipper scanner state for final refresh
        last_klipper_scanning_state = False

        while self.running:
            try:
                # Only enter sleep if sleep mode is enabled
                if self.power_manager.should_enter_sleep():
                    self._enter_sleep()

                # Check battery status at the configured interval
                current_time = time.time()
                current_mono = time.monotonic()

                if (self.battery_monitor and
                    not self.power_manager.is_sleeping and
                    current_mono - last_battery_check >= battery_status_interval):

                    # Force a fresh battery reading
                    self.battery_monitor.force_update()

                    battery_percentage = self.battery_monitor.get_percentage()
                    battery_charging = self.battery_monitor.is_charging()
                    battery_power_source = (
                        self.battery_monitor.get_power_source()
                    )

                    # Reuse this battery sample for Power Profiles v2.
                    # No additional battery polling is introduced.
                    self._update_power_profile(
                        battery_percentage,
                        battery_power_source,
                    )

                    # The Waveshare INA219 backend already smooths its
                    # samples. Update the charging icon immediately when
                    # the detected state changes.
                    if battery_charging != last_battery_charging:
                        if last_battery_charging is not None:
                            self.logger.info(
                                f"Battery charging changed: "
                                f"{last_battery_charging} -> "
                                f"{battery_charging}"
                            )
                            last_battery_charging = battery_charging
                            self._update_battery_display()
                        else:
                            # First background reading: initialise state.
                            last_battery_charging = battery_charging
                    # Update display if battery percentage changed
                    # Filter out unrealistic jumps (PiSugar glitches during charging transitions)
                    if last_battery_percentage is not None and battery_percentage != last_battery_percentage:
                        percentage_change = abs(battery_percentage - last_battery_percentage)

                        # Allow changes if:
                        # 1. Small gradual change (<=5% in 5 minutes)
                        # 2. OR charging state is changing (can cause legitimate jumps)
                        if percentage_change <= 5 or battery_charging != last_battery_charging:
                            self.logger.info(f"Battery percentage changed: {last_battery_percentage}% -> {battery_percentage}%")
                            last_battery_percentage = battery_percentage
                            self._update_battery_display()
                        else:
                            # Ignore unrealistic jump (likely PiSugar glitch)
                            self.logger.warning(f"Ignoring unrealistic battery jump: {last_battery_percentage}% -> {battery_percentage}% (change: {percentage_change}%)")
                    elif last_battery_percentage is None:
                        # First reading
                        last_battery_percentage = battery_percentage

                    last_battery_check = current_mono

                # Battery safety is sampled separately from the UI.
                if (
                    self.battery_monitor
                    and self.battery_safety
                    and current_mono - last_battery_safety_check
                    >= battery_safety_interval
                ):
                    self.battery_monitor.force_update()
                    safety_status = self.battery_monitor.get_status()
                    self._handle_battery_safety(safety_status)
                    last_battery_safety_check = current_mono

                # Refresh the Wi-Fi screen only if it is still dirty
                # after acquiring the global render lock. This prevents a
                # monitor refresh from duplicating a button/web refresh.
                if (
                    self.navigation.is_on_screen(Screen.WIFI)
                    and not self.power_manager.is_sleeping
                    and not self._battery_warning_visible
                    and not self._battery_critical_visible
                    and not self._battery_shutdown_started
                ):
                    try:
                        self._render_wifi_if_dirty()
                    except Exception as wifi_error:
                        self.logger.error(
                            f"Error refreshing Wi-Fi screen: {wifi_error}",
                            exc_info=True,
                        )

                # Refresh IP scanner screen while scanning AND once when it completes
                if (
                    self.navigation.is_on_screen(Screen.IP_SCANNER)
                    and not self.power_manager.is_sleeping
                    and not self._battery_warning_visible
                    and not self._battery_critical_visible
                    and not self._battery_shutdown_started
                ):
                    # No scanner polling while idle. Once a scan has
                    # started, refresh its shared state at the view's
                    # rate-limited cadence until completion.
                    if self.ip_scanner_screen.scanning:
                        self.ip_scanner_screen.refresh_status()

                    current_scanning = self.ip_scanner_screen.scanning

                    # Refresh if currently scanning OR just finished scanning
                    if current_scanning or (last_scanning_state and not current_scanning):
                        try:
                            # Force partial refresh during scanning (no full refresh needed)
                            self._render_current_screen(force_partial=True)
                            if not current_scanning and last_scanning_state:
                                self.logger.info("IP scan completed - final refresh done")
                        except Exception as scan_error:
                            self.logger.error(f"Error refreshing IP scanner: {scan_error}", exc_info=True)

                    last_scanning_state = current_scanning

                # Refresh Klipper screen while scanning AND once when it completes.
                # Also trigger a status-only auto-refresh every 3 minutes.
                if (
                    self.navigation.is_on_screen(Screen.KLIPPER)
                    and not self.power_manager.is_sleeping
                    and not self._battery_warning_visible
                    and not self._battery_critical_visible
                    and not self._battery_shutdown_started
                ):
                    current_klipper_scanning = self.klipper_screen.scanning

                    # --- Periodic status refresh (every 3 minutes) ---
                    klipper_has_printers = len(self.klipper_screen.printers) > 0
                    if klipper_has_printers and not current_klipper_scanning:
                        elapsed = current_time - self.klipper_screen.last_status_refresh
                        if (self.klipper_screen.last_status_refresh == 0 or
                                elapsed >= self.klipper_screen.STATUS_REFRESH_INTERVAL):
                            self.logger.info("Triggering Klipper 3-minute status refresh")
                            self.klipper_screen.refresh_all_printers()

                    # --- Refresh display while scanning, just-finished, or during/after status refresh ---
                    needs_display_update = (
                        current_klipper_scanning
                        or (last_klipper_scanning_state and not current_klipper_scanning)
                        or self.klipper_screen.refreshing
                    )
                    # Also do one final display update when a status refresh just finished
                    klipper_just_refreshed = (
                        not self.klipper_screen.refreshing
                        and getattr(self, '_last_klipper_refreshing', False)
                    )
                    self._last_klipper_refreshing = self.klipper_screen.refreshing

                    if needs_display_update or klipper_just_refreshed:
                        try:
                            self._render_current_screen(force_partial=True)
                            if not current_klipper_scanning and last_klipper_scanning_state:
                                self.logger.info("Klipper scan completed - final refresh done")
                            if klipper_just_refreshed:
                                self.logger.info("Klipper status refresh done - display updated")
                        except Exception as scan_error:
                            self.logger.error(f"Error refreshing Klipper screen: {scan_error}", exc_info=True)

                    last_klipper_scanning_state = current_klipper_scanning

                time.sleep(1)  # Check more frequently for scanner updates
            except Exception as e:
                self.logger.error(f"Error in monitor thread: {e}", exc_info=True)
                time.sleep(1)  # Continue monitoring even if error occurs

    def _suspend_network_for_sleep(self, profile):
        """Apply the profile network policy when entering sleep."""
        if network_sleep_enabled(self.settings, profile):
            with self._sleep_network_lock:
                self._sleep_network_state = "idle"
                self._sleep_network_resume_requested = False
            self.logger.info(
                "Sleep network policy: profile=%s network=on",
                profile,
            )
            return

        with self._sleep_network_lock:
            self._sleep_network_state = "suspending"
            self._sleep_network_resume_requested = False

        def worker():
            try:
                result = subprocess.run(
                    [
                        "/usr/bin/sudo",
                        "-n",
                        "/usr/local/sbin/pibook-networkctl",
                        "suspend",
                        "sleep",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=120,
                    check=False,
                )

                if result.returncode == 0:
                    self.logger.info(
                        "Sleep network policy: profile=%s network=off",
                        profile,
                    )
                else:
                    detail = (
                        result.stderr.strip()
                        or result.stdout.strip()
                        or f"code {result.returncode}"
                    )
                    self.logger.info(
                        "Sleep did not take network ownership: %s",
                        detail,
                    )

            except Exception as exc:
                self.logger.warning(
                    "Sleep network suspend failed: %s",
                    exc,
                )
            finally:
                with self._sleep_network_lock:
                    resume_now = self._sleep_network_resume_requested
                    self._sleep_network_state = "armed"

                if resume_now:
                    self._resume_network_after_sleep()

        threading.Thread(
            target=worker,
            name="PiBookSleepNetwork",
            daemon=True,
        ).start()

    def _resume_network_after_sleep(self):
        """Restore only networking associated with the sleep context."""
        with self._sleep_network_lock:
            if self._sleep_network_state == "suspending":
                self._sleep_network_resume_requested = True
                return

            if self._sleep_network_state in {"idle", "resuming"}:
                return

            self._sleep_network_state = "resuming"

        def worker():
            try:
                result = subprocess.run(
                    [
                        "/usr/bin/sudo",
                        "-n",
                        "/usr/local/sbin/pibook-networkctl",
                        "resume",
                        "sleep",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=120,
                    check=False,
                )

                if result.returncode == 0:
                    self.logger.info(
                        "Sleep network context ended."
                    )
                else:
                    detail = (
                        result.stderr.strip()
                        or result.stdout.strip()
                        or f"code {result.returncode}"
                    )
                    self.logger.info(
                        "Sleep network restore not required: %s",
                        detail,
                    )

            except Exception as exc:
                self.logger.warning(
                    "Sleep network restore failed: %s",
                    exc,
                )
            finally:
                with self._sleep_network_lock:
                    self._sleep_network_state = "idle"
                    self._sleep_network_resume_requested = False

        threading.Thread(
            target=worker,
            name="PiBookSleepNetworkRestore",
            daemon=True,
        ).start()

    def _enter_sleep(self):
        """Enter sleep mode"""
        if self.navigation.is_on_screen(Screen.READER):
            self._flush_reading_time(continue_session=False)

        # Save reading progress before sleeping (app-specific)
        if self.navigation.is_on_screen(Screen.READER) and self.reader_screen.current_book_path:
            self.progress_manager.save_progress(
                self.reader_screen.current_book_path,
                self.reader_screen.current_page,
                self.reader_screen.renderer.get_page_count()
            )
            self.logger.info("💾 Saved reading progress before sleep")
        
        profile = self._effective_power_profile or "battery"
        self._suspend_network_for_sleep(profile)

        sleep_message = self.settings.get(
            'sleep_message',
            "Shh I'm sleeping",
        )
        self.power_manager.enter_sleep(sleep_message)

    def _wake_from_sleep(self):
        """Wake from sleep mode."""
        self.power_manager.wake_from_sleep()
        self._resume_network_after_sleep()
        self._render_current_screen()

    def _flush_reading_time(self, continue_session=True):
        """Accumulate effective Reader time, capped at 5 idle minutes."""
        now = time.monotonic()
        last = self._reading_clock_last

        self._reading_clock_last = (
            now if continue_session else None
        )

        if last is None:
            return 0.0

        if (
            not self.reader_screen
            or not self.reader_screen.current_book_path
        ):
            return 0.0

        elapsed = max(0.0, now - last)
        counted = min(
            elapsed,
            self._reading_idle_cap_seconds,
        )

        if counted > 0:
            self.progress_manager.add_reading_time(
                self.reader_screen.current_book_path,
                counted,
            )

        return counted

    def _reset_activity(self) -> bool:
        # While reading, each real interaction closes the previous reading
        # interval. Gaps longer than 5 minutes count only as 300 seconds.
        try:
            if self.navigation.is_on_screen(Screen.READER):
                self._flush_reading_time(continue_session=True)
        except Exception as exc:
            self.logger.debug(
                "Could not flush reading time on interaction: %s",
                exc,
            )

        # Critical battery has absolute priority and cannot be dismissed.
        if self._battery_critical_visible or self._battery_shutdown_started:
            self.logger.info(
                "User interaction ignored during critical-battery protection"
            )
            return False

        # A normal low-battery warning behaves like a modal:
        # the first user interaction only dismisses the warning.
        if self._battery_warning_visible:
            self._battery_warning_visible = False
            self.logger.info(
                "Low-battery visual warning acknowledged; "
                "user interaction consumed"
            )

            self.power_manager.reset_activity()
            self._render_current_screen(force_partial=True)
            return False

        self.power_manager.reset_activity()
        return True

    def _handle_next(self):
        """Handle next button press"""
        if not self.running: return
        
        # Reset activity timer
        if not self._reset_activity():
            return
        
        # If sleeping, just wake up and consume event
        if self.power_manager.is_sleeping:
            self._wake_from_sleep()
            return

        self.logger.info("Button: Next")

        if self._waiting_for_book_path is not None:
            self.logger.info(
                "Book preparation wait active; NEXT ignored"
            )
            return

        if self.navigation.is_on_screen(Screen.MAIN_MENU):
            self.logger.info("🏠 Action: NEXT APP (Main Menu)")
            self.main_menu_screen.next_app()
        elif self.navigation.is_on_screen(Screen.LIBRARY):
            self.logger.info("📖 Action: NEXT (Library - Move down)")
            self.library_screen.next_item()
        elif self.navigation.is_on_screen(Screen.WIFI):
            self.logger.info("📶 Action: NEXT (Wi-Fi - Next action)")
            self.wifi_screen.next_action()
        elif self.navigation.is_on_screen(Screen.IP_SCANNER):
            if self.ip_scanner_screen.scanning:
                self.logger.info(
                    "🔍 Action: NEXT - Scan in progress, ignored"
                )
            else:
                self.logger.info(
                    "🔍 Action: NEXT (IP Scanner - Next page)"
                )
                self.ip_scanner_screen.next_page()
        elif self.navigation.is_on_screen(Screen.TODO):
            self.logger.info("✓ Action: NEXT (To Do - Move down)")
            self._ensure_todo_screen().move_down()
        elif self.navigation.is_on_screen(Screen.KLIPPER):
            # Start scan if no results yet, otherwise scroll pages
            if len(self.klipper_screen.printers) == 0 and not self.klipper_screen.scanning:
                self.logger.info("🖨️ Action: NEXT - Starting Klipper scan")
                self.klipper_screen.start_scan()
            elif not self.klipper_screen.scanning and len(self.klipper_screen.printers) > 0:
                self.logger.info("🖨️ Action: NEXT (Klipper - Next page)")
                self.klipper_screen.next_page()
            else:
                self.logger.info("🖨️ Action: NEXT - Scan in progress, waiting...")
        elif self.navigation.is_on_screen(Screen.READER):
            self.logger.info("📄 Action: NEXT PAGE (Reader)")
            if not self.reader_screen.next_page():
                if self.reader_screen.completion_mode:
                    if self.reader_screen.completion_rating_editing:
                        self.reader_screen.completion_rating_value = min(
                            10,
                            self.reader_screen.completion_rating_value + 1,
                        )
                        self.logger.info(
                            "Completion rating value: %d/10",
                            self.reader_screen.completion_rating_value,
                        )
                    else:
                        self.reader_screen.completion_menu_index = (
                            self.reader_screen.completion_menu_index + 1
                        ) % 3
                        self.logger.info(
                            "Completion menu index: %d",
                            self.reader_screen.completion_menu_index,
                        )

                    self._render_current_screen(force_partial=True)
                    return

                book_path = self.reader_screen.current_book_path
                renderer = self.reader_screen.renderer

                if book_path and renderer:
                    # The interaction itself already closed the previous
                    # reading interval in _reset_activity(). End the session
                    # completely before recording the completed cycle.
                    self._flush_reading_time(
                        continue_session=False
                    )

                    self.progress_manager.mark_finished(
                        book_path,
                        self.reader_screen.current_page,
                        renderer.get_page_count(),
                    )

                    self.reader_screen.completion_details = (
                        self.progress_manager.get_progress_details(book_path)
                    )
                    self.reader_screen.completion_mode = True
                    self._force_full_on_next_render = True

                    self.logger.info(
                        "Reader reached generated completion page"
                    )

                    self._render_current_screen()
                    return

                self.logger.info(
                    "Reader already on last page; no e-paper refresh"
                )
                return
            if self.reader_screen.current_book_path:
                self.progress_manager.save_progress(
                    self.reader_screen.current_book_path,
                    self.reader_screen.current_page,
                    self.reader_screen.renderer.get_page_count()
                )

        self._render_current_screen()
        
        # Force garbage collection on page turn (battery optimization)
        
        self._trigger_gc_if_needed()

    def _handle_prev(self):
        """Handle previous button press"""
        if not self.running: return
        
        if not self._reset_activity():
            return
        if self.power_manager.is_sleeping:
            self._wake_from_sleep()
            return

        self.logger.info("Button: Previous")

        if self._waiting_for_book_path is not None:
            self.logger.info(
                "Book preparation wait active; PREVIOUS ignored"
            )
            return

        if self.navigation.is_on_screen(Screen.MAIN_MENU):
            self.logger.info("🏠 Action: PREVIOUS APP (Main Menu)")
            self.main_menu_screen.prev_app()
        elif self.navigation.is_on_screen(Screen.LIBRARY):
            self.logger.info("📖 Action: PREVIOUS (Library - Move up)")
            self.library_screen.prev_item()
        elif self.navigation.is_on_screen(Screen.WIFI):
            self.logger.info("📶 Action: PREVIOUS (Wi-Fi - Previous action)")
            self.wifi_screen.prev_action()
        elif self.navigation.is_on_screen(Screen.IP_SCANNER):
            self.logger.info("🔍 Action: PREVIOUS (IP Scanner - Previous page)")
            self.ip_scanner_screen.prev_page()
        elif self.navigation.is_on_screen(Screen.TODO):
            self.logger.info("✓ Action: PREVIOUS (To Do - Move up)")
            self._ensure_todo_screen().move_up()
        elif self.navigation.is_on_screen(Screen.READER):
            self.logger.info("📄 Action: PREVIOUS PAGE (Reader)")

            if self.reader_screen.completion_mode:
                if self.reader_screen.completion_rating_editing:
                    self.reader_screen.completion_rating_value = max(
                        0,
                        self.reader_screen.completion_rating_value - 1,
                    )
                    self.logger.info(
                        "Completion rating value: %d/10",
                        self.reader_screen.completion_rating_value,
                    )
                else:
                    self.reader_screen.completion_menu_index = (
                        self.reader_screen.completion_menu_index - 1
                    ) % 3
                    self.logger.info(
                        "Completion menu index: %d",
                        self.reader_screen.completion_menu_index,
                    )

                self._render_current_screen(force_partial=True)
                return

            if not self.reader_screen.prev_page():
                self.logger.info(
                    "Reader already on first page; no e-paper refresh"
                )
                return
            if self.reader_screen.current_book_path:
                self.progress_manager.save_progress(
                    self.reader_screen.current_book_path,
                    self.reader_screen.current_page,
                    self.reader_screen.renderer.get_page_count()
                )

        self._render_current_screen()
        self._trigger_gc_if_needed()

    def _handle_confirm_long_press(self):
        """GPIO5 long press: request one FULL for its visible action."""
        self._force_full_on_next_render = True
        try:
            return self._handle_confirm()
        finally:
            # If the action rendered, the renderer already consumed this.
            # If it did not render, do not leak FULL into a later short press.
            self._force_full_on_next_render = False

    def _handle_back_long_press(self):
        """GPIO6 long press: request one FULL for its visible action."""
        self._force_full_on_next_render = True
        try:
            return self._handle_back()
        finally:
            self._force_full_on_next_render = False

    def _handle_select(self):
        """Handle select button press"""
        if not self.running: return
        
        if not self._reset_activity():
            return
        if self.power_manager.is_sleeping:
            self._wake_from_sleep()
            return

        if self.navigation.is_on_screen(Screen.LIBRARY):
            book = self.library_screen.get_selected_book()
            if book:
                # Check if Home icon is selected
                if book['path'] == '__home__':
                    self.logger.info("🏠 Action: SELECT - Returning to main menu")
                    self.navigation.navigate_to(Screen.MAIN_MENU)
                    self._render_current_screen()
                else:
                    self.logger.info(f"📚 Action: SELECT - Opening book '{book['title']}'")
                    self._open_book(book)
            else:
                self.logger.warning("No book selected")

    def _handle_back(self):
        """Go back one navigation level."""
        if not self.running:
            return

        if not self._reset_activity():
            return
        if self.power_manager.is_sleeping:
            self._wake_from_sleep()
            return

        self.logger.info("Button: Back")

        if self._waiting_for_book_path is not None:
            self.logger.info(
                "Book preparation wait cancelled by BACK: %s",
                self._waiting_for_book_title or self._waiting_for_book_path,
            )
            self._waiting_for_book_path = None
            self._waiting_for_book_title = None
            self._book_loading_visible_path = None
            self._force_full_on_next_render = False
            self._render_current_screen(force_partial=True)
            return

        if self.navigation.is_on_screen(Screen.MAIN_MENU):
            self.logger.info("Already on the main menu")
            return

        if self.navigation.is_on_screen(Screen.READER):
            if (
                self.reader_screen.completion_mode
                and self.reader_screen.completion_rating_editing
            ):
                self.reader_screen.completion_rating_editing = False

                self.logger.info(
                    "Completion rating editing cancelled"
                )

                self._render_current_screen(force_partial=True)
                return

            self._suspend_reader_state()
            self._restore_all_cores()
            self._enable_wifi_after_reader()

            return_screen = getattr(
                self,
                "_reader_return_screen",
                Screen.LIBRARY,
            )

            self.logger.info(
                "Reader Back -> %s",
                getattr(return_screen, "name", str(return_screen)),
            )

            self.navigation.navigate_to(return_screen)
            self._render_current_screen()
            return

        if (
            self.navigation.is_on_screen(Screen.IP_SCANNER)
            or self.navigation.is_on_screen(Screen.KLIPPER)
        ):
            self.logger.info("📶 Returning to Wi-Fi menu")
            self._ensure_wifi_screen()
            self.wifi_screen.on_enter()
            self.navigation.navigate_to(Screen.WIFI)
            self._render_current_screen()
            return

        if self.navigation.is_on_screen(Screen.TYPEWRITER):
            if self.keyboard_handler:
                self.keyboard_handler.raw_key_callback = None

        # Library and auxiliary applications return to main menu.
        self.navigation.navigate_to(Screen.MAIN_MENU)
        self._render_current_screen()

    def _handle_go_home(self):
        """Handle home key press - go directly to main menu"""
        if not self.running: return

        if not self._reset_activity():
            return
        if self.power_manager.is_sleeping:
            self._wake_from_sleep()
            return

        self.logger.info("Keyboard: Home - Going to main menu")

        # Close reader if open
        if self.navigation.is_on_screen(Screen.READER):
            self._suspend_reader_state()
            self._restore_all_cores()
            self._enable_wifi_after_reader()

        # Go to main menu from any screen
        if not self.navigation.is_on_screen(Screen.MAIN_MENU):
            self.navigation.navigate_to(Screen.MAIN_MENU)
            self._render_current_screen()

    def _handle_menu(self):
        """Handle menu button press"""
        if not self.running: return

        if not self._reset_activity():
            return
        if self.power_manager.is_sleeping:
            self._wake_from_sleep()
            return

        self.logger.info("Button: Menu")

        # Always return to main menu
        if self.navigation.is_on_screen(Screen.READER):
            self._suspend_reader_state()
            # Restore all CPU cores when leaving reader
            self._restore_all_cores()
            # Re-enable WiFi when leaving reader
            self._enable_wifi_after_reader()

        self.navigation.navigate_to(Screen.MAIN_MENU)
        self._render_current_screen()

    def _handle_confirm(self):
        # Confirm or select the current item.
        if not self.running:
            return

        if not self._reset_activity():
            return
        if self.power_manager.is_sleeping:
            self._wake_from_sleep()
            return

        self.logger.info("Button: Confirm")

        if self._waiting_for_book_path is not None:
            self.logger.info(
                "Book preparation wait active; CONFIRM ignored"
            )
            return

        if (
            self.navigation.is_on_screen(Screen.MAIN_MENU)
            or self.navigation.is_on_screen(Screen.LIBRARY)
        ):
            self._handle_gpio5_hold()
        elif self.navigation.is_on_screen(Screen.READER):
            if not self.reader_screen.completion_mode:
                self.logger.info(
                    "Confirm has no action in normal Reader mode"
                )
                return

            if self.reader_screen.completion_rating_editing:
                book_path = self.reader_screen.current_book_path
                value = int(
                    self.reader_screen.completion_rating_value
                )

                if book_path:
                    self.progress_manager.set_rating(
                        book_path,
                        value,
                    )
                    self.reader_screen.completion_details = (
                        self.progress_manager.get_progress_details(
                            book_path
                        )
                    )

                self.reader_screen.completion_rating_editing = False

                self.logger.info(
                    "Completion rating saved: %d/10",
                    value,
                )

                self._force_full_on_next_render = True
                self._render_current_screen()
                return

            if self.reader_screen.completion_menu_index == 0:
                details = (
                    self.reader_screen.completion_details or {}
                )
                current = details.get("rating")

                try:
                    current = int(current)
                except (TypeError, ValueError):
                    current = 6

                current = max(0, min(10, current))

                self.reader_screen.completion_rating_value = current
                self.reader_screen.completion_rating_editing = True

                self.logger.info(
                    "Completion rating editing started at %d/10",
                    current,
                )

                self._force_full_on_next_render = True
                self._render_current_screen()
                return

            if self.reader_screen.completion_menu_index == 1:
                if not self._prepare_loaded_book_reread():
                    self.logger.warning(
                        "Re-reading request rejected for current book"
                    )
                    return

                self._force_full_on_next_render = True
                self._render_current_screen()
                self._reading_clock_last = time.monotonic()

                self.logger.info(
                    "Re-reading started from page 1"
                )
                return

            if self.reader_screen.completion_menu_index == 2:
                self.reader_screen.completion_rating_editing = False

                self._suspend_reader_state()
                self._restore_all_cores()
                self._enable_wifi_after_reader()

                self.navigation.navigate_to(Screen.LIBRARY)
                self._render_current_screen()

                self.logger.info(
                    "Completion menu -> Library"
                )
                return

            self.logger.info(
                "Completion menu action %d not connected yet",
                self.reader_screen.completion_menu_index,
            )
            return

        elif self.navigation.is_on_screen(Screen.WIFI):
            self.logger.info("📶 Action: CONFIRM (Wi-Fi)")
            result = self.wifi_screen.activate_selected()

            if result == "klipper":
                self.logger.info("🖨️ Wi-Fi menu -> Klipper")
                self._ensure_klipper_screen()
                self.navigation.navigate_to(Screen.KLIPPER)
                self._render_current_screen()
            elif result == "ip_scanner":
                self.logger.info("🔍 Wi-Fi menu -> IP Scanner")
                self._ensure_ip_scanner_screen()
                self.navigation.navigate_to(Screen.IP_SCANNER)
                self._render_current_screen()
            elif result:
                self._render_current_screen()
        elif self.navigation.is_on_screen(Screen.IP_SCANNER):
            if self.ip_scanner_screen.scanning:
                self.logger.info(
                    "🔍 Action: CONFIRM - Scan already in progress"
                )
                return

            self.logger.info(
                "🔍 Action: CONFIRM - Starting IP Scanner v2"
            )
            self.ip_scanner_screen.start_scan()
            self._render_current_screen()

        elif self.navigation.is_on_screen(Screen.TODO):
            self._handle_toggle()
        else:
            self.logger.info(
                "Confirm has no action on the current screen"
            )



    def _launch_menu_app(self, app, source="MENU"):
        """Launch one main-menu app through the shared application path."""
        if not app:
            self.logger.warning("%s: no menu app supplied", source)
            return False

        screen = app.get("screen")
        name = app.get("name", screen or "unknown")

        self.logger.info(
            "🚀 Action: %s - Launching app '%s'",
            source,
            name,
        )

        if screen == "library":
            self.navigation.navigate_to(Screen.LIBRARY)
            self._render_current_screen()

        elif screen == "continue":
            self._continue_reading()

        elif screen == "wifi":
            self._ensure_wifi_screen()
            self.wifi_screen.on_enter()
            self.navigation.navigate_to(Screen.WIFI)
            self._render_current_screen()

        elif screen == "ip_scanner":
            self._ensure_ip_scanner_screen()
            self.navigation.navigate_to(Screen.IP_SCANNER)
            self._render_current_screen()

        elif screen == "todo":
            self._ensure_todo_screen()
            self.navigation.navigate_to(Screen.TODO)
            self._render_current_screen()

        elif screen == "klipper":
            self._ensure_klipper_screen()
            self.navigation.navigate_to(Screen.KLIPPER)
            self._render_current_screen()

        elif screen == "typewriter":
            self._ensure_typewriter_screen()
            self.navigation.navigate_to(Screen.TYPEWRITER)

            if self.keyboard_handler:
                self.keyboard_handler.raw_key_callback = (
                    self._handle_typewriter_key
                )

            self._render_current_screen()

        elif screen == "shutdown":
            self.logger.info(
                "🛑 Action: SHUT DOWN - Starting shutdown sequence"
            )

            try:
                from src.ui.shutdown_screen import ShutdownScreen

                shutdown_message = self.settings_manager.get(
                    "shutdown_message",
                    "OFF",
                )

                shutdown_screen = ShutdownScreen(
                    self.display.width,
                    self.display.height,
                    shutdown_message,
                )

                shutdown_image = shutdown_screen.render()

                if self.display.hardware_available and self.display.epd:
                    self.display.epd.init()

                self.display.display_image(
                    shutdown_image,
                    use_partial=False,
                )

                time.sleep(6)

            except Exception as exc:
                self.logger.error(
                    "Failed to show shutdown screen: %s",
                    exc,
                    exc_info=True,
                )

            self.stop()
            os.system("sudo shutdown -h now")

        elif screen is None:
            self.logger.info(
                "App '%s' not yet implemented",
                name,
            )
            return False

        else:
            self.logger.warning(
                "Unknown menu app screen: %s",
                screen,
            )
            return False

        return True

    def _open_menu_app_direct(self, screen, source="WEB DIRECT"):
        """Open a main-menu application directly from another interface."""
        if not self.running:
            return False

        if not self._reset_activity():
            return False

        if self.power_manager.is_sleeping:
            self._wake_from_sleep()

        # Leaving Reader directly must preserve progress and restore the
        # resources normally restored by Menu/Back navigation.
        if self.navigation.is_on_screen(Screen.READER):
            self._suspend_reader_state()
            self._restore_all_cores()
            self._enable_wifi_after_reader()

        # Leaving Typewriter must also release raw keyboard handling.
        if self.navigation.is_on_screen(Screen.TYPEWRITER):
            if self.keyboard_handler:
                self.keyboard_handler.raw_key_callback = None

        app = next(
            (
                item
                for item in self.main_menu_screen.apps
                if item.get("screen") == screen
            ),
            None,
        )

        if app is None:
            self.logger.warning(
                "%s: unknown main-menu application '%s'",
                source,
                screen,
            )
            return False

        return self._launch_menu_app(
            app,
            source=source,
        )

    def _handle_gpio5_hold(self):
        """Handle GPIO5 long press - select app on main menu, return to menu elsewhere"""
        if not self.running: return

        if not self._reset_activity():
            return
        if self.power_manager.is_sleeping:
            self._wake_from_sleep()
            return

        if self.navigation.is_on_screen(Screen.MAIN_MENU):
            app = self.main_menu_screen.get_selected_app()
            self._launch_menu_app(app, source="GPIO5 HOLD")

        elif self.navigation.is_on_screen(Screen.LIBRARY):
            # On library - open selected book (same as select button)
            book = self.library_screen.get_selected_book()
            if book:
                # Check if Home icon is selected
                if book['path'] == '__home__':
                    self.logger.info("🏠 Action: GPIO5 HOLD - Returning to main menu from library")
                    self.navigation.navigate_to(Screen.MAIN_MENU)
                    self._render_current_screen()
                else:
                    self.logger.info(f"📖 Action: GPIO5 HOLD - Opening book '{book['title']}'")
                    self._open_book(book)
            else:
                self.logger.warning("No book selected to open")
        else:
            # On any other screen (IP Scanner, To-Do, Reader, Typewriter) - return to main menu
            self.logger.info("🏠 Action: GPIO5 HOLD - Returning to main menu")

            if self.navigation.is_on_screen(Screen.READER):
                self._suspend_reader_state()
                # Restore all CPU cores when leaving reader
                self._restore_all_cores()
                # Re-enable WiFi when leaving reader
                self._enable_wifi_after_reader()

            if self.navigation.is_on_screen(Screen.TYPEWRITER):
                # Disable raw key mode when leaving typewriter
                if self.keyboard_handler:
                    self.keyboard_handler.raw_key_callback = None

            self.navigation.navigate_to(Screen.MAIN_MENU)
            self._render_current_screen()

    def _handle_toggle(self):
        """Handle toggle button press (PiSugar long press)"""
        if not self.running: return

        if not self._reset_activity():
            return
        if self.power_manager.is_sleeping:
            self._wake_from_sleep()
            return

        if self.navigation.is_on_screen(Screen.MAIN_MENU):
            app = self.main_menu_screen.get_selected_app()
            self._launch_menu_app(app, source="TOGGLE")

        elif self.navigation.is_on_screen(Screen.LIBRARY):
            # On library - open selected book (same as select)
            book = self.library_screen.get_selected_book()
            if book:
                self.logger.info(
                    f"🔄 Action: TOGGLE - Opening book '{book['title']}' from library"
                )
                self._open_book(book)
            else:
                self.logger.warning("No book selected to open")
        elif self.navigation.is_on_screen(Screen.READER):
            self._suspend_reader_state()
            self._restore_all_cores()
            self._enable_wifi_after_reader()

            return_screen = getattr(
                self,
                "_reader_return_screen",
                Screen.LIBRARY,
            )

            self.logger.info(
                "🔄 Action: TOGGLE - Reader -> %s",
                getattr(return_screen, "name", str(return_screen)),
            )

            self.navigation.navigate_to(return_screen)
            self._render_current_screen()
        elif self.navigation.is_on_screen(Screen.WIFI):
            self.logger.info(
                "🏠 Action: TOGGLE - Returning to main menu from Wi-Fi"
            )
            self.navigation.navigate_to(Screen.MAIN_MENU)
            self._render_current_screen()
        elif self.navigation.is_on_screen(Screen.IP_SCANNER):
            self.logger.info("📶 Action: TOGGLE - Returning to Wi-Fi from IP Scanner")
            self._ensure_wifi_screen()
            self.wifi_screen.on_enter()
            self.navigation.navigate_to(Screen.WIFI)
            self._render_current_screen()
        elif self.navigation.is_on_screen(Screen.TODO):
            # On To Do - toggle completion status of current item
            self.logger.info("✓ Action: TOGGLE - Toggle todo completion")
            self._ensure_todo_screen().toggle_todo()
            self._render_current_screen()
        elif self.navigation.is_on_screen(Screen.KLIPPER):
            self.logger.info("📶 Action: TOGGLE - Returning to Wi-Fi from Klipper")
            self._ensure_wifi_screen()
            self.wifi_screen.on_enter()
            self.navigation.navigate_to(Screen.WIFI)
            self._render_current_screen()
        elif self.navigation.is_on_screen(Screen.TYPEWRITER):
            # On Typewriter - return to main menu and disable raw key mode
            self.logger.info("🏠 Action: TOGGLE - Returning to main menu from Typewriter")
            if self.keyboard_handler:
                self.keyboard_handler.raw_key_callback = None
            self.navigation.navigate_to(Screen.MAIN_MENU)
            self._render_current_screen()

    def _handle_typewriter_key(self, key_code: int, char: str, modifiers: dict):
        """Handle keyboard input for typewriter app - render every keystroke"""
        try:
            from evdev import ecodes

            # Alt key switches tabs
            if modifiers.get('alt'):
                self.typewriter_screen.toggle_mode()
                self._render_current_screen()
                return

            # Escape key returns to main menu
            if key_code == ecodes.KEY_ESC:
                self.logger.info("🏠 Action: ESC - Returning to main menu from Typewriter")
                if self.keyboard_handler:
                    self.keyboard_handler.raw_key_callback = None
                self.navigation.navigate_to(Screen.MAIN_MENU)
                self._render_current_screen()
                return

            # Pass key to typewriter screen and render immediately
            self.typewriter_screen.handle_key(key_code, char, modifiers)
            self._render_current_screen()

        except Exception as e:
            self.logger.error(f"Error handling typewriter key: {e}")

    def _suspend_reader_state(self):
        # Close the effective-reading interval before leaving the Reader.
        try:
            self._flush_reading_time(continue_session=False)
        except Exception as exc:
            self.logger.debug(
                "Could not flush reading time on Reader exit: %s",
                exc,
            )

        # Persist position but keep one Reader hot in RAM.
        if (
            self.reader_screen
            and self.reader_screen.renderer
            and self.reader_screen.current_book_path
        ):
            total = self.reader_screen.renderer.get_page_count()
            self.progress_manager.save_progress(
                self.reader_screen.current_book_path,
                self.reader_screen.current_page,
                total,
            )
            self.logger.info(
                "Reader suspended warm: %s page %d/%d",
                os.path.basename(self.reader_screen.current_book_path),
                self.reader_screen.current_page + 1,
                total,
            )
        else:
            self.logger.debug("Reader suspension requested with no active book")

    def _get_last_read_book(self):
        # Newest valid EPUB from persistent reading progress.
        progress = self.progress_manager.get_all_progress()
        candidates = sorted(
            progress.items(),
            key=lambda item: str(item[1].get("last_read", "")),
            reverse=True,
        )

        for path, data in candidates:
            absolute = os.path.abspath(path)
            if not os.path.isfile(absolute):
                continue

            for book in getattr(self.library_screen, "books", []):
                book_path = book.get("path")
                if not book_path or book_path == "__home__":
                    continue
                if os.path.abspath(book_path) == absolute:
                    return dict(book)

            return {
                "path": absolute,
                "title": Path(absolute).stem.replace("_", " "),
            }

        return None

    def _show_continue_notice(self, book: dict):
        """Show short PARTIAL feedback before resuming the last book."""
        from PIL import Image, ImageDraw, ImageFont

        try:
            saved_page = self.progress_manager.load_progress(book["path"])
        except Exception:
            saved_page = None

        page_number = (saved_page + 1) if saved_page is not None else 1
        title = str(book.get("title") or Path(book["path"]).stem)

        width = self.display.width
        height = self.display.height

        image = Image.new("1", (width, height), 1)
        draw = ImageDraw.Draw(image)

        try:
            title_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                30,
            )
            body_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                24,
            )
            page_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                26,
            )
        except Exception:
            title_font = ImageFont.load_default()
            body_font = ImageFont.load_default()
            page_font = ImageFont.load_default()

        def fit_text(text, font, max_width):
            value = text
            while value:
                box = draw.textbbox((0, 0), value, font=font)
                if box[2] - box[0] <= max_width:
                    return value
                if len(value) <= 4:
                    break
                value = value[:-4] + "..."
            return value

        heading = "A continuar"
        book_title = fit_text(title, body_font, width - 100)
        page_text = f"Página {page_number}"

        box = draw.textbbox((0, 0), heading, font=title_font)
        draw.text(
            ((width - (box[2] - box[0])) // 2, 145),
            heading,
            font=title_font,
            fill=0,
        )

        box = draw.textbbox((0, 0), book_title, font=body_font)
        draw.text(
            ((width - (box[2] - box[0])) // 2, 220),
            book_title,
            font=body_font,
            fill=0,
        )

        box = draw.textbbox((0, 0), page_text, font=page_font)
        draw.text(
            ((width - (box[2] - box[0])) // 2, 285),
            page_text,
            font=page_font,
            fill=0,
        )

        self.logger.info(
            "Continuar notice: %s page %d",
            title,
            page_number,
        )

        with self._render_lock:
            self.display.display_image(
                image,
                use_partial=True,
                enforce_partial_budget=False,
            )

        # Apenas tempo suficiente para a informação ser perceptível.
        time.sleep(0.8)

    def _continue_reading(self):
        book = self._get_last_read_book()
        if not book:
            self.logger.info(
                "Continuar: sem histórico válido; a abrir a biblioteca"
            )
            self.navigation.navigate_to(Screen.LIBRARY)
            self._render_current_screen()
            return

        self.logger.info("📖 Continuar: %s", book["title"])
        self._show_continue_notice(book)
        self._book_loading_visible_path = os.path.abspath(book["path"])
        self._open_book(book)

    def _show_book_loading(self, book: dict):
        # One static PARTIAL feedback frame for a genuinely cold EPUB open.
        from PIL import Image, ImageDraw, ImageFont

        width = self.display.width
        height = self.display.height
        image = Image.new("1", (width, height), 1)
        draw = ImageDraw.Draw(image)

        try:
            title_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                34,
            )
            body_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                20,
            )
        except OSError:
            title_font = ImageFont.load_default()
            body_font = ImageFont.load_default()

        heading = "A abrir livro…"
        box = draw.textbbox((0, 0), heading, font=title_font)
        draw.text(
            ((width - (box[2] - box[0])) // 2, 165),
            heading,
            font=title_font,
            fill=0,
        )

        book_title = str(book.get("title", ""))
        while book_title:
            box = draw.textbbox((0, 0), book_title, font=body_font)
            if box[2] - box[0] <= width - 100:
                break
            book_title = book_title[:-2].rstrip() + "…"

        if book_title:
            box = draw.textbbox((0, 0), book_title, font=body_font)
            draw.text(
                ((width - (box[2] - box[0])) // 2, 225),
                book_title,
                font=body_font,
                fill=0,
            )

        self.logger.info("Cold EPUB open - showing one PARTIAL loading frame")
        with self._render_lock:
            # Deliberately bypass the GPIO long-press FULL here.
            # The actual Reader screen change performs the single FULL.
            self.display.display_image(
                image,
                use_partial=True,
                enforce_partial_budget=False,
            )

    def _wait_for_prepared_book(self, book: dict, initial_status: dict):
        """Wait asynchronously for an existing background preparation."""
        target_path = os.path.abspath(book["path"])

        if self._waiting_for_book_path is not None:
            self.logger.info(
                "Book preparation wait already active; duplicate request ignored"
            )
            return

        self._waiting_for_book_path = target_path
        self._waiting_for_book_title = book.get("title")

        self.logger.info(
            "Waiting for existing EPUB preparation before opening: "
            "%s state=%s progress=%s%%",
            book.get("title"),
            initial_status.get("state"),
            initial_status.get("progress"),
        )

        # Keep whichever transition frame is already visible. Continuar
        # deliberately uses its own "A continuar" frame; Library uses the
        # generic "A abrir livro" frame.
        if self._book_loading_visible_path == target_path:
            self.logger.info(
                "Reusing existing book transition frame while waiting "
                "for EPUB preparation"
            )
        else:
            self._show_book_loading(book)
            self._book_loading_visible_path = target_path

        preparer = getattr(self, "epub_preparation", None)

        def worker():
            last_progress = None

            try:
                while (
                    self.running
                    and self._waiting_for_book_path == target_path
                ):
                    status = (
                        preparer.get_status(target_path)
                        if preparer is not None
                        else {
                            "state": "error",
                            "message": "Preparation manager unavailable",
                        }
                    )

                    state = status.get("state")
                    progress = status.get("progress")

                    if progress != last_progress:
                        last_progress = progress
                        self.logger.info(
                            "Waiting EPUB preparation: %s %s%% (%s)",
                            book.get("title"),
                            progress,
                            state,
                        )

                    if state == "ready":
                        self.logger.info(
                            "Prepared EPUB ready; opening from cache: %s",
                            book.get("title"),
                        )
                        self._waiting_for_book_path = None
                        self._waiting_for_book_title = None
                        self._open_book(book)
                        return

                    if state == "error":
                        self.logger.error(
                            "EPUB preparation failed while waiting: %s",
                            status.get("message", "unknown error"),
                        )
                        self._waiting_for_book_path = None
                        self._waiting_for_book_title = None
                        self._book_loading_visible_path = None
                        self._render_current_screen(force_partial=True)
                        return

                    time.sleep(0.25)

            except Exception:
                self.logger.exception(
                    "Failed while waiting for prepared EPUB"
                )
                if self._waiting_for_book_path == target_path:
                    self._waiting_for_book_path = None
                    self._waiting_for_book_title = None
                    self._book_loading_visible_path = None
                    try:
                        self._render_current_screen(force_partial=True)
                    except Exception:
                        pass

        threading.Thread(
            target=worker,
            name="PiBookEPUBOpenWait",
            daemon=True,
        ).start()

    def _prepare_loaded_book_reread(self) -> bool:
        """Start a new reading cycle for the EPUB currently loaded in Reader."""
        book_path = self.reader_screen.current_book_path
        renderer = self.reader_screen.renderer

        if not book_path or not renderer:
            return False

        total_pages = renderer.get_page_count()

        if not self.progress_manager.start_reread(
            book_path,
            total_pages,
        ):
            return False

        self.reader_screen.go_to_page(0)
        self.reader_screen.completion_mode = False
        self.reader_screen.completion_details = None
        self.reader_screen.completion_menu_index = 0
        self.reader_screen.completion_rating_editing = False
        self.reader_screen.completion_rating_value = 0

        return True

    def _open_book(self, book: dict):
        """Serialize EPUB opening; reuse preparation already in progress."""

        preparer = getattr(self, "epub_preparation", None)

        if preparer is not None:
            try:
                status = preparer.get_status(book["path"])
            except Exception:
                status = {}

            state = status.get("state")
            effective = getattr(self, "_effective_power_profile", None)

            # In normal battery/mains operation, never throw away background
            # preparation that is already underway. Powersave is excluded
            # because background preparation intentionally remains paused.
            if (
                state in {"pending", "preparing", "paused"}
                and effective != "powersave"
            ):
                self._wait_for_prepared_book(book, status)
                return

        if not self._book_open_lock.acquire(blocking=False):
            self.logger.info(
                "Book open already in progress; duplicate request ignored"
            )
            return

        try:
            return self._open_book_unlocked(book)
        finally:
            self._book_open_lock.release()

    def _open_book_unlocked(self, book: dict):
        # Open a cold EPUB or resume the already-hot Reader state.
        try:
            self.logger.info(f"Opening book: {book['title']}")

            # Memorizar de onde este livro foi aberto.
            # Biblioteca -> volta à Biblioteca.
            # Continuar / outras entradas -> volta ao Menu Principal.
            self._reader_return_screen = (
                Screen.LIBRARY
                if self.navigation.is_on_screen(Screen.LIBRARY)
                else Screen.MAIN_MENU
            )

            self.logger.info(
                "Reader return screen: %s",
                getattr(
                    self._reader_return_screen,
                    "name",
                    str(self._reader_return_screen),
                ),
            )

            target_path = os.path.abspath(book["path"])
            current_path = (
                os.path.abspath(self.reader_screen.current_book_path)
                if self.reader_screen.current_book_path
                else None
            )
            warm = bool(
                self.reader_screen.renderer
                and self.reader_screen.page_cache is not None
                and current_path == target_path
            )

            if warm:
                # A Continuar transition frame may already be visible.
                # Clear its guard before entering the warm Reader.
                self._book_loading_visible_path = None
                self.logger.info(
                    "Warm Reader resume: reusing renderer/layout/page cache"
                )
            else:
                loading_already_visible = (
                    self._book_loading_visible_path == target_path
                )
                self._book_loading_visible_path = None

                if loading_already_visible:
                    self.logger.info(
                        "Reusing existing book loading frame; "
                        "skipping redundant PARTIAL refresh"
                    )
                else:
                    self._show_book_loading(book)

                # Keep at most one hot Reader.
                if self.reader_screen.renderer:
                    self.logger.info(
                        "Closing previous warm Reader before loading another book"
                    )
                    self.reader_screen.close()

                def update_loading_progress(percent: float, message: str):
                    self.logger.debug(
                        "EPUB preparation %.0f%%: %s", percent, message
                    )

                self.reader_screen.load_epub(
                    book["path"],
                    progress_callback=update_loading_progress,
                )

                saved_page = self.progress_manager.load_progress(book["path"])
                if saved_page is not None and saved_page > 0:
                    self.reader_screen.go_to_page(saved_page)
                    self.logger.info(
                        f"📖 Restored to page {saved_page + 1}"
                    )

            if book.get("start_reread"):
                if not self._prepare_loaded_book_reread():
                    raise RuntimeError(
                        "Explicit re-reading request was rejected"
                    )

                self.logger.info(
                    "Explicit re-reading prepared before Reader entry"
                )

            progress_details = self.progress_manager.get_progress_details(
                book["path"]
            )
            book_finished = bool(
                progress_details
                and progress_details.get("status") == "finished"
            )

            # A finished book always opens on the generated completion page.
            # Re-reading must be started explicitly.
            self.reader_screen.completion_mode = book_finished
            self.reader_screen.completion_details = (
                progress_details if book_finished else None
            )

            # Reader networking is controlled by Power Profiles v2.
            self._reader_network_policy_state = None

            self._enable_single_core_mode()
            self.navigation.navigate_to(Screen.READER, {"book": book})

            self._apply_reader_network_policy(
                self._effective_power_profile or "battery"
            )

            # Exactly one final physical update; screen change => FULL.
            self._render_current_screen()

            info = self.reader_screen.get_page_info()

            if book_finished:
                self._reading_clock_last = None
                self.logger.info(
                    "Finished book opened on completion page"
                )
            else:
                self.progress_manager.mark_started(
                    self.reader_screen.current_book_path,
                    self.reader_screen.current_page,
                    info["total"],
                )
                self._reading_clock_last = time.monotonic()

            self.logger.info(
                "Book opened: %d pages (%s)",
                info["total"],
                "warm" if warm else "cold",
            )

        except Exception as e:
            self.logger.error(f"Failed to open book: {e}", exc_info=True)

            # Never leave the static loading frame stuck after an error.
            try:
                self._force_full_on_next_render = False
                self._render_current_screen(force_partial=True)
            except Exception:
                self.logger.exception(
                    "Failed to restore previous screen after book-open error"
                )

            # Stay on library screen

    def _apply_power_profile_async(self, profile):
        """Apply secondary system-level effects for the active profile."""
        if self._power_profile_apply_pending:
            return

        self._power_profile_apply_pending = True

        def worker():
            try:
                result = subprocess.run(
                    [
                        '/usr/bin/sudo',
                        '-n',
                        '/usr/local/sbin/pibook-power-profile',
                        profile,
                    ],
                    text=True,
                    capture_output=True,
                    timeout=20,
                    check=False,
                )

                if result.returncode == 0:
                    self.logger.info(
                        "System power profile applied: %s",
                        profile,
                    )
                else:
                    detail = (
                        result.stderr.strip()
                        or result.stdout.strip()
                        or f"code {result.returncode}"
                    )
                    self.logger.warning(
                        "System power profile %s failed: %s",
                        profile,
                        detail,
                    )
            except Exception as exc:
                self.logger.warning(
                    "System power profile failed: %s",
                    exc,
                )
            finally:
                self._power_profile_apply_pending = False

                latest = self._effective_power_profile
                if latest and latest != profile:
                    self._apply_power_profile_async(latest)

        threading.Thread(
            target=worker,
            name="PiBookPowerProfile",
            daemon=True,
        ).start()

    def _reader_prefetch_radius(self):
        """Return prefetch radius for the effective profile."""
        profile = self._effective_power_profile or 'battery'
        return reader_prefetch(self.settings, profile)

    def _request_reader_network_state(
        self,
        keep_network,
        profile,
    ):
        """Serialize Reader suspend/resume; latest request wins."""
        keep_network = bool(keep_network)

        with self._reader_network_lock:
            self._reader_network_desired = keep_network

            if self._reader_network_policy_state == keep_network:
                return

            if self._reader_network_worker_running:
                return

            self._reader_network_worker_running = True

        def worker():
            while True:
                with self._reader_network_lock:
                    target = self._reader_network_desired
                    current = self._reader_network_policy_state

                    if current == target:
                        self._reader_network_worker_running = False
                        return

                command = "resume" if target else "suspend"

                try:
                    result = subprocess.run(
                        [
                            "/usr/bin/sudo",
                            "-n",
                            "/usr/local/sbin/pibook-networkctl",
                            command,
                            "reader",
                        ],
                        text=True,
                        capture_output=True,
                        timeout=120,
                        check=False,
                    )
                except Exception as exc:
                    self.logger.warning(
                        "Reader network policy failed: %s",
                        exc,
                    )
                    with self._reader_network_lock:
                        self._reader_network_policy_state = None
                        self._reader_network_worker_running = False
                    return

                if result.returncode != 0:
                    detail = (
                        result.stderr.strip()
                        or result.stdout.strip()
                        or f"code {result.returncode}"
                    )
                    self.logger.warning(
                        "Reader network %s failed: %s",
                        command,
                        detail,
                    )
                    with self._reader_network_lock:
                        self._reader_network_policy_state = None
                        self._reader_network_worker_running = False
                    return

                with self._reader_network_lock:
                    self._reader_network_policy_state = target

                self.logger.info(
                    "Reader network policy: profile=%s network=%s",
                    profile,
                    "on" if target else "off",
                )

                # Loop again: the desired state may have changed while
                # suspend/resume was still running.

        threading.Thread(
            target=worker,
            name="PiBookReaderNetwork",
            daemon=True,
        ).start()

    def _apply_reader_network_policy(self, profile):
        """Resolve and request Reader networking for the active profile."""
        if not self.navigation.is_on_screen(Screen.READER):
            return

        keep_network = network_reading_enabled(
            self.settings,
            profile,
        )

        self._request_reader_network_state(
            keep_network,
            profile,
        )

    def _apply_sleep_profile_policy(self, profile):
        """Apply sleep policy for the effective energy profile."""
        enabled, timeout = sleep_policy(
            self.settings,
            profile,
        )

        previous_enabled = bool(
            self.power_manager.sleep_enabled
        )
        previous_timeout = int(
            self.power_manager.sleep_timeout
        )

        self.power_manager.sleep_enabled = enabled
        self.power_manager.sleep_timeout = timeout
        self.library_screen.sleep_enabled = enabled

        changed = (
            previous_enabled != enabled
            or previous_timeout != timeout
        )

        # When sleep becomes active, start its idle clock now rather
        # than inheriting inactivity from the previous profile.
        if enabled and changed:
            self.power_manager.reset_activity()

        if changed:
            self.logger.info(
                "Sleep policy: profile=%s enabled=%s timeout=%ss",
                profile,
                enabled,
                timeout,
            )

    def _update_power_profile(self, percentage, power_source):
        """Resolve and activate the effective energy profile."""
        previous = self._effective_power_profile

        desired = effective_profile(
            self.settings,
            power_source,
            percentage,
            previous,
        )

        if desired is None:
            return

        profile_changed = desired != previous

        if profile_changed:
            self._effective_power_profile = desired

        self._apply_sleep_profile_policy(desired)
        self._apply_reader_network_policy(desired)

        if not profile_changed:
            return

        self.logger.info(
            "Power profile transition: mode=%s effective=%s -> %s "
            "(battery=%s%% source=%s)",
            self.settings.get('power_mode', 'auto'),
            previous or 'unknown',
            desired,
            percentage,
            power_source,
        )

        self._apply_power_profile_async(desired)

    def _update_battery_display(self):
        """Refresh battery UI with a background-only strict PARTIAL.

        This path can never trigger a FULL refresh. Physical PARTIAL history
        is still counted so a later user-driven Reader render can perform
        the scheduled FULL when due.
        """
        if (
            not self.running
            or self.power_manager.is_sleeping
            or self._battery_shutdown_started
            or self._battery_warning_visible
            or self._battery_critical_visible
        ):
            return

        # While the static "opening book" frame is visible, the logical
        # navigation screen can still be Library. A background battery redraw
        # must never overwrite that loading frame.
        if (
            self._waiting_for_book_path is not None
            or self._book_loading_visible_path is not None
        ):
            self.logger.debug(
                "Battery redraw deferred while book loading is visible"
            )
            return

        with self._render_lock:
            current_screen = self.navigation.current_screen

            # Never let a battery event become the first display update.
            if getattr(self.display, "first_display", False):
                self.logger.debug(
                    "Battery redraw deferred before first display"
                )
                return

            # Do not interfere with a screen transition.
            if self.last_screen != current_screen:
                self.logger.debug(
                    "Battery redraw deferred during screen transition"
                )
                return

            # A FULL explicitly requested by user interaction remains pending.
            if self._force_full_on_next_render:
                self.logger.debug(
                    "Battery redraw deferred: user FULL pending"
                )
                return

            try:
                if self.navigation.is_on_screen(Screen.MAIN_MENU):
                    image = self.main_menu_screen.render()
                elif self.navigation.is_on_screen(Screen.LIBRARY):
                    image = self.library_screen.render()
                elif self.navigation.is_on_screen(Screen.WIFI):
                    image = self.wifi_screen.render()
                elif self.navigation.is_on_screen(Screen.IP_SCANNER):
                    image = self.ip_scanner_screen.render()
                elif self.navigation.is_on_screen(Screen.TODO):
                    image = self._ensure_todo_screen().render()
                elif self.navigation.is_on_screen(Screen.KLIPPER):
                    image = self.klipper_screen.render()
                elif self.navigation.is_on_screen(Screen.TYPEWRITER):
                    image = self.typewriter_screen.render()
                elif self.navigation.is_on_screen(Screen.READER):
                    # Cached EPUB content stays intact; battery overlay
                    # is drawn fresh by get_current_image().
                    image = self.reader_screen.get_current_image()
                else:
                    return

                before = self.display.partial_refresh_count

                self.display.display_image(
                    image,
                    use_partial=True,
                    enforce_partial_budget=False,
                    strict_partial=True,
                )

                after = self.display.partial_refresh_count

                self.logger.info(
                    "Battery UI background PARTIAL: physical count %d -> %d",
                    before,
                    after,
                )

            except Exception as exc:
                # Never repair a failed automatic battery refresh with FULL.
                self.logger.warning(
                    "Battery background PARTIAL failed/skipped; "
                    "FULL forbidden: %s",
                    exc,
                )

    def _render_wifi_if_dirty(self) -> bool:
        """Coalesce asynchronous Wi-Fi redraw requests."""
        with self._render_lock:
            if (
                not self.navigation.is_on_screen(Screen.WIFI)
                or self.power_manager.is_sleeping
                or not self.wifi_screen.needs_render()
            ):
                return False

            self._render_current_screen_locked(force_partial=True)
            return True

    def _render_current_screen(self, force_partial: bool = False):
        """Serialize every render and hardware refresh."""
        with self._render_lock:
            return self._render_current_screen_locked(force_partial)

    def _render_current_screen_locked(
        self,
        force_partial: bool = False,
    ):
        """Render the current screen to display

        Args:
            force_partial: Request partial refresh for same-screen updates;
                the display driver may promote it to FULL when due.
        """
        # A critical-battery screen has absolute visual priority.
        # Generic renders must never overwrite it while shutdown is pending.
        if self._battery_critical_visible or self._battery_shutdown_started:
            self.logger.debug(
                "Screen render blocked by critical-battery protection"
            )
            return False

        try:
            # Check if we changed screens - if so, do full refresh
            current_screen = self.navigation.current_screen
            screen_changed = (self.last_screen != current_screen)

            # Screen changes are genuine visual transitions and always request FULL.
            # force_partial is only meaningful for same-screen updates.
            force_full = self._force_full_on_next_render
            self._force_full_on_next_render = False
            is_reader = self.navigation.is_on_screen(Screen.READER)

            # Entering/changing screen is FULL. A GPIO long press can also
            # request one FULL. Same-screen menus stay PARTIAL without the
            # Reader's five-refresh limit.
            use_partial = not (screen_changed or force_full)

            completion_page = bool(
                is_reader
                and self.reader_screen.completion_mode
            )

            # Completion UI has its own refresh policy:
            # navigation/editing is strict PARTIAL; explicit actions request FULL.
            enforce_partial_budget = (
                is_reader and not completion_page
            )
            strict_partial = (
                completion_page and use_partial
            )

            if self.navigation.is_on_screen(Screen.MAIN_MENU):
                image = self.main_menu_screen.render()
            elif self.navigation.is_on_screen(Screen.LIBRARY):
                image = self.library_screen.render()
            elif self.navigation.is_on_screen(Screen.WIFI):
                image = self.wifi_screen.render()
            elif self.navigation.is_on_screen(Screen.IP_SCANNER):
                image = self.ip_scanner_screen.render()
            elif self.navigation.is_on_screen(Screen.TODO):
                image = self._ensure_todo_screen().render()
            elif self.navigation.is_on_screen(Screen.KLIPPER):
                image = self.klipper_screen.render()
            elif self.navigation.is_on_screen(Screen.TYPEWRITER):
                image = self.typewriter_screen.render()
            elif self.navigation.is_on_screen(Screen.READER):
                image = self.reader_screen.get_current_image()

                # Log page info
                info = self.reader_screen.get_page_info()
                self.logger.info(f"Page {info['current']} of {info['total']}")

                # Log cache stats periodically
                if 'cache_stats' in info:
                    stats = info['cache_stats']
                    self.logger.debug(f"Cache: {stats.get('hit_rate', 0):.1f}% hit rate")

            else:
                self.logger.warning(f"Unknown screen: {self.navigation.current_screen}")
                return

            # Update last screen tracker
            self.last_screen = current_screen

            # Reader keeps the 5-partial budget; menus intentionally bypass it.
            if screen_changed:
                self.logger.info("Screen changed - using full refresh")
            elif force_full:
                self.logger.info("Long-press action - using full refresh")
            self.display.display_image(
                image,
                use_partial=use_partial,
                enforce_partial_budget=enforce_partial_budget,
                strict_partial=strict_partial,
            )

            # Only after the visible e-paper refresh has completed, prepare
            # nearby Reader pages in RAM according to the active power profile.
            # Prefetch never touches the display.
            if is_reader:
                self.reader_screen.prefetch_around_async(
                    self._reader_prefetch_radius()
                )

        except Exception as e:
            self.logger.error(f"Render error: {e}", exc_info=True)

    def _trigger_gc_if_needed(self):
        """Trigger garbage collection periodically (for Pi Zero 2 W)"""
        self.page_turn_count += 1

        if self.page_turn_count % self.gc_threshold == 0:
            gc.collect()
            self.logger.debug(f"Garbage collection triggered (count: {self.page_turn_count})")


def main():
    """Main entry point"""
    # Determine config path
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
    else:
        config_path = os.environ.get(
            'PIBOOK_CONFIG',
            os.path.join(os.path.dirname(__file__), '../config/config.yaml')
        )

    # Ensure config exists
    if not os.path.exists(config_path):
        print(f"ERROR: Configuration file not found: {config_path}")
        print(f"Usage: python3 {sys.argv[0]} [config_path]")
        sys.exit(1)

    # Create and start application
    app = PiBookApp(config_path)
    app.start()


if __name__ == '__main__':
    main()
