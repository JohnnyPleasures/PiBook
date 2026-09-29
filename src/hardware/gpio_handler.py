"""
GPIO button handling with gpiozero.
Supports short press and long press detection.
PORTABILITY: 100% portable - GPIO layout identical on Pi 3B+ and Pi Zero 2 W
"""

import yaml
import logging
import time
import threading
import queue
from typing import Callable, Dict, Optional


class GPIOHandler:
    """
    Handle GPIO button inputs using gpiozero with short/long press detection
    """

    def __init__(self, config_path: str, long_press_duration: float = 0.5):
        """
        Initialize GPIO handler

        Args:
            config_path: Path to GPIO configuration YAML
            long_press_duration: Seconds to hold for long press (default 0.5s)
        """
        self.logger = logging.getLogger(__name__)
        self.callbacks: Dict[str, Callable] = {}
        self.long_press_callbacks: Dict[str, Callable] = {}
        self.buttons: Dict[str, Optional[object]] = {}
        self.long_press_duration = long_press_duration
        
        # Track physical button state independently from application work.
        self.press_start_time: Dict[str, Optional[float]] = {}
        self.long_press_triggered: Dict[str, bool] = {}
        self._watching_press: Dict[str, bool] = {}
        self._state_lock = threading.Lock()

        # GPIO detection must never block on e-paper rendering/navigation.
        self._event_queue = queue.Queue()
        self._stop_event = threading.Event()
        self._callback_thread = threading.Thread(
            target=self._callback_worker,
            daemon=True,
            name="pibook-gpio-callbacks",
        )
        self._callback_thread.start()

        # Load configuration
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)

        # Try to import gpiozero
        try:
            from gpiozero import Button
            self.Button = Button
            self.hardware_available = True
            self.logger.info("GPIO hardware available")
        except ImportError:
            self.logger.warning("gpiozero not available. Running in mock mode.")
            self.hardware_available = False
            self.Button = None

        # Setup buttons
        self._setup_buttons()

    def _setup_buttons(self):
        """Configure all buttons from config"""
        if not self.hardware_available:
            # Mock mode - create dummy button objects
            for button_name in self.config['buttons'].keys():
                self.buttons[button_name] = None
                self.press_start_time[button_name] = None
                self.long_press_triggered[button_name] = False
                self._watching_press[button_name] = False
            self.logger.info("Mock GPIO buttons configured")
            return

        for button_name, button_config in self.config['buttons'].items():
            pin = button_config['pin']
            pull_up = (button_config['pull'] == 'up')
            bounce_time = button_config.get('bounce_time', 0.2)

            try:
                self.buttons[button_name] = self.Button(
                    pin,
                    pull_up=pull_up,
                    bounce_time=bounce_time
                )
                self.press_start_time[button_name] = None
                self.long_press_triggered[button_name] = False
                self._watching_press[button_name] = False
                self.logger.info(f"Configured button '{button_name}' on GPIO {pin}")
            except Exception as e:
                self.logger.error(f"Failed to setup button '{button_name}': {e}")

    def register_callback(self, button_name: str, callback: Callable, long_press: bool = False):
        """
        Register a callback function for a button

        Args:
            button_name: Name of button (from config)
            callback: Function to call when button is pressed
            long_press: If True, callback is for long press; if False, for short press
        """
        if button_name not in self.buttons:
            raise ValueError(f"Unknown button: {button_name}")

        if long_press:
            self.long_press_callbacks[button_name] = callback
            self.logger.info(f"Registered LONG PRESS callback for button '{button_name}'")
        else:
            self.callbacks[button_name] = callback
            self.logger.info(f"Registered SHORT PRESS callback for button '{button_name}'")

        if not self.hardware_available:
            return

        button = self.buttons[button_name]
        if button:
            # gpiozero only announces the beginning of a press. A dedicated
            # watcher observes the physical state until release.
            button.when_pressed = lambda bn=button_name: self._on_button_press(bn)
            button.when_released = None

    def _on_button_press(self, button_name: str):
        """Start one non-blocking physical press watcher."""
        with self._state_lock:
            if self._watching_press.get(button_name, False):
                return

            self._watching_press[button_name] = True
            self.press_start_time[button_name] = time.monotonic()
            self.long_press_triggered[button_name] = False

        threading.Thread(
            target=self._watch_button_press,
            args=(button_name,),
            daemon=True,
            name=f"pibook-gpio-{button_name}",
        ).start()

    def _watch_button_press(self, button_name: str):
        """Observe the actual GPIO state until release."""
        button = self.buttons.get(button_name)
        if not button:
            return

        started = time.monotonic()
        long_triggered = False

        try:
            while not self._stop_event.is_set():
                if not button.is_pressed:
                    break

                elapsed = time.monotonic() - started

                if (
                    not long_triggered
                    and elapsed >= self.long_press_duration
                ):
                    long_triggered = True
                    self.long_press_triggered[button_name] = True
                    self.logger.info(
                        "🔘 GPIO Button '%s': LONG PRESS detected (%.2fs)",
                        button_name,
                        elapsed,
                    )
                    self._event_queue.put((button_name, True))

                time.sleep(0.01)

            duration = time.monotonic() - started

            if not long_triggered and not self._stop_event.is_set():
                self.logger.info(
                    "🔘 GPIO Button '%s': SHORT PRESS detected (%.2fs)",
                    button_name,
                    duration,
                )
                self._event_queue.put((button_name, False))

        finally:
            with self._state_lock:
                self.press_start_time[button_name] = None
                self.long_press_triggered[button_name] = False
                self._watching_press[button_name] = False

    def _callback_worker(self):
        """Run application callbacks outside gpiozero event handling."""
        while not self._stop_event.is_set():
            try:
                button_name, long_press = self._event_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            callbacks = (
                self.long_press_callbacks
                if long_press
                else self.callbacks
            )
            callback = callbacks.get(button_name)

            if callback:
                try:
                    callback()
                except Exception:
                    self.logger.exception(
                        "Error in GPIO callback '%s' (long=%s)",
                        button_name,
                        long_press,
                    )

            self._event_queue.task_done()

    def cleanup(self):
        """Clean up GPIO resources."""
        self._stop_event.set()

        if not self.hardware_available:
            self.logger.debug("Mock GPIO cleanup")
            return

        for button_name, button in self.buttons.items():
            if button:
                try:
                    button.close()
                except Exception as e:
                    self.logger.error(f"Error cleaning up button '{button_name}': {e}")

        self.logger.info("GPIO cleaned up")

    def trigger_button(self, button_name: str, long_press: bool = False):
        """
        Manually trigger a button callback (for testing/mock mode)

        Args:
            button_name: Name of button to trigger
            long_press: If True, trigger long press; if False, trigger short press
        """
        if long_press and button_name in self.long_press_callbacks:
            self.logger.info(f"Manually triggering LONG PRESS for '{button_name}'")
            self.long_press_callbacks[button_name]()
        elif not long_press and button_name in self.callbacks:
            self.logger.info(f"Manually triggering SHORT PRESS for '{button_name}'")
            self.callbacks[button_name]()
        else:
            self.logger.warning(f"No callback registered for '{button_name}' (long_press={long_press})")
