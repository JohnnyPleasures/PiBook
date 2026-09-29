"""
Reading progress manager for tracking page positions across sessions.
Stores progress in JSON file for persistence.
"""

import json
import os
import logging
from datetime import datetime
from typing import Optional, Dict
import threading


class ProgressManager:
    """
    Manage reading progress persistence for books
    """
    
    def __init__(self, progress_file: str = "data/reading_progress.json"):
        """
        Initialize progress manager
        
        Args:
            progress_file: Path to JSON file for storing progress
        """
        self.logger = logging.getLogger(__name__)
        self.progress_file = progress_file
        self.lock = threading.Lock()
        
        # Ensure data directory exists
        os.makedirs(os.path.dirname(progress_file), exist_ok=True)
        
        # Load existing progress
        self.progress_data = self._load_progress_file()
    
    def _load_progress_file(self) -> Dict:
        """Load progress data from JSON file"""
        if not os.path.exists(self.progress_file):
            self.logger.info(f"No existing progress file, creating new one")
            return {}
        
        try:
            with open(self.progress_file, 'r') as f:
                data = json.load(f)
                self.logger.info(f"Loaded progress for {len(data)} books")
                return data
        except Exception as e:
            self.logger.error(f"Failed to load progress file: {e}")
            return {}
    
    def _save_progress_file(self):
        """Save progress data to JSON file"""
        try:
            with open(self.progress_file, 'w') as f:
                json.dump(self.progress_data, f, indent=2)
            self.logger.debug(f"Saved progress to {self.progress_file}")
        except Exception as e:
            self.logger.error(f"Failed to save progress file: {e}")
    
    @staticmethod
    def _normalize_entry(entry):
        """Return lifecycle V2 fields while accepting legacy records."""
        data = dict(entry or {})

        data.setdefault('schema_version', 2)
        data.setdefault('status', 'reading')
        data.setdefault('rating', None)
        data.setdefault('times_finished', 0)
        data.setdefault('reading_history', [])
        data.setdefault('total_reading_seconds', 0.0)

        if 'current_read' not in data:
            data['current_read'] = (
                None if data['status'] == 'finished'
                else {
                    'started_at': None,
                    'reading_seconds': 0.0,
                }
            )

        return data

    def save_progress(self, book_path: str, current_page: int, total_pages: int):
        """
        Save reading progress for a book
        
        Args:
            book_path: Full path to the book file
            current_page: Current page number (0-indexed)
            total_pages: Total number of pages in book
        """
        with self.lock:
            # Normalize path
            book_path = os.path.abspath(book_path)
            
            # Preserve lifecycle/statistics fields while updating position.
            existing = self.progress_data.get(book_path)

            if existing is None:
                now = datetime.now().isoformat()
                data = {
                    'schema_version': 2,
                    'status': 'reading',
                    'rating': None,
                    'times_finished': 0,
                    'current_read': {
                        'started_at': now,
                        'reading_seconds': 0.0,
                    },
                    'reading_history': [],
                    'total_reading_seconds': 0.0,
                }
            else:
                data = self._normalize_entry(existing)

            data.update({
                'current_page': current_page,
                'total_pages': total_pages,
                'last_read': datetime.now().isoformat()
            })

            self.progress_data[book_path] = data
            
            # Save to file
            self._save_progress_file()
            
            self.logger.info(f"📖 Saved progress: {os.path.basename(book_path)} - Page {current_page + 1}/{total_pages}")
    
    def mark_started(self, book_path: str, current_page: int = 0, total_pages: int = 0):
        """Register the first real opening of a book."""
        with self.lock:
            book_path = os.path.abspath(book_path)
            now = datetime.now().isoformat()

            existing = self.progress_data.get(book_path)

            if existing is None:
                data = {
                    'schema_version': 2,
                    'current_page': int(current_page),
                    'total_pages': int(total_pages),
                    'last_read': now,
                    'status': 'reading',
                    'rating': None,
                    'times_finished': 0,
                    'current_read': {
                        'started_at': now,
                        'reading_seconds': 0.0,
                    },
                    'reading_history': [],
                    'total_reading_seconds': 0.0,
                }
            else:
                data = self._normalize_entry(existing)

                # Legacy reading: keep unknown historical start as None.
                # Finished books also remain finished until explicit reread.
                if data.get('status') == 'unread':
                    data['status'] = 'reading'
                    data['current_read'] = {
                        'started_at': now,
                        'reading_seconds': 0.0,
                    }

                data['last_read'] = now

                if total_pages:
                    data['total_pages'] = int(total_pages)

            self.progress_data[book_path] = data
            self._save_progress_file()

    def mark_finished(self, book_path: str, current_page: int, total_pages: int):
        """Complete the current reading cycle without losing history."""
        with self.lock:
            book_path = os.path.abspath(book_path)
            now = datetime.now().isoformat()

            data = self._normalize_entry(
                self.progress_data.get(book_path, {})
            )

            # Do not count the same completion twice.
            if data.get('status') == 'finished':
                return

            current = data.get('current_read') or {
                'started_at': None,
                'reading_seconds': 0.0,
            }

            history = list(data.get('reading_history') or [])
            history.append({
                'started_at': current.get('started_at'),
                'finished_at': now,
                'reading_seconds': float(
                    current.get('reading_seconds', 0.0) or 0.0
                ),
            })

            data.update({
                'current_page': int(current_page),
                'total_pages': int(total_pages),
                'last_read': now,
                'status': 'finished',
                'times_finished': int(
                    data.get('times_finished', 0) or 0
                ) + 1,
                'current_read': None,
                'reading_history': history,
            })

            self.progress_data[book_path] = data
            self._save_progress_file()

            self.logger.info(
                "Book finished: %s - read %d time(s)",
                os.path.basename(book_path),
                data['times_finished'],
            )

    def start_reread(self, book_path: str, total_pages: int) -> bool:
        """Start a new reading cycle for an already finished book."""
        with self.lock:
            book_path = os.path.abspath(book_path)

            existing = self.progress_data.get(book_path)
            if existing is None:
                return False

            data = self._normalize_entry(existing)

            if data.get('status') != 'finished':
                return False

            now = datetime.now().isoformat()

            data.update({
                'current_page': 0,
                'total_pages': int(total_pages),
                'last_read': now,
                'status': 'reading',
                'current_read': {
                    'started_at': now,
                    'reading_seconds': 0.0,
                },
            })

            self.progress_data[book_path] = data
            self._save_progress_file()

            self.logger.info(
                "Re-reading started: %s",
                os.path.basename(book_path),
            )

            return True

    def set_rating(self, book_path: str, rating: Optional[int]):
        """Set book rating to 0..10 half-star steps, or None to clear it."""
        if rating is not None:
            try:
                rating = int(rating)
            except (TypeError, ValueError):
                raise ValueError("rating must be 0..10 or None")

            if not 0 <= rating <= 10:
                raise ValueError("rating must be 0..10 or None")

        with self.lock:
            book_path = os.path.abspath(book_path)

            existing = self.progress_data.get(book_path)

            if existing is None:
                data = {
                    'schema_version': 2,
                    'current_page': 0,
                    'total_pages': 0,
                    'last_read': None,
                    'status': 'unread',
                    'rating': rating,
                    'times_finished': 0,
                    'current_read': None,
                    'reading_history': [],
                    'total_reading_seconds': 0.0,
                }
            else:
                data = self._normalize_entry(existing)
                data['rating'] = rating

            self.progress_data[book_path] = data
            self._save_progress_file()

    def add_reading_time(self, book_path: str, seconds: float):
        """Add effective reading time to the current reading cycle."""
        try:
            seconds = max(0.0, float(seconds))
        except (TypeError, ValueError):
            return

        if seconds <= 0:
            return

        with self.lock:
            book_path = os.path.abspath(book_path)
            existing = self.progress_data.get(book_path)

            if existing is None:
                return

            data = self._normalize_entry(existing)

            if data.get('status') != 'reading':
                return

            current = data.get('current_read') or {
                'started_at': None,
                'reading_seconds': 0.0,
            }

            current = dict(current)
            current['reading_seconds'] = (
                float(current.get('reading_seconds', 0.0) or 0.0)
                + seconds
            )

            data['current_read'] = current
            data['total_reading_seconds'] = (
                float(data.get('total_reading_seconds', 0.0) or 0.0)
                + seconds
            )

            self.progress_data[book_path] = data
            self._save_progress_file()

    def get_progress_details(self, book_path: str):
        """Return normalized lifecycle data for one book."""
        with self.lock:
            book_path = os.path.abspath(book_path)
            existing = self.progress_data.get(book_path)

            if existing is None:
                return None

            return self._normalize_entry(existing)

    def load_progress(self, book_path: str) -> Optional[int]:
        """
        Load saved progress for a book
        
        Args:
            book_path: Full path to the book file
            
        Returns:
            Last page number (0-indexed) or None if no progress saved
        """
        with self.lock:
            # Normalize path
            book_path = os.path.abspath(book_path)
            
            if book_path in self.progress_data:
                progress = self.progress_data[book_path]
                page = progress['current_page']
                total = progress['total_pages']
                self.logger.info(f"📚 Restored progress: {os.path.basename(book_path)} - Page {page + 1}/{total}")
                return page
            else:
                self.logger.debug(f"No saved progress for {os.path.basename(book_path)}")
                return None
    
    def reset_position(self, book_path: str) -> bool:
        """Reset an active reading position to page 1 without losing lifecycle data."""
        with self.lock:
            book_path = os.path.abspath(book_path)
            existing = self.progress_data.get(book_path)

            if existing is None:
                return False

            data = self._normalize_entry(existing)

            # Finished books must use explicit re-reading instead.
            if data.get('status') == 'finished':
                return False

            data['current_page'] = 0

            self.progress_data[book_path] = data
            self._save_progress_file()

            self.logger.info(
                "Reading position reset to page 1: %s",
                os.path.basename(book_path),
            )

            return True

    def reset_all_positions(self):
        """Reset only active reading positions; preserve completed books/history."""
        with self.lock:
            reset_count = 0
            skipped_finished = 0

            for book_path, existing in list(self.progress_data.items()):
                data = self._normalize_entry(existing)

                if data.get('status') == 'finished':
                    skipped_finished += 1
                    continue

                if int(data.get('current_page', 0) or 0) != 0:
                    data['current_page'] = 0
                    self.progress_data[book_path] = data
                    reset_count += 1

            if reset_count:
                self._save_progress_file()

            self.logger.info(
                "Reading positions reset: %d; finished books preserved: %d",
                reset_count,
                skipped_finished,
            )

            return {
                'reset': reset_count,
                'skipped_finished': skipped_finished,
            }

    def clear_progress(self, book_path: str):
        """
        Clear saved progress for a book
        
        Args:
            book_path: Full path to the book file
        """
        with self.lock:
            # Normalize path
            book_path = os.path.abspath(book_path)
            
            if book_path in self.progress_data:
                del self.progress_data[book_path]
                self._save_progress_file()
                self.logger.info(f"Cleared progress for {os.path.basename(book_path)}")
    
    def get_all_progress(self) -> Dict:
        """
        Get all saved progress data
        
        Returns:
            Dictionary of all book progress
        """
        with self.lock:
            return self.progress_data.copy()
    
    def clear_all_progress(self):
        """Clear all saved progress"""
        with self.lock:
            self.progress_data = {}
            self._save_progress_file()
            self.logger.info("Cleared all reading progress")
