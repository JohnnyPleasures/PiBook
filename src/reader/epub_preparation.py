"""Background EPUB preparation for PiBook.

A single low-priority worker prepares persistent EPUB layout caches without
blocking uploads or the reader UI.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from src.reader.layout_cache import (
    cache_is_fresh as layout_cache_is_fresh,
    cache_path as layout_cache_path,
)


class _PreparationDeferred(Exception):
    """Preparation should yield to foreground work and be retried later."""


class EPUBPreparationManager:
    """Serialize EPUB preparation in one background worker."""

    def __init__(
        self,
        *,
        width: int,
        height: int,
        zoom_factor: float = 1.0,
        should_pause: Optional[Callable[[], bool]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.width = int(width)
        self.height = int(height)
        self.zoom_factor = float(zoom_factor)
        self.should_pause = should_pause
        self.logger = logger or logging.getLogger(__name__)

        self._queue: queue.Queue[str] = queue.Queue()
        self._queued: set[str] = set()
        self._status: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()

        self._thread = threading.Thread(
            target=self._worker,
            name="PiBookEPUBPreparation",
            daemon=True,
        )
        self._thread.start()

    def _cache_path(self, epub_path: str) -> Path:
        return layout_cache_path(
            epub_path,
            self.width,
            self.height,
            self.zoom_factor,
        )

    def _cache_is_fresh(self, epub_path: str) -> bool:
        return layout_cache_is_fresh(
            epub_path,
            self.width,
            self.height,
            self.zoom_factor,
        )

    def enqueue(self, epub_path: str) -> str:
        path = str(Path(epub_path).resolve())

        if not Path(path).is_file():
            raise FileNotFoundError(path)
        if Path(path).suffix.lower() != ".epub":
            raise ValueError("Only EPUB files can be prepared")

        with self._lock:
            if self._cache_is_fresh(path):
                self._status[path] = {
                    "state": "ready",
                    "progress": 100,
                    "message": "Layout cache already prepared",
                    "updated_at": time.time(),
                }
                return "ready"

            current = self._status.get(path, {}).get("state")
            if path in self._queued or current == "preparing":
                return current or "pending"

            self._queued.add(path)
            self._status[path] = {
                "state": "pending",
                "progress": 0,
                "message": "Waiting for background preparation",
                "updated_at": time.time(),
            }
            self._queue.put(path)
            self.logger.info(
                "EPUB queued for background preparation: %s",
                Path(path).name,
            )
            return "pending"

    def get_status(self, epub_path: str) -> dict:
        path = str(Path(epub_path).resolve())
        with self._lock:
            status = self._status.get(path)
            if status:
                return dict(status)

        if self._cache_is_fresh(path):
            return {
                "state": "ready",
                "progress": 100,
                "message": "Layout cache already prepared",
            }

        return {
            "state": "not_prepared",
            "progress": 0,
            "message": "Not prepared",
        }

    def _set_status(
        self,
        path: str,
        state: str,
        progress: int,
        message: str,
    ) -> None:
        with self._lock:
            self._status[path] = {
                "state": state,
                "progress": max(0, min(100, int(progress))),
                "message": str(message),
                "updated_at": time.time(),
            }

    def _wait_until_allowed(self, path: str) -> bool:
        while not self._stop_event.is_set():
            paused = False
            if self.should_pause is not None:
                try:
                    paused = bool(self.should_pause())
                except Exception as exc:
                    self.logger.debug(
                        "EPUB preparation pause check failed: %s",
                        exc,
                    )

            if not paused:
                return True

            self._set_status(
                path,
                "paused",
                self.get_status(path).get("progress", 0),
                "Paused while PiBook is busy or saving power",
            )
            self._stop_event.wait(2.0)

        return False

    def _prepare_one(self, path: str) -> None:
        if self._cache_is_fresh(path):
            self._set_status(
                path,
                "ready",
                100,
                "Layout cache already prepared",
            )
            return

        if not self._wait_until_allowed(path):
            return

        self._set_status(path, "preparing", 1, "Starting preparation")

        def progress(percent: float, message: str) -> None:
            if self.should_pause is not None:
                try:
                    if self.should_pause():
                        raise _PreparationDeferred()
                except _PreparationDeferred:
                    raise
                except Exception as exc:
                    self.logger.debug(
                        "EPUB preparation pause check failed: %s",
                        exc,
                    )

            self._set_status(
                path,
                "preparing",
                int(percent),
                message,
            )

        renderer = None
        try:
            from src.reader.pillow_text_renderer import PillowTextRenderer

            started = time.perf_counter()
            renderer = PillowTextRenderer(
                path,
                width=self.width,
                height=self.height,
                zoom_factor=self.zoom_factor,
                progress_callback=progress,
            )
            elapsed = time.perf_counter() - started

            self._set_status(
                path,
                "ready",
                100,
                f"Prepared in {elapsed:.1f}s",
            )
            self.logger.info(
                "Background EPUB preparation complete: %s in %.2f s",
                Path(path).name,
                elapsed,
            )
        except _PreparationDeferred:
            self._set_status(
                path,
                "paused",
                self.get_status(path).get("progress", 0),
                "Yielding to foreground work or power saving",
            )
            self.logger.info(
                "Background EPUB preparation yielded: %s",
                Path(path).name,
            )
            return True
        except Exception as exc:
            self._set_status(
                path,
                "error",
                0,
                str(exc),
            )
            self.logger.error(
                "Background EPUB preparation failed for %s: %s",
                Path(path).name,
                exc,
                exc_info=True,
            )
        finally:
            if renderer is not None:
                try:
                    renderer.close()
                except Exception:
                    pass

    def _worker(self) -> None:
        # Keep EPUB pagination genuinely lower priority than the foreground UI.
        # On Linux, setpriority(PRIO_PROCESS, native_tid, ...) applies to this
        # worker thread only, leaving the main PiBook thread at nice=0.
        try:
            native_tid = threading.get_native_id()
            os.setpriority(os.PRIO_PROCESS, native_tid, 10)
            actual_nice = os.getpriority(os.PRIO_PROCESS, native_tid)
            self.logger.info(
                "EPUB preparation worker priority set: tid=%s nice=%s",
                native_tid,
                actual_nice,
            )
        except Exception as exc:
            # Priority tuning is an optimisation only; preparation must still
            # work normally if the platform refuses the request.
            self.logger.warning(
                "Could not lower EPUB preparation worker priority: %s",
                exc,
            )

        while not self._stop_event.is_set():
            try:
                path = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            deferred = False
            try:
                deferred = bool(self._prepare_one(path))
            finally:
                with self._lock:
                    self._queued.discard(path)
                self._queue.task_done()

            if deferred and not self._stop_event.is_set():
                self._stop_event.wait(2.0)
                if not self._stop_event.is_set():
                    self.enqueue(path)

    def stop(self) -> None:
        self._stop_event.set()
