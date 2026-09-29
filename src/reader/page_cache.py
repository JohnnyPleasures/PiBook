"""
LRU cache for rendered page images.
Optimized for Pi Zero 2 W's limited memory.
PORTABILITY: 100% portable between Pi 3B+ and Pi Zero 2 W
"""

from collections import OrderedDict
from PIL import Image
import logging
import gc
import threading


class PageCache:
    """
    LRU (Least Recently Used) cache for page images
    """

    def __init__(self, max_size: int = 5):
        """
        Initialize page cache

        Args:
            max_size: Maximum number of pages to cache
        """
        self.logger = logging.getLogger(__name__)
        self.max_size = max_size
        self.cache: OrderedDict[int, Image.Image] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self._lock = threading.RLock()

    def get(self, page_num: int) -> Image.Image | None:
        """
        Get a page from cache

        Args:
            page_num: Page number

        Returns:
            Cached PIL Image or None if not in cache
        """
        with self._lock:
            if page_num in self.cache:
                # Move to end (most recently used)
                self.cache.move_to_end(page_num)
                self.hits += 1
                self.logger.debug(f"Cache hit: page {page_num}")
                return self.cache[page_num]

            self.misses += 1
            self.logger.debug(f"Cache miss: page {page_num}")
            return None

    def put(self, page_num: int, image: Image.Image):
        """
        Add a page to cache

        Args:
            page_num: Page number
            image: PIL Image to cache
        """
        with self._lock:
            # If page already in cache, move to end
            if page_num in self.cache:
                self.cache.move_to_end(page_num)
                self.cache[page_num] = image
                self.logger.debug(f"Updated cache: page {page_num}")
                return

            # If cache is full, remove oldest (first) item
            if len(self.cache) >= self.max_size:
                oldest_page = next(iter(self.cache))
                del self.cache[oldest_page]
                self.logger.debug(f"Evicted page {oldest_page} from cache")

                # Suggest garbage collection on memory-constrained devices
                # GC explícito removido: uma única evicção não justifica full GC.

            # Add new page
            self.cache[page_num] = image
            self.logger.debug(
                f"Cached page {page_num} (cache size: {len(self.cache)})"
            )

    def retain_only(self, page_numbers):
        """Keep only pages belonging to the requested Reader window."""
        wanted = set(page_numbers)

        with self._lock:
            removed = [
                page_num
                for page_num in list(self.cache.keys())
                if page_num not in wanted
            ]

            for page_num in removed:
                del self.cache[page_num]

        if removed:
            self.logger.debug(
                "Removed %d cached page(s) outside active Reader window",
                len(removed),
            )

    def resize(self, max_size: int):
        """Resize the cache while preserving the most recently used pages."""
        new_size = max(1, int(max_size))

        with self._lock:
            self.max_size = new_size

            while len(self.cache) > self.max_size:
                oldest_page = next(iter(self.cache))
                del self.cache[oldest_page]
                self.logger.debug(
                    f"Evicted page {oldest_page} while resizing cache"
                )

        self.logger.debug(f"Page cache resized to {new_size}")

    def contains(self, page_num: int) -> bool:
        """Check cache membership without changing hit/miss statistics."""
        with self._lock:
            return page_num in self.cache

    def clear(self):
        """Clear all cached pages"""
        with self._lock:
            self.cache.clear()
        self.logger.info("Page cache cleared")
        gc.collect()

    def get_stats(self) -> dict:
        """
        Get cache statistics

        Returns:
            Dictionary with hit/miss counts and hit rate
        """
        with self._lock:
            total = self.hits + self.misses
            hit_rate = (self.hits / total * 100) if total > 0 else 0

            return {
                'hits': self.hits,
                'misses': self.misses,
                'total_requests': total,
                'hit_rate': hit_rate,
                'cache_size': len(self.cache),
                'max_size': self.max_size
            }

    def reset_stats(self):
        """Reset hit/miss statistics"""
        with self._lock:
            self.hits = 0
            self.misses = 0
        self.logger.debug("Cache statistics reset")

    def __len__(self) -> int:
        """Get current cache size"""
        with self._lock:
            return len(self.cache)
