"""
Flask web server for PiBook remote control and file management.
Provides web interface for:
- Uploading/managing EPUB files
- Remote navigation (next/prev/select buttons)
- Book selection
"""

from flask import Flask, render_template, request, jsonify, redirect, url_for, Response
import os
import logging
import json
import platform
import shutil
import subprocess
import threading
import time
from werkzeug.utils import secure_filename
from pathlib import Path
from src.core.settings import SettingsManager
from io import BytesIO

from src.utils.epub_metadata import get_epub_title


_SCREEN_LABELS = {
    'main_menu': 'Menu principal',
    'library': 'Biblioteca',
    'reader': 'Leitor',
    'wifi': 'Wi-Fi',
    'ip_scanner': 'IP Scanner',
    'todo': 'To Do',
    'klipper': 'Klipper',
    'typewriter': 'Terminal',
}


def _screen_label(value):
    """Human-readable name for an internal PiBook screen identifier."""
    if value is None:
        return 'Desconhecido'

    raw = getattr(value, 'value', value)
    raw = str(raw).strip()

    if not raw or raw == 'unknown':
        return 'Desconhecido'

    return _SCREEN_LABELS.get(
        raw,
        raw.replace('_', ' ').capitalize(),
    )


class PiBookWebServer:
    """
    Web server for remote control and file management
    """

    def __init__(self, books_dir: str, app_instance, port: int = 5000, version: str = "v1.0"):
        """
        Initialize web server

        Args:
            books_dir: Path to books directory
            app_instance: PiBookApp instance for remote control
            port: Port to run server on
            version: PiBook version string
        """
        self.logger = logging.getLogger(__name__)
        self.books_dir = books_dir
        self.app_instance = app_instance
        self.port = port
        self.version = version
        self.project_dir = Path(__file__).resolve().parents[2]
        self.settings_path = self.project_dir / "settings.json"
        self.service_name = "pibook-zero.service"
        self._action_lock = threading.RLock()
        # Serialize settings application. Reader reflow can take tens of
        # seconds on Pi Zero W and must never overlap another save.
        self._settings_lock = threading.Lock()
        
        # Configure Flask with template and static folders
        template_dir = os.path.join(os.path.dirname(__file__), 'templates')
        static_dir = os.path.join(os.path.dirname(__file__), 'static')
        self.flask_app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
        self.flask_app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max file size

        # Initialize To-Do module
        from src.apps.todo.manager import TodoManager
        from src.apps.todo.routes import todo_bp, init_routes
        self.todo_manager = TodoManager(app_instance=app_instance)
        init_routes(self.todo_manager)
        self.flask_app.register_blueprint(todo_bp)
        self.logger.info("Registered To-Do Blueprint")

        self._setup_routes()

        # Register the permanent Wi-Fi/network API after the existing routes.
        from .network_api import register_network_routes
        register_network_routes(self)

    def _epub_cache_files(self, epub_path):
        """Return persistent layout caches belonging to one EPUB."""
        source = Path(epub_path)
        prefix = source.name + "."

        try:
            return [
                entry
                for entry in source.parent.iterdir()
                if entry.is_file()
                and entry.name.startswith(prefix)
                and entry.name.endswith(".cache")
            ]
        except OSError:
            return []

    def _epub_temp_cache_files(self, epub_path):
        """Return abandoned private cache temp files belonging to one EPUB."""
        source = Path(epub_path)
        prefix = source.name + "."

        try:
            return [
                entry
                for entry in source.parent.iterdir()
                if entry.is_file()
                and entry.name.startswith(prefix)
                and ".cache." in entry.name
                and entry.name.endswith(".tmp")
            ]
        except OSError:
            return []

    def _remove_epub_caches(self, epub_path):
        """Remove all final/stale layout cache files for one EPUB."""
        removed = []

        for cache_file in (
            self._epub_cache_files(epub_path)
            + self._epub_temp_cache_files(epub_path)
        ):
            try:
                cache_file.unlink()
                removed.append(cache_file.name)
            except FileNotFoundError:
                pass

        return removed

    def _move_epub_caches(self, old_epub_path, new_epub_path):
        """Move compatible persistent caches when an EPUB is renamed."""
        old_source = Path(old_epub_path)
        new_source = Path(new_epub_path)
        moved = []

        # Stale temporary files are never worth carrying forward.
        for temp_file in self._epub_temp_cache_files(old_source):
            try:
                temp_file.unlink()
            except FileNotFoundError:
                pass

        for cache_file in self._epub_cache_files(old_source):
            suffix = cache_file.name[len(old_source.name):]
            destination = new_source.parent / (new_source.name + suffix)

            # A stale orphan for the destination must never win.
            if destination.exists():
                destination.unlink()

            cache_file.rename(destination)
            moved.append(destination.name)

        return moved

    def _epub_file_change_blocked(self, epub_path):
        """True while background preparation still owns this EPUB."""
        preparer = getattr(
            self.app_instance,
            "epub_preparation",
            None,
        )
        if preparer is None:
            return False, None

        try:
            status = preparer.get_status(str(epub_path))
        except Exception:
            return False, None

        state = status.get("state")
        return state in {"pending", "preparing", "paused"}, status

    def _setup_routes(self):
        """Setup Flask routes"""

        @self.flask_app.route('/')
        def index():
            """Main page with file manager and controls"""
            settings_data = self._load_settings('settings.json')
            return render_template('base.html', books=self._get_books(), settings=settings_data, version=self.version)

        @self.flask_app.route('/upload', methods=['POST'])
        def upload():
            """Upload one or more EPUB files."""
            if 'file' not in request.files:
                return jsonify({'success': False, 'error': 'No file uploaded'}), 400

            files = request.files.getlist('file')
            uploaded = []
            errors = []
            books_path = Path(self.books_dir)
            books_path.mkdir(parents=True, exist_ok=True)

            for file in files:
                original_name = file.filename or ''
                if not original_name:
                    continue
                if not original_name.lower().endswith('.epub'):
                    errors.append(f'{original_name}: only EPUB files are supported')
                    continue

                filename = secure_filename(original_name)
                if not filename:
                    errors.append(f'{original_name}: invalid filename')
                    continue

                destination = books_path / filename
                if destination.exists():
                    stem = destination.stem
                    suffix = destination.suffix
                    counter = 2
                    while destination.exists():
                        destination = books_path / f"{stem} ({counter}){suffix}"
                        counter += 1

                file.save(destination)
                uploaded.append(destination.name)
                self.logger.info("Uploaded: %s", destination.name)

                # Prepare the EPUB asynchronously so the upload request
                # remains fast and the Reader can later reuse the cache.
                try:
                    preparer = getattr(
                        self.app_instance,
                        'epub_preparation',
                        None,
                    )
                    if preparer is not None:
                        state = preparer.enqueue(str(destination))
                        self.logger.info(
                            "EPUB preparation state after upload: %s -> %s",
                            destination.name,
                            state,
                        )
                except Exception as exc:
                    # Upload remains successful even if preparation cannot
                    # be queued; cold-open remains a valid fallback.
                    self.logger.warning(
                        "Could not queue EPUB preparation for %s: %s",
                        destination.name,
                        exc,
                    )

            if uploaded:
                self._reload_library()

            status = 200 if uploaded else 400
            return jsonify({
                'success': bool(uploaded),
                'count': len(uploaded),
                'uploaded': uploaded,
                'errors': errors,
                'error': None if uploaded else (errors[0] if errors else 'No valid EPUB files selected')
            }), status

        @self.flask_app.route('/api/epub/preparation')
        def epub_preparation_list():
            """Return preparation status for all EPUBs in one lightweight call."""
            preparer = getattr(
                self.app_instance,
                'epub_preparation',
                None,
            )

            if preparer is None:
                return jsonify({
                    'success': False,
                    'error': 'EPUB preparation manager unavailable',
                }), 503

            books_root = Path(self.books_dir).resolve()
            result = {}

            try:
                epub_files = sorted(
                    (
                        entry
                        for entry in books_root.iterdir()
                        if entry.is_file()
                        and entry.suffix.lower() == '.epub'
                    ),
                    key=lambda entry: entry.name.lower(),
                )
            except OSError as exc:
                return jsonify({
                    'success': False,
                    'error': str(exc),
                }), 500

            for filepath in epub_files:
                try:
                    result[filepath.name] = preparer.get_status(str(filepath))
                except Exception as exc:
                    result[filepath.name] = {
                        'state': 'error',
                        'progress': 0,
                        'message': str(exc),
                    }

            return jsonify({
                'success': True,
                'books': result,
            })

        @self.flask_app.route('/api/epub/preparation/<path:filename>')
        def epub_preparation_status(filename):
            """Return background preparation status for one EPUB."""
            safe_name = Path(filename).name
            filepath = (Path(self.books_dir) / safe_name).resolve()
            books_root = Path(self.books_dir).resolve()

            if (
                not safe_name
                or filename != safe_name
                or filepath.parent != books_root
                or filepath.suffix.lower() != '.epub'
            ):
                return jsonify({
                    'success': False,
                    'error': 'Invalid filename',
                }), 400

            if not filepath.exists():
                return jsonify({
                    'success': False,
                    'error': 'Book not found',
                }), 404

            preparer = getattr(
                self.app_instance,
                'epub_preparation',
                None,
            )

            if preparer is None:
                return jsonify({
                    'success': False,
                    'error': 'EPUB preparation manager unavailable',
                }), 503

            status = preparer.get_status(str(filepath))

            return jsonify({
                'success': True,
                'filename': safe_name,
                **status,
            })

        @self.flask_app.route('/delete/<path:filename>', methods=['POST', 'GET'])
        def delete(filename):
            """Delete an EPUB file safely."""
            safe_name = Path(filename).name
            filepath = (Path(self.books_dir) / safe_name).resolve()
            books_root = Path(self.books_dir).resolve()

            if (
                not safe_name
                or filename != safe_name
                or filepath.parent != books_root
                or filepath.suffix.lower() != '.epub'
            ):
                return jsonify({'success': False, 'error': 'Invalid filename'}), 400
            if not filepath.exists():
                return jsonify({'success': False, 'error': 'Book not found'}), 404

            current_book = getattr(self.app_instance.reader_screen, 'current_book_path', None)
            if current_book and Path(current_book).resolve() == filepath:
                return jsonify({
                    'success': False,
                    'error': 'Close the book on the reader before deleting it'
                }), 409

            blocked, prep_status = self._epub_file_change_blocked(filepath)
            if blocked:
                return jsonify({
                    'success': False,
                    'error': 'Wait for EPUB preparation to finish before deleting it',
                    'state': prep_status.get('state'),
                    'progress': prep_status.get('progress'),
                }), 409

            filepath.unlink()
            removed_caches = self._remove_epub_caches(filepath)
            self.logger.info(
                "Deleted: %s (removed %d layout cache files)",
                safe_name,
                len(removed_caches),
            )

            try:
                self.app_instance.progress_manager.clear_progress(str(filepath))
            except Exception:
                pass

            self._reload_library()

            if request.method == 'POST' or request.accept_mimetypes.accept_json:
                return jsonify({
                    'success': True,
                    'filename': safe_name,
                    'removed_caches': len(removed_caches),
                })
            return redirect(url_for('index'))

        @self.flask_app.route('/api/progress/list')
        def list_progress():
            """List all saved reading positions"""
            try:
                if not hasattr(self.app_instance, 'progress_manager'):
                    return jsonify({'error': 'Progress manager not available'}), 500

                all_progress = self.app_instance.progress_manager.get_all_progress()
                result = []
                for book_path, progress in all_progress.items():
                    result.append({
                        'path': book_path,
                        'filename': os.path.basename(book_path),
                        'title': get_epub_title(book_path),
                        'current_page': progress['current_page'] + 1,  # 1-indexed for display
                        'total_pages': progress['total_pages'],
                        'last_read': progress.get('last_read', 'Unknown')
                    })
                # Sort by the title shown to the user.
                result.sort(key=lambda x: x['title'].lower())
                return jsonify({'progress': result})
            except Exception as e:
                self.logger.error(f"Error listing progress: {e}")
                return jsonify({'error': str(e)}), 500

        @self.flask_app.route('/api/progress/reset', methods=['POST'])
        def reset_progress():
            """Reset reading position for a book or all books"""
            try:
                if not hasattr(self.app_instance, 'progress_manager'):
                    return jsonify({'error': 'Progress manager not available'}), 500

                data = request.get_json() or {}
                book_path = data.get('path')

                with self._action_lock:
                    if book_path == '__all__':
                        result = (
                            self.app_instance.progress_manager
                            .reset_all_positions()
                        )

                        # Keep the hot Reader consistent with persisted position.
                        reader = getattr(
                            self.app_instance,
                            'reader_screen',
                            None,
                        )
                        if (
                            reader
                            and reader.current_book_path
                            and reader.renderer
                        ):
                            details = (
                                self.app_instance.progress_manager
                                .get_progress_details(
                                    reader.current_book_path
                                )
                            )
                            if (
                                details
                                and details.get('status') != 'finished'
                            ):
                                reader.go_to_page(
                                    int(details.get('current_page', 0) or 0)
                                )

                        self.logger.info(
                            "Reset active reading positions via web: %s",
                            result,
                        )
                        return jsonify({
                            'status': 'success',
                            'message': (
                                f"{result['reset']} posição(ões) reposta(s); "
                                f"{result['skipped_finished']} livro(s) concluído(s) preservado(s)"
                            ),
                            'result': result,
                        })

                    elif book_path:
                        reset = (
                            self.app_instance.progress_manager
                            .reset_position(book_path)
                        )

                        if not reset:
                            details = (
                                self.app_instance.progress_manager
                                .get_progress_details(book_path)
                            )

                            if (
                                details
                                and details.get('status') == 'finished'
                            ):
                                return jsonify({
                                    'error': (
                                        'Livro concluído: use Reler em vez de '
                                        'Repor posição'
                                    )
                                }), 409

                            return jsonify({
                                'error': 'Não existe uma leitura ativa para repor'
                            }), 404

                        reader = getattr(
                            self.app_instance,
                            'reader_screen',
                            None,
                        )

                        if (
                            reader
                            and reader.current_book_path
                            and os.path.abspath(reader.current_book_path)
                            == os.path.abspath(book_path)
                            and reader.renderer
                        ):
                            reader.go_to_page(0)

                        self.logger.info(
                            "Reset reading position for %s via web interface",
                            os.path.basename(book_path),
                        )

                        book_title = get_epub_title(book_path)

                        return jsonify({
                            'status': 'success',
                            'message': (
                                f'“{book_title}” foi reposto para a página 1'
                            ),
                        })

                    else:
                        return jsonify({'error': 'No book path provided'}), 400

            except Exception as e:
                self.logger.error(f"Error resetting progress: {e}")
                return jsonify({'error': str(e)}), 500

        @self.flask_app.route('/rename', methods=['POST'])
        def rename():
            """Rename an EPUB file."""
            data = request.get_json(silent=True) or request.form
            old_raw = str(data.get('old_name', '')).strip()
            new_raw = str(data.get('new_name', '')).strip()

            old_name = Path(old_raw).name
            new_name = Path(new_raw).name

            if (
                not old_name
                or not new_name
                or old_raw != old_name
                or new_raw != new_name
                or '/' in old_raw
                or '\\' in old_raw
                or '/' in new_raw
                or '\\' in new_raw
            ):
                return jsonify({
                    'success': False,
                    'error': 'Invalid filename',
                }), 400

            if not old_name.lower().endswith('.epub'):
                return jsonify({
                    'success': False,
                    'error': 'Source file must be an EPUB',
                }), 400

            if not new_name.lower().endswith('.epub'):
                new_name += '.epub'

            books_root = Path(self.books_dir).resolve()
            old_path = (books_root / old_name).resolve()
            new_path = (books_root / new_name).resolve()

            if (
                old_path.parent != books_root
                or new_path.parent != books_root
                or new_path.suffix.lower() != '.epub'
            ):
                return jsonify({'success': False, 'error': 'Invalid filename'}), 400
            if not old_path.exists():
                return jsonify({'success': False, 'error': 'Book not found'}), 404
            if new_path.exists():
                return jsonify({'success': False, 'error': 'A book with that name already exists'}), 409

            current_book = getattr(self.app_instance.reader_screen, 'current_book_path', None)
            if current_book and Path(current_book).resolve() == old_path:
                return jsonify({
                    'success': False,
                    'error': 'Close the book on the reader before renaming it'
                }), 409

            blocked, prep_status = self._epub_file_change_blocked(old_path)
            if blocked:
                return jsonify({
                    'success': False,
                    'error': 'Wait for EPUB preparation to finish before renaming it',
                    'state': prep_status.get('state'),
                    'progress': prep_status.get('progress'),
                }), 409

            old_path.rename(new_path)
            moved_caches = self._move_epub_caches(old_path, new_path)

            self.logger.info(
                "Renamed: %s -> %s (moved %d layout cache files)",
                old_name,
                new_name,
                len(moved_caches),
            )
            self._reload_library()
            return jsonify({
                'success': True,
                'old_name': old_name,
                'new_name': new_name,
                'moved_caches': len(moved_caches),
            })

        @self.flask_app.route('/control/<action>', methods=['POST', 'GET'])
        @self.flask_app.route('/remote/<action>', methods=['POST', 'GET'])
        def control(action):
            """Execute a remote navigation action."""
            actions = {
                'next': self.app_instance._handle_next,
                'prev': self.app_instance._handle_prev,
                # "select" represents confirmation, equivalent to the
                # physical GPIO5 long press. _handle_toggle is a separate
                # legacy action and returns from Wi-Fi to the main menu.
                'select': getattr(
                    self.app_instance,
                    '_handle_confirm',
                    getattr(
                        self.app_instance,
                        '_handle_toggle',
                        self.app_instance._handle_select,
                    ),
                ),
                'back': self.app_instance._handle_back,
                'menu': self.app_instance._handle_menu,
            }
            handler = actions.get(action)
            if handler is None:
                return jsonify({'success': False, 'error': 'Unknown action'}), 404
            if not getattr(self.app_instance, 'running', False):
                return jsonify({'success': False, 'error': 'PiBook is not ready'}), 503

            try:
                with self._action_lock:
                    handler()
                return jsonify({'success': True, 'status': 'ok', 'action': action})
            except Exception as exc:
                self.logger.error("Remote action %s failed: %s", action, exc, exc_info=True)
                return jsonify({'success': False, 'error': str(exc)}), 500

        @self.flask_app.route('/api/menu/open', methods=['POST'])
        def open_menu_direct():
            """Open one of the physical main-menu applications directly."""
            data = request.get_json(silent=True) or request.form
            screen = str(data.get('screen', '')).strip()

            allowed = {
                'library',
                'continue',
                'wifi',
                'todo',
                'typewriter',
                'shutdown',
            }

            if screen not in allowed:
                return jsonify({
                    'success': False,
                    'error': 'Unknown menu application',
                }), 400

            if not getattr(self.app_instance, 'running', False):
                return jsonify({
                    'success': False,
                    'error': 'PiBook is not ready',
                }), 503

            handler = getattr(
                self.app_instance,
                '_open_menu_app_direct',
                None,
            )

            if handler is None:
                return jsonify({
                    'success': False,
                    'error': 'Direct menu access unavailable',
                }), 503

            # Shutdown must return to the browser before the PiBook stops.
            if screen == 'shutdown':
                def shutdown_worker():
                    try:
                        with self._action_lock:
                            handler(
                                screen,
                                source='WEB DIRECT',
                            )
                    except Exception as exc:
                        self.logger.error(
                            "Direct Web shutdown failed: %s",
                            exc,
                            exc_info=True,
                        )

                threading.Thread(
                    target=shutdown_worker,
                    daemon=True,
                    name='pibook-web-shutdown',
                ).start()

                return jsonify({
                    'success': True,
                    'status': 'accepted',
                    'screen': screen,
                }), 202

            try:
                with self._action_lock:
                    success = handler(
                        screen,
                        source='WEB DIRECT',
                    )

                if not success:
                    return jsonify({
                        'success': False,
                        'error': 'Application could not be opened',
                    }), 409

                return jsonify({
                    'success': True,
                    'status': 'ok',
                    'screen': screen,
                })

            except Exception as exc:
                self.logger.error(
                    "Direct Web menu action %s failed: %s",
                    screen,
                    exc,
                    exc_info=True,
                )
                return jsonify({
                    'success': False,
                    'error': str(exc),
                }), 500

        @self.flask_app.route('/api/state')
        def reader_state():
            """Return the current PiBook screen and reader state."""
            screen = getattr(getattr(self.app_instance, 'navigation', None), 'current_screen', None)
            screen_value = getattr(screen, 'value', str(screen) if screen else 'unknown')
            reader = getattr(self.app_instance, 'reader_screen', None)
            state = {
                'screen': screen_value,
                'screen_label': _screen_label(screen_value),
                'book': None,
                'page': None,
                'total_pages': None,
            }
            if reader is not None:
                current_path = getattr(reader, 'current_book_path', None)
                if current_path:
                    state['book'] = get_epub_title(current_path)
                if getattr(reader, 'renderer', None):
                    state['page'] = int(getattr(reader, 'current_page', 0)) + 1
                    try:
                        state['total_pages'] = reader.renderer.get_page_count()
                    except Exception:
                        pass
            return jsonify(state)

        @self.flask_app.route('/api/display/preview')
        def display_preview():
            """Return the last frame successfully shown on the e-paper."""
            display = getattr(self.app_instance, 'display', None)

            if display is None or not hasattr(
                display,
                'get_last_display_image',
            ):
                return jsonify({
                    'success': False,
                    'error': 'Display preview unavailable',
                }), 503

            image = display.get_last_display_image()

            if image is None:
                return jsonify({
                    'success': False,
                    'error': 'No displayed frame available yet',
                }), 503

            output = BytesIO()
            image.save(output, format='PNG')

            response = Response(
                output.getvalue(),
                mimetype='image/png',
            )
            response.headers['Cache-Control'] = 'no-store, max-age=0'
            return response

        @self.flask_app.route('/api/books/open', methods=['POST'])
        def open_book():
            """Open a library book on the e-paper reader."""
            data = request.get_json(silent=True) or request.form
            filename = secure_filename(Path(data.get('filename', '')).name)
            filepath = (Path(self.books_dir) / filename).resolve()
            books_root = Path(self.books_dir).resolve()

            if filepath.parent != books_root or filepath.suffix.lower() != '.epub':
                return jsonify({'success': False, 'error': 'Invalid filename'}), 400
            if not filepath.exists():
                return jsonify({'success': False, 'error': 'Book not found'}), 404

            book = {
                'title': get_epub_title(filepath),
                'path': str(filepath),
            }

            def worker():
                try:
                    with self._action_lock:
                        self.app_instance._open_book(book)
                except Exception as exc:
                    self.logger.error("Opening book from web failed: %s", exc, exc_info=True)

            threading.Thread(target=worker, daemon=True).start()
            return jsonify({
                'success': True,
                'status': 'opening',
                'filename': filename
            }), 202

        @self.flask_app.route('/api/books/reread', methods=['POST'])
        def reread_book():
            """Start a new reading cycle for a finished EPUB."""
            data = request.get_json(silent=True) or request.form
            filename = secure_filename(
                Path(data.get('filename', '')).name
            )
            filepath = (
                Path(self.books_dir) / filename
            ).resolve()
            books_root = Path(self.books_dir).resolve()

            if (
                filepath.parent != books_root
                or filepath.suffix.lower() != '.epub'
            ):
                return jsonify({
                    'success': False,
                    'error': 'Invalid filename',
                }), 400

            if not filepath.exists():
                return jsonify({
                    'success': False,
                    'error': 'Book not found',
                }), 404

            details = (
                self.app_instance.progress_manager
                .get_progress_details(str(filepath))
            )

            if (
                not details
                or details.get('status') != 'finished'
            ):
                return jsonify({
                    'success': False,
                    'error': (
                        'Só é possível iniciar uma releitura '
                        'num livro concluído'
                    ),
                }), 409

            book = {
                'title': get_epub_title(filepath),
                'path': str(filepath),
                'start_reread': True,
            }

            def worker():
                try:
                    with self._action_lock:
                        self.app_instance._open_book(book)
                except Exception as exc:
                    self.logger.error(
                        "Starting re-reading from web failed: %s",
                        exc,
                        exc_info=True,
                    )

            threading.Thread(
                target=worker,
                daemon=True,
            ).start()

            return jsonify({
                'success': True,
                'status': 'opening',
                'filename': filename,
            }), 202

        # To-Do List API Routes

        # To-Do List API Routes
        @self.flask_app.route('/api/cpu_voltage')
        def cpu_voltage():
            """Return the current CPU core voltage when supported."""
            try:
                result = subprocess.run(
                    ['vcgencmd', 'measure_volts', 'core'],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if result.returncode == 0:
                    return jsonify({'voltage': result.stdout.strip()})
                return jsonify({'error': 'Could not read voltage'}), 500
            except Exception as exc:
                return jsonify({'error': str(exc)}), 500

        @self.flask_app.route('/api/battery_status')
        def battery_status():
            """Get current battery status including charging state"""
            try:
                if self.app_instance.battery_monitor:
                    status = self.app_instance.battery_monitor.get_status()
                    return jsonify(status)
                else:
                    return jsonify({'error': 'Battery monitor not available'}), 503
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        @self.flask_app.route('/api/battery_log_snapshot')
        def battery_log_snapshot():
            """
            Return the low-cost snapshot used by the battery cycle logger.

            This endpoint deliberately avoids subprocesses. Battery data comes
            from the existing monitor, application state comes from memory,
            and radio state is read from cheap kernel/sysfs interfaces.
            """
            try:
                stats = {}

                # Battery: preserve the same field names historically consumed
                # by battery_cycle_logger.py.
                monitor = getattr(
                    self.app_instance,
                    'battery_monitor',
                    None,
                )

                if monitor is not None:
                    battery = monitor.get_status()

                    stats.update({
                        'battery_percentage':
                            battery.get('percentage'),
                        'battery_soc_precise':
                            battery.get('soc_precise'),
                        'battery_percentage_voltage':
                            battery.get('percentage_voltage'),
                        'battery_voltage':
                            battery.get('voltage'),
                        'battery_current_ma':
                            battery.get('current_ma'),
                        'battery_charging':
                            battery.get('is_charging'),
                        'battery_backend':
                            battery.get('backend'),
                    })

                # Current physical screen: already available in memory.
                try:
                    screen = (
                        self.app_instance
                        .navigation
                        .current_screen
                    )
                    stats['current_screen'] = getattr(
                        screen,
                        'value',
                        str(screen),
                    )
                except Exception:
                    stats['current_screen'] = 'unknown'

                stats['current_screen_label'] = _screen_label(
                    stats.get('current_screen')
                )

                # Include the effective profile for diagnostics/future clients.
                try:
                    profile = getattr(
                        self.app_instance,
                        '_effective_power_profile',
                        None,
                    )
                    stats['effective_power_profile'] = (
                        profile or 'unknown'
                    )
                except Exception:
                    stats['effective_power_profile'] = 'unknown'

                # Wi-Fi administrative state from sysfs.
                # IFF_UP == 0x1, matching the old "ip link" intent without
                # starting a subprocess every 30 seconds.
                try:
                    flags_text = Path(
                        '/sys/class/net/wlan0/flags'
                    ).read_text().strip()

                    flags = int(flags_text, 0)

                    stats['wifi_status'] = (
                        'On' if (flags & 0x1) else 'Off'
                    )
                except FileNotFoundError:
                    stats['wifi_status'] = 'Off'
                except Exception:
                    stats['wifi_status'] = 'Unknown'

                # Bluetooth state directly from rfkill sysfs.
                # No rfkill entry is the expected true-lazy OFF state.
                try:
                    bluetooth_found = False
                    bluetooth_on = False

                    for entry in Path('/sys/class/rfkill').glob('rfkill*'):
                        try:
                            radio_type = (
                                (entry / 'type')
                                .read_text()
                                .strip()
                                .lower()
                            )
                        except Exception:
                            continue

                        if radio_type != 'bluetooth':
                            continue

                        bluetooth_found = True

                        try:
                            state = (
                                (entry / 'state')
                                .read_text()
                                .strip()
                            )
                            bluetooth_on = state == '1'
                        except Exception:
                            bluetooth_on = False

                        break

                    if not bluetooth_found:
                        stats['bluetooth_status'] = 'Off'
                    else:
                        stats['bluetooth_status'] = (
                            'On' if bluetooth_on else 'Off'
                        )

                except Exception:
                    stats['bluetooth_status'] = 'Unknown'

                return jsonify(stats)

            except Exception as exc:
                self.logger.error(
                    "Battery log snapshot failed: %s",
                    exc,
                )
                return jsonify({'error': str(exc)}), 500

        # IP Scanner v2 API Routes
        from src.core.ip_scanner_service import IPScannerService

        ip_scanner_service = IPScannerService()

        @self.flask_app.route('/api/ipscanner/status')
        def ipscanner_status():
            """Return IP Scanner v2 status and latest results."""
            try:
                return jsonify(ip_scanner_service.status())
            except Exception as exc:
                self.logger.error(
                    "IP Scanner v2 status error: %s",
                    exc,
                )
                return jsonify({'error': str(exc)}), 503

        @self.flask_app.route('/api/ipscanner/scan', methods=['POST'])
        def ipscanner_start():
            """Start one asynchronous IP Scanner v2 discovery."""
            try:
                return jsonify(ip_scanner_service.start())
            except Exception as exc:
                self.logger.error(
                    "IP Scanner v2 start error: %s",
                    exc,
                )
                return jsonify({'error': str(exc)}), 503

        # Klipper Printer Discovery API
        # Store discovered printers
        self.klipper_printers = []
        self.klipper_scanning = False

        @self.flask_app.route('/api/klipper/printers')
        def klipper_printers():
            """Get list of discovered Klipper printers"""
            return jsonify({
                'printers': self.klipper_printers,
                'scanning': self.klipper_scanning
            })

        @self.flask_app.route('/api/klipper/scan', methods=['POST'])
        def klipper_scan():
            """Scan network for Klipper printers"""
            import threading
            import time

            if self.klipper_scanning:
                return jsonify({'status': 'scanning'})

            def scan_for_klipper():
                self.klipper_scanning = True
                self.klipper_printers = []

                try:
                    # Use the shared IP Scanner v2.
                    status = ip_scanner_service.status()

                    if not status.get('scanning'):
                        ip_scanner_service.start()

                    deadline = time.monotonic() + 60

                    while time.monotonic() < deadline:
                        status = ip_scanner_service.status()

                        if not status.get('scanning'):
                            break

                        time.sleep(0.5)

                    if str(status.get('status', '')).lower() != 'success':
                        raise RuntimeError(
                            status.get('error')
                            or 'Não foi possível pesquisar a rede local.'
                        )

                    for device in status.get('devices', []):
                        ip = device['ip']

                        # Check if port 7125 (Moonraker API) is open
                        if self._check_port(ip, 7125):
                            printer_info = self._get_klipper_info(ip, device.get('hostname', ''))
                            if printer_info:
                                self.klipper_printers.append(printer_info)

                    self.logger.info(f"Found {len(self.klipper_printers)} Klipper printers")

                except Exception as e:
                    self.logger.error(f"Klipper scan error: {e}")
                finally:
                    self.klipper_scanning = False

            thread = threading.Thread(target=scan_for_klipper, daemon=True)
            thread.start()

            return jsonify({'status': 'started'})

        def schedule_power_action(action: str):
            """Clean the display, then request reboot or power-off."""
            def worker():
                time.sleep(1.0)
                try:
                    self.app_instance.stop()
                except Exception as exc:
                    self.logger.error("PiBook cleanup before %s failed: %s", action, exc, exc_info=True)
                command = 'reboot' if action == 'reboot' else 'poweroff'
                try:
                    subprocess.run(
                        ['sudo', 'systemctl', command],
                        check=False,
                        timeout=15
                    )
                except Exception as exc:
                    self.logger.error("System %s failed: %s", action, exc, exc_info=True)

            threading.Thread(target=worker, daemon=True).start()

        @self.flask_app.route('/reboot', methods=['POST', 'GET'])
        @self.flask_app.route('/api/system/reboot', methods=['POST'])
        def reboot():
            self.logger.info("Graceful reboot requested via web interface")
            schedule_power_action('reboot')
            return jsonify({'success': True, 'status': 'rebooting'})

        @self.flask_app.route('/shutdown', methods=['POST'])
        @self.flask_app.route('/api/system/shutdown', methods=['POST'])
        def shutdown():
            self.logger.info("Graceful shutdown requested via web interface")
            schedule_power_action('shutdown')
            return jsonify({'success': True, 'status': 'shutting_down'})

        @self.flask_app.route('/settings')
        def settings():
            return redirect(url_for('index') + '#settings')

        @self.flask_app.route('/save_settings', methods=['POST'])
        def save_settings():
            """Save and apply settings supported by the Pi Zero W build."""
            with self._settings_lock:
                try:
                    data = request.get_json(silent=True) or request.form.to_dict()
                    current = self._load_settings()

                    def as_bool(name, default):
                        value = data.get(name, default)
                        if isinstance(value, bool):
                            return value
                        return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}

                    def bounded_float(name, default, minimum, maximum):
                        return max(minimum, min(maximum, float(data.get(name, default))))

                    def bounded_int(name, default, minimum, maximum):
                        return max(minimum, min(maximum, int(float(data.get(name, default)))))

                    power_mode = str(
                        data.get(
                            'power_mode',
                            current.get('power_mode', 'auto'),
                        )
                    ).strip().lower()

                    if power_mode not in {
                        'auto', 'mains', 'battery', 'powersave'
                    }:
                        power_mode = 'auto'

                    settings_data = dict(current)
                    settings_data.update({
                        'zoom': bounded_float(
                            'zoom', current.get('zoom', 1.0), 0.5, 2.0
                        ),
                        'show_page_numbers': as_bool(
                            'show_page_numbers',
                            current.get('show_page_numbers', True),
                        ),
                        'sleep_message': str(
                            data.get(
                                'sleep_message',
                                current.get(
                                    'sleep_message',
                                    "Shh I'm sleeping",
                                ),
                            )
                        )[:50],
                        'shutdown_message': str(
                            data.get(
                                'shutdown_message',
                                current.get('shutdown_message', 'OFF'),
                            )
                        )[:20],
                        'items_per_page': bounded_int(
                            'items_per_page',
                            current.get('items_per_page', 4),
                            3,
                            6,
                        ),
                        'library_font_size': bounded_int(
                            'library_font_size',
                            current.get('library_font_size', 20),
                            14,
                            28,
                        ),
                        'power_mode': power_mode,
                        'auto_powersave_enabled': as_bool(
                            'auto_powersave_enabled',
                            current.get('auto_powersave_enabled', True),
                        ),
                        'auto_powersave_threshold': bounded_int(
                            'auto_powersave_threshold',
                            current.get('auto_powersave_threshold', 30),
                            5,
                            80,
                        ),
                    })
                    settings_data.pop('boot_cores', None)

                    # Power Profiles v2.
                    profiles = json.loads(json.dumps(
                        current.get('power_profiles', {})
                    ))

                    for name in ('mains', 'battery', 'powersave'):
                        profile = profiles.setdefault(name, {})

                        for key in (
                            'sleep',
                            'network_reading',
                            'network_sleep',
                        ):
                            field = f'profile_{name}_{key}'
                            value = str(
                                data.get(
                                    field,
                                    profile.get(key, 'auto'),
                                )
                            ).strip().lower()

                            if value not in {'auto', 'on', 'off'}:
                                value = 'auto'

                            profile[key] = value

                        profile['sleep_timeout'] = max(
                            30,
                            min(
                                3600,
                                int(float(data.get(
                                    f'profile_{name}_sleep_timeout',
                                    profile.get('sleep_timeout', 300),
                                ))),
                            ),
                        )

                        profile['reader_prefetch'] = max(
                            0,
                            min(
                                20,
                                int(float(data.get(
                                    f'profile_{name}_reader_prefetch',
                                    profile.get('reader_prefetch', 3),
                                ))),
                            ),
                        )

                    settings_data['power_profiles'] = profiles

                    old_zoom = float(current.get('zoom', 1.0))

                    self._save_settings(settings_data)

                    manager = getattr(self.app_instance, 'settings_manager', None)
                    if manager is not None:
                        manager.settings = manager.load()
                        self.app_instance.settings = manager.get_all()
                    else:
                        self.app_instance.settings = settings_data

                    reader = self.app_instance.reader_screen
                    reader.zoom_factor = settings_data['zoom']
                    reader.show_page_numbers = settings_data['show_page_numbers']

                    book_reloaded = False
                    layout_changed = (
                        old_zoom != settings_data['zoom']
                    )
                    epub_path = (
                        getattr(reader, 'current_book_path', None)
                        or getattr(reader, 'epub_path', None)
                    )

                    if layout_changed and epub_path:
                        current_page = int(getattr(reader, 'current_page', 0))
                        book_lock = getattr(
                            self.app_instance,
                            '_book_open_lock',
                            None,
                        )
                        if book_lock is None:
                            raise RuntimeError(
                                'Reader EPUB lock is unavailable'
                            )

                        with book_lock:
                            reader.close()
                            reader.load_epub(
                                epub_path,
                                zoom_factor=settings_data['zoom'],
                            )
                            try:
                                reader.go_to_page(current_page)
                            except Exception:
                                reader.current_page = current_page

                        book_reloaded = True

                    library = self.app_instance.library_screen
                    library.items_per_page = settings_data['items_per_page']
                    library.font_size = settings_data['library_font_size']
                    try:
                        from PIL import ImageFont
                        library.font = ImageFont.truetype(
                            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                            library.font_size,
                        )
                    except Exception:
                        library.font = ImageFont.load_default()
                    self._reload_library(render=False)

                    self.app_instance.config.set('reader.zoom', settings_data['zoom'])
                    self.app_instance.config.set(
                        'library.items_per_page',
                        settings_data['items_per_page']
                    )
                    self.app_instance.config.set(
                        'library.font_size',
                        settings_data['library_font_size']
                    )
                    self.app_instance.config.save()

                    monitor = getattr(
                        self.app_instance,
                        'battery_monitor',
                        None,
                    )
                    percentage = (
                        monitor.get_percentage()
                        if monitor is not None
                        else None
                    )
                    power_source = (
                        monitor.get_power_source()
                        if monitor is not None
                        else 'unknown'
                    )

                    self.app_instance._update_power_profile(
                        percentage,
                        power_source,
                    )

                    if book_reloaded or getattr(self.app_instance, 'running', False):
                        try:
                            self.app_instance._render_current_screen()
                        except Exception as exc:
                            self.logger.warning("Display refresh after settings failed: %s", exc)

                    self.logger.info("Settings saved for Pi Zero W: %s", settings_data)
                    return jsonify({
                        'success': True,
                        'status': 'success',
                        'message': 'Settings saved and applied',
                        'settings': settings_data,
                        'book_reloaded': book_reloaded,
                    })
                except Exception as exc:
                    self.logger.error("Failed to save settings: %s", exc, exc_info=True)
                    return jsonify({'success': False, 'error': str(exc)}), 400

        @self.flask_app.route('/terminal/execute', methods=['POST'])
        def terminal_execute():
            """Execute a terminal command and stream stdout/stderr as SSE."""
            import json
            import os
            import re
            import shlex
            import signal
            import subprocess
            import time
            import uuid
            from flask import Response, stream_with_context

            payload = request.get_json(silent=True) or {}
            command = str(payload.get('command', '')).strip()
            requested_id = str(payload.get('command_id', '')).strip()

            if not command:
                return jsonify({'error': 'No command provided'}), 400

            if requested_id and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', requested_id):
                command_id = requested_id
            else:
                command_id = uuid.uuid4().hex

            if not hasattr(self, '_terminal_processes'):
                self._terminal_processes = {}

            normalized = ' '.join(command.split())

            # PiBook is heavily customized locally.
            # Direct git pull is intentionally blocked because it could
            # overwrite or conflict with PiBook-specific modifications.
            if normalized == 'git pull':
                self.logger.warning(
                    "Blocked direct git pull from web terminal"
                )
                return jsonify({
                    'error': (
                        'git pull está bloqueado neste PiBook. '
                        'As atualizações devem ser aplicadas através dos '
                        'instaladores PiBook com backup, validação e rollback.'
                    )
                }), 409

            # A direct restart would kill the web request that launched it.
            # Schedule it outside pibook-zero's cgroup and answer the browser first.
            restart_requested = normalized in {
                'sudo systemctl restart pibook-zero.service',
                'sudo systemctl restart pibook-zero',
            }

            if restart_requested:
                unit = f"pibook-terminal-restart-{int(time.time())}-{command_id[:8]}"
                command = (
                    "sudo systemd-run --quiet "
                    f"--unit={shlex.quote(unit)} --on-active=2s "
                    "/bin/systemctl restart pibook-zero.service"
                )

            self.logger.info("Terminal command [%s] requested", command_id)

            def sse(data):
                return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

            @stream_with_context
            def generate():
                process = None
                started = time.monotonic()

                try:
                    env = os.environ.copy()
                    env.update({
                        'TERM': 'dumb',
                        'NO_COLOR': '1',
                        'SYSTEMD_COLORS': '0',
                        'SYSTEMD_PAGER': 'cat',
                        'PAGER': 'cat',
                        'GIT_PAGER': 'cat',
                        'PYTHONUNBUFFERED': '1',
                    })

                    process = subprocess.Popen(
                        command,
                        shell=True,
                        executable='/bin/bash',
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding='utf-8',
                        errors='replace',
                        cwd=str(self.project_dir),
                        bufsize=1,
                        start_new_session=True,
                        env=env,
                    )

                    self._terminal_processes[command_id] = process

                    yield sse({
                        'type': 'started',
                        'command_id': command_id,
                        'pid': process.pid,
                        'restart_requested': restart_requested,
                    })

                    for line in iter(process.stdout.readline, ''):
                        if line:
                            yield sse({
                                'type': 'stdout',
                                'stdout': line,
                            })

                    if process.stdout:
                        process.stdout.close()

                    returncode = process.wait()
                    duration = round(time.monotonic() - started, 2)

                    yield sse({
                        'type': 'finished',
                        'returncode': returncode,
                        'duration': duration,
                        'restart_requested': restart_requested,
                    })

                except GeneratorExit:
                    # If the browser disappears, do not leave an unattended
                    # command tree running in the background.
                    if process and process.poll() is None:
                        try:
                            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                        except Exception:
                            pass
                    raise

                except Exception as exc:
                    self.logger.error(
                        "Error during terminal command [%s]: %s",
                        command_id,
                        exc,
                    )
                    yield sse({
                        'type': 'error',
                        'error': str(exc),
                        'duration': round(time.monotonic() - started, 2),
                    })

                finally:
                    self._terminal_processes.pop(command_id, None)

            return Response(
                generate(),
                mimetype='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache',
                    'X-Accel-Buffering': 'no',
                },
            )

        @self.flask_app.route('/terminal/stop', methods=['POST'])
        def terminal_stop():
            """Stop a command launched by the web terminal."""
            import os
            import signal
            import subprocess

            payload = request.get_json(silent=True) or {}
            command_id = str(payload.get('command_id', '')).strip()

            processes = getattr(self, '_terminal_processes', {})
            process = processes.get(command_id)

            if process is None:
                return jsonify({
                    'success': False,
                    'error': 'Command is not running',
                }), 404

            if process.poll() is not None:
                processes.pop(command_id, None)
                return jsonify({
                    'success': True,
                    'status': 'already_finished',
                })

            try:
                # start_new_session=True gives each terminal command its own
                # process group, so children are stopped as well.
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)

                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)

                return jsonify({
                    'success': True,
                    'status': 'stopped',
                })

            except Exception as exc:
                self.logger.error(
                    "Failed to stop terminal command [%s]: %s",
                    command_id,
                    exc,
                )
                return jsonify({
                    'success': False,
                    'error': str(exc),
                }), 500

        # Log Viewing APIs
        @self.flask_app.route('/api/logs/app')
        def view_app_logs():
            """Get recent application logs"""
            try:
                # Default path, although we should prefer config value if accessible cleanly
                log_path = self.app_instance.config.get('logging.file', str(self.project_dir / 'logs' / 'pibook.log'))
                
                if not os.path.exists(log_path):
                    return jsonify({'logs': f"Log file not found at {log_path}", 'type': 'app'})
                
                # Use tail to get last 200 lines efficiently
                import subprocess
                result = subprocess.run(
                    ['tail', '-n', '200', log_path],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
                if result.returncode == 0:
                    return jsonify({'logs': result.stdout, 'type': 'app'})
                else:
                    return jsonify({'logs': f"Error reading logs: {result.stderr}", 'type': 'app'})
                    
            except Exception as e:
                self.logger.error(f"Failed to read app logs: {e}")
                return jsonify({'error': str(e)}), 500

        @self.flask_app.route('/api/logs/system')
        def view_system_logs():
            """Get recent logs for the PiBook Zero service."""
            try:
                result = subprocess.run(
                    ['journalctl', '-u', self.service_name, '-n', '200', '--no-pager'],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode != 0:
                    return jsonify({
                        'logs': f"Error reading system logs: {result.stderr}",
                        'type': 'system'
                    })
                logs = result.stdout
                if not logs.strip():
                    logs = (
                        f"No system logs found for {self.service_name}. "
                        "The service may be disabled while PiBook is run manually."
                    )
                return jsonify({'logs': logs, 'type': 'system'})
            except subprocess.TimeoutExpired:
                return jsonify({'error': 'Reading system logs timed out'}), 408
            except Exception as exc:
                self.logger.error("Failed to read system logs: %s", exc)
                return jsonify({'error': str(exc)}), 500

        @self.flask_app.route('/api/bluetooth/status')
        def bluetooth_status():
            """Get Bluetooth status and paired devices"""
            try:
                import subprocess
                
                # Check if Bluetooth is powered on using rfkill (much faster than bluetoothctl)
                result = subprocess.run(['rfkill', 'list', 'bluetooth'], capture_output=True, text=True, timeout=2)
                # Bluetooth is on if it's not soft-blocked (hard block is physical switch)
                powered = 'Soft blocked: no' in result.stdout
                self.logger.debug(f"Bluetooth status check: powered={powered}, rfkill output: {result.stdout[:100]}")
                
                # When Bluetooth is OFF, never invoke bluetoothctl. With
                # BlueZ configured on-demand, bluetoothctl could activate
                # bluetooth.service through D-Bus just to answer a status poll.
                if not powered:
                    return jsonify({
                        'powered': False,
                        'paired_devices': [],
                    })

                # Bluetooth is intentionally ON: querying BlueZ is safe.
                result = subprocess.run(
                    [
                        'sudo',
                        str(self.project_dir / 'scripts' / 'bluetooth_helper.sh'),
                        'paired_devices',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                paired_devices = []
                for line in result.stdout.strip().split('\n'):
                    if line.startswith('Device '):
                        parts = line.split(' ', 2)
                        if len(parts) >= 3:
                            paired_devices.append({
                                'mac': parts[1],
                                'name': parts[2],
                            })

                self.logger.debug(
                    f"Paired devices raw output: {result.stdout}"
                )

                return jsonify({
                    'powered': True,
                    'paired_devices': paired_devices,
                })
            except Exception as e:
                self.logger.error(f"Bluetooth status check failed: {e}")
                return jsonify({'error': str(e)}), 500

        @self.flask_app.route('/api/bluetooth/power', methods=['POST'])
        def bluetooth_power():
            """Toggle Bluetooth power"""
            try:
                import subprocess
                data = request.get_json()
                power_on = data.get('power', False)
                action = 'power_on' if power_on else 'power_off'
                
                result = subprocess.run(['sudo', str(self.project_dir / 'scripts' / 'bluetooth_helper.sh'), action],
                                      capture_output=True, text=True, timeout=10)
                
                if result.returncode != 0:
                    return jsonify({
                        'error': result.stderr or result.stdout or
                                 'Bluetooth helper failed'
                    }), 500

                verify = subprocess.run(
                    ['rfkill', 'list', 'bluetooth'],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if verify.returncode != 0:
                    return jsonify({
                        'error': 'Could not verify Bluetooth state'
                    }), 500

                actual_powered = 'Soft blocked: no' in verify.stdout
                if actual_powered != power_on:
                    return jsonify({
                        'error': 'Bluetooth state did not change as requested',
                        'powered': actual_powered,
                    }), 500

                return jsonify({
                    'success': True,
                    'powered': actual_powered,
                })
            except Exception as e:
                self.logger.error(f"Bluetooth power toggle failed: {e}")
                return jsonify({'error': str(e)}), 500

        @self.flask_app.route('/api/bluetooth/scan', methods=['POST'])
        def bluetooth_scan():
            """Start/stop Bluetooth scanning"""
            try:
                import subprocess
                data = request.get_json()
                scan_on = data.get('scan', False)
                action = 'scan_on' if scan_on else 'scan_off'
                
                result = subprocess.run(['sudo', str(self.project_dir / 'scripts' / 'bluetooth_helper.sh'), action],
                                      capture_output=True, text=True, timeout=10)
                
                return jsonify({'success': True, 'scanning': scan_on})
            except Exception as e:
                self.logger.error(f"Bluetooth scan toggle failed: {e}")
                return jsonify({'error': str(e)}), 500

        @self.flask_app.route('/api/bluetooth/devices')
        def bluetooth_devices():
            """Get discovered Bluetooth devices"""
            try:
                import subprocess
                devices = []
                seen_macs = set()

                # Get all known devices using helper script (includes recently discovered ones)
                result = subprocess.run(['sudo', str(self.project_dir / 'scripts' / 'bluetooth_helper.sh'), 'devices'],
                                      capture_output=True, text=True, timeout=10)
                for line in result.stdout.strip().split('\n'):
                    if line.startswith('Device '):
                        parts = line.split(' ', 2)
                        if len(parts) >= 2:
                            mac = parts[1]
                            name = parts[2] if len(parts) >= 3 else mac
                            if mac not in seen_macs:
                                seen_macs.add(mac)
                                devices.append({'mac': mac, 'name': name})

                # Also get paired devices to mark them
                paired_result = subprocess.run(['sudo', str(self.project_dir / 'scripts' / 'bluetooth_helper.sh'), 'paired_devices'],
                                             capture_output=True, text=True, timeout=10)
                paired_macs = set()
                for line in paired_result.stdout.strip().split('\n'):
                    if line.startswith('Device '):
                        parts = line.split(' ', 2)
                        if len(parts) >= 2:
                            paired_macs.add(parts[1])

                # Mark paired devices
                for device in devices:
                    device['paired'] = device['mac'] in paired_macs

                return jsonify({'devices': devices})
            except Exception as e:
                self.logger.error(f"Bluetooth device list failed: {e}")
                return jsonify({'error': str(e)}), 500

        @self.flask_app.route('/api/bluetooth/pair', methods=['POST'])
        def bluetooth_pair():
            """Pair with a Bluetooth device"""
            try:
                import subprocess
                import threading
                import time
                data = request.get_json()
                mac = data.get('mac')
                pin = data.get('pin', '')

                if not mac:
                    return jsonify({'error': 'MAC address required'}), 400

                # For PIN-based pairing, use synchronous call
                if pin:
                    result = subprocess.run(['sudo', str(self.project_dir / 'scripts' / 'bluetooth_helper.sh'), 'pair', mac, pin],
                                          capture_output=True, text=True, timeout=60)
                    if result.returncode == 0 or 'successful' in result.stdout.lower():
                        return jsonify({'success': True, 'status': 'paired'})
                    else:
                        return jsonify({'error': result.stderr or result.stdout}), 500

                # For passkey-based pairing (keyboards), start process and read output incrementally
                # to capture passkey early
                process = subprocess.Popen(
                    ['sudo', str(self.project_dir / 'scripts' / 'bluetooth_helper.sh'), 'pair', mac],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True
                )

                # Read output with timeout, looking for passkey
                import select
                import re
                output_lines = []
                passkey = None
                start_time = time.time()

                # Wait up to 15 seconds for passkey to appear, then return response
                while time.time() - start_time < 15:
                    # Check if process has output
                    if process.poll() is not None:
                        # Process finished
                        remaining = process.stdout.read()
                        if remaining:
                            output_lines.append(remaining)
                        break

                    try:
                        line = process.stdout.readline()
                        if line:
                            output_lines.append(line)
                            self.logger.info(f"BT pair output: {line.strip()}")

                            # Check for passkey
                            match = re.search(r'PASSKEY_REQUIRED:(\d+)', line)
                            if match:
                                passkey = match.group(1)
                                self.logger.info(f"Found passkey: {passkey}")
                                # Return immediately with passkey, let process continue in background
                                return jsonify({
                                    'success': True,
                                    'status': 'passkey_required',
                                    'passkey': passkey,
                                    'message': f"Type {passkey} on the keyboard and press Enter"
                                })
                    except Exception as e:
                        self.logger.warning(f"Error reading BT output: {e}")
                        break

                    time.sleep(0.1)

                # Check final output
                full_output = ''.join(output_lines)
                self.logger.info(f"BT pair full output: {full_output[:500]}")

                # Check for explicit failure messages
                if 'PAIRING_FAILED' in full_output or 'Failed to pair' in full_output:
                    error_msg = 'Pairing failed. Make sure the keyboard is in pairing mode (hold power button until light blinks).'
                    if 'ConnectionAttemptFailed' in full_output:
                        error_msg = 'Could not connect to keyboard. Press the power button to wake it up, then try again immediately.'
                    elif 'not available' in full_output.lower():
                        error_msg = 'Device not found. Make sure Bluetooth is scanning and the keyboard is discoverable.'
                    return jsonify({'success': False, 'error': error_msg}), 200

                if 'DEVICE_NOT_AVAILABLE' in full_output:
                    return jsonify({
                        'success': False,
                        'error': 'Device not found. Run a new scan and try pairing again while the keyboard light is blinking.'
                    }), 200

                if 'PAIRING_TIMEOUT' in full_output:
                    return jsonify({
                        'success': False,
                        'error': 'Pairing timed out. Wake up the keyboard and try again.'
                    }), 200

                # Check if pairing succeeded
                if 'PAIRING_SUCCESS' in full_output or 'Pairing successful' in full_output:
                    return jsonify({'success': True, 'status': 'paired'})

                # If we got here without passkey, check if process is still running
                if process.poll() is None:
                    # Process still running, might be waiting for passkey
                    # Return a message to try manual PIN entry
                    return jsonify({
                        'success': False,
                        'error': 'Pairing initiated but no passkey detected. Try manual PIN entry (0000 or 1234).'
                    }), 200

                # Default - check for any success indicator
                if 'successful' in full_output.lower():
                    return jsonify({'success': True, 'status': 'paired'})

                return jsonify({
                    'success': False,
                    'error': 'Pairing result unknown. Check if the device appears in paired devices list.'
                }), 200
            except subprocess.TimeoutExpired:
                return jsonify({'error': 'Pairing timed out'}), 500
            except Exception as e:
                self.logger.error(f"Bluetooth pairing failed: {e}")
                return jsonify({'error': str(e)}), 500

        @self.flask_app.route('/api/bluetooth/remove', methods=['POST'])
        def bluetooth_remove():
            """Remove a paired Bluetooth device"""
            try:
                import subprocess
                data = request.get_json()
                mac = data.get('mac')
                
                if not mac:
                    return jsonify({'error': 'MAC address required'}), 400
                
                result = subprocess.run(['sudo', str(self.project_dir / 'scripts' / 'bluetooth_helper.sh'), 'remove', mac],
                                      capture_output=True, text=True, timeout=10)
                
                if result.returncode == 0:
                    return jsonify({'success': True})
                else:
                    return jsonify({'error': result.stderr}), 500
            except Exception as e:
                self.logger.error(f"Bluetooth device removal failed: {e}")
                return jsonify({'error': str(e)}), 500

        @self.flask_app.route('/api/system_stats')
        def system_stats():
            """Get comprehensive system statistics"""
            try:
                import subprocess
                import platform
                
                stats = {}
                
                # CPU Temperature - direct kernel/sysfs read.
                try:
                    temp_milli = int(
                        Path(
                            '/sys/class/thermal/thermal_zone0/temp'
                        ).read_text().strip()
                    )
                    stats['cpu_temp'] = f"{temp_milli / 1000:.1f} °C"
                except Exception:
                    stats['cpu_temp'] = 'N/A'

                # CPU Speed
                try:
                    with open('/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq', 'r') as f:
                        freq_khz = int(f.read().strip())
                        freq_mhz = freq_khz / 1000
                        stats['cpu_speed'] = f"{freq_mhz:.0f} MHz"
                except:
                    # Fallback to vcgencmd if file not found
                    try:
                        result = subprocess.run(['vcgencmd', 'measure_clock', 'arm'], capture_output=True, text=True, timeout=2)
                        if result.returncode == 0:
                            # Output format: frequency(48)=600000000
                            freq_hz = int(result.stdout.strip().split('=')[1])
                            freq_mhz = freq_hz / 1000000
                            stats['cpu_speed'] = f"{freq_mhz:.0f} MHz"
                        else:
                            stats['cpu_speed'] = 'N/A'
                    except:
                        stats['cpu_speed'] = 'N/A'

                # Wi-Fi administrative state directly from sysfs.
                try:
                    flags_text = Path(
                        '/sys/class/net/wlan0/flags'
                    ).read_text().strip()
                    flags = int(flags_text, 0)
                    stats['wifi_status'] = (
                        'On' if (flags & 0x1) else 'Off'
                    )
                except FileNotFoundError:
                    stats['wifi_status'] = 'Off'
                except Exception:
                    stats['wifi_status'] = 'Unknown'

                # Bluetooth state directly from rfkill sysfs.
                # No rfkill entry is the expected true-lazy OFF state.
                try:
                    bluetooth_found = False
                    bluetooth_on = False

                    for entry in Path('/sys/class/rfkill').glob('rfkill*'):
                        try:
                            radio_type = (
                                (entry / 'type')
                                .read_text()
                                .strip()
                                .lower()
                            )
                        except Exception:
                            continue

                        if radio_type != 'bluetooth':
                            continue

                        bluetooth_found = True

                        try:
                            bluetooth_on = (
                                (entry / 'state')
                                .read_text()
                                .strip()
                                == '1'
                            )
                        except Exception:
                            bluetooth_on = False

                        break

                    if not bluetooth_found:
                        stats['bluetooth_status'] = 'Off'
                    else:
                        stats['bluetooth_status'] = (
                            'On' if bluetooth_on else 'Off'
                        )

                except Exception:
                    stats['bluetooth_status'] = 'Unknown'

                # CPU Voltage
                try:
                    result = subprocess.run(['vcgencmd', 'measure_volts'], capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        stats['cpu_voltage'] = result.stdout.strip()
                    else:
                        stats['cpu_voltage'] = 'N/A'
                except:
                    stats['cpu_voltage'] = 'N/A'
                
                # Throttle status
                try:
                    result = subprocess.run(['vcgencmd', 'get_throttled'], capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        throttled = result.stdout.strip().replace('throttled=', '')
                        if throttled == '0x0':
                            stats['throttle_status'] = 'OK'
                            stats['throttle_detail'] = 'No throttling detected'
                        else:
                            stats['throttle_status'] = throttled
                            stats['throttle_detail'] = 'Warning: Throttling detected!'
                    else:
                        stats['throttle_status'] = 'N/A'
                        stats['throttle_detail'] = 'Unable to read'
                except:
                    stats['throttle_status'] = 'N/A'
                    stats['throttle_detail'] = 'Unable to read'
                
                # OS Information
                try:
                    with open('/etc/os-release', 'r') as f:
                        os_info = {}
                        for line in f:
                            if '=' in line:
                                key, value = line.strip().split('=', 1)
                                os_info[key] = value.strip('"')
                        stats['os_name'] = os_info.get('PRETTY_NAME', 'Linux')
                except:
                    stats['os_name'] = platform.system() + ' ' + platform.release()
                
                # System Uptime
                try:
                    with open('/proc/uptime', 'r') as f:
                        uptime_seconds = float(f.read().split()[0])
                        days = int(uptime_seconds // 86400)
                        hours = int((uptime_seconds % 86400) // 3600)
                        minutes = int((uptime_seconds % 3600) // 60)
                        if days > 0:
                            stats['uptime'] = f"{days}d {hours}h {minutes}m"
                        elif hours > 0:
                            stats['uptime'] = f"{hours}h {minutes}m"
                        else:
                            stats['uptime'] = f"{minutes}m"
                except:
                    stats['uptime'] = 'N/A'
                
                # CPU Core Information
                try:
                    # Get total CPU cores
                    with open('/sys/devices/system/cpu/present', 'r') as f:
                        present = f.read().strip()
                        # Format is usually "0-3" for 4 cores
                        if '-' in present:
                            total_cores = int(present.split('-')[1]) + 1
                        else:
                            total_cores = 1
                    
                    # Get online/active CPU cores
                    with open('/sys/devices/system/cpu/online', 'r') as f:
                        online = f.read().strip()
                        # Format can be "0-3" or "0,2-3" etc
                        active_cores = 0
                        for part in online.split(','):
                            if '-' in part:
                                start, end = part.split('-')
                                active_cores += int(end) - int(start) + 1
                            else:
                                active_cores += 1
                    
                    stats['total_cores'] = total_cores
                    stats['active_cores'] = active_cores
                except:
                    stats['total_cores'] = 'N/A'
                    stats['active_cores'] = 'N/A'
                
                # Disk space without spawning df.
                try:
                    disk = os.statvfs('/')
                    total_bytes = disk.f_blocks * disk.f_frsize
                    free_bytes = disk.f_bavail * disk.f_frsize
                    used_bytes = total_bytes - (
                        disk.f_bfree * disk.f_frsize
                    )

                    def format_size(value):
                        value = float(value)
                        for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
                            if value < 1024 or unit == 'TiB':
                                if unit in ('GiB', 'TiB'):
                                    return f"{value:.1f} {unit}"
                                return f"{value:.0f} {unit}"
                            value /= 1024

                    stats['disk_total'] = format_size(total_bytes)
                    stats['disk_used'] = format_size(used_bytes)
                    stats['disk_free'] = format_size(free_bytes)
                    stats['disk_percent'] = (
                        f"{int(round((used_bytes / total_bytes) * 100))}%"
                        if total_bytes > 0
                        else '0%'
                    )
                except Exception:
                    stats['disk_free'] = 'N/A'

                # Memory usage directly from /proc/meminfo.
                try:
                    meminfo = {}

                    with open('/proc/meminfo', 'r') as f:
                        for line in f:
                            key, value = line.split(':', 1)
                            meminfo[key] = int(value.strip().split()[0])

                    total_kib = meminfo['MemTotal']
                    available_kib = meminfo.get(
                        'MemAvailable',
                        meminfo.get('MemFree', 0),
                    )
                    used_kib = max(0, total_kib - available_kib)

                    def format_memory(kib):
                        mib = kib / 1024
                        if mib >= 1024:
                            return f"{mib / 1024:.1f} GiB"
                        return f"{mib:.0f} MiB"

                    stats['memory_total'] = format_memory(total_kib)
                    stats['memory_used'] = format_memory(used_kib)
                    stats['memory_free'] = format_memory(available_kib)
                    stats['memory_percent'] = (
                        f"{int(round((used_kib / total_kib) * 100))}%"
                        if total_kib > 0
                        else '0%'
                    )

                except Exception:
                    stats['memory_used'] = 'N/A'
                    stats['memory_total'] = 'N/A'

                # Raspberry Pi model
                try:
                    model_path = Path('/proc/device-tree/model')
                    stats['model'] = model_path.read_bytes().replace(b'\x00', b'').decode().strip()
                except Exception:
                    stats['model'] = platform.machine()

                # Battery information
                try:
                    if self.app_instance.battery_monitor:
                        battery = self.app_instance.battery_monitor.get_status()
                        stats['battery_percentage'] = battery.get('percentage')
                        stats['battery_soc_precise'] = battery.get('soc_precise')
                        stats['battery_percentage_voltage'] = battery.get('percentage_voltage')
                        stats['battery_voltage'] = battery.get('voltage')
                        stats['battery_current_ma'] = battery.get('current_ma')
                        stats['battery_charging'] = battery.get('is_charging')
                        stats['battery_remaining_hours'] = battery.get(
                            'remaining_hours_estimate'
                        )
                        stats['battery_backend'] = battery.get('backend')
                except Exception as exc:
                    self.logger.debug("Battery stats unavailable: %s", exc)

                # Current PiBook screen
                try:
                    stats['current_screen'] = self.app_instance.navigation.current_screen.value
                except Exception:
                    stats['current_screen'] = 'unknown'

                stats['current_screen_label'] = _screen_label(
                    stats.get('current_screen')
                )

                # Effective power profile currently applied to the system.
                try:
                    effective_profile = getattr(
                        self.app_instance,
                        '_effective_power_profile',
                        None,
                    )
                    stats['effective_power_profile'] = (
                        effective_profile or 'unknown'
                    )
                except Exception:
                    stats['effective_power_profile'] = 'unknown'

                return jsonify(stats)
                
            except Exception as e:
                self.logger.error(f"Failed to get system stats: {e}")
                return jsonify({'error': str(e)}), 500

    def _check_port(self, ip: str, port: int) -> bool:
        """Check if a port is open on the given IP"""
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((ip, port))
            sock.close()
            return result == 0
        except:
            return False

    def _get_klipper_info(self, ip: str, hostname: str = '') -> dict:
        """Get Klipper printer info from Moonraker API"""
        import urllib.request
        import json as json_lib

        try:
            # Get printer info from Moonraker API
            base_url = f"http://{ip}:7125"

            printer_info = {
                'ip': ip,
                'hostname': hostname,
                'state': 'unknown',
                'klipper_version': None,
                'extruder_temp': None,
                'extruder_target': None,
                'bed_temp': None,
                'bed_target': None,
                'progress': None
            }

            # Get server info (includes Klipper version)
            try:
                req = urllib.request.Request(f"{base_url}/server/info", method='GET')
                req.add_header('Content-Type', 'application/json')
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json_lib.loads(response.read().decode())
                    if 'result' in data:
                        printer_info['klipper_version'] = data['result'].get('klippy_state', 'unknown')
            except Exception as e:
                self.logger.debug(f"Failed to get server info from {ip}: {e}")

            # Get printer state
            try:
                req = urllib.request.Request(f"{base_url}/printer/info", method='GET')
                req.add_header('Content-Type', 'application/json')
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json_lib.loads(response.read().decode())
                    if 'result' in data:
                        printer_info['state'] = data['result'].get('state', 'unknown')
            except Exception as e:
                self.logger.debug(f"Failed to get printer info from {ip}: {e}")

            # Get temperature data
            try:
                req = urllib.request.Request(
                    f"{base_url}/printer/objects/query?extruder&heater_bed&print_stats",
                    method='GET'
                )
                req.add_header('Content-Type', 'application/json')
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json_lib.loads(response.read().decode())
                    if 'result' in data and 'status' in data['result']:
                        status = data['result']['status']

                        # Extruder temps
                        if 'extruder' in status:
                            printer_info['extruder_temp'] = status['extruder'].get('temperature', 0)
                            printer_info['extruder_target'] = status['extruder'].get('target', 0)

                        # Bed temps
                        if 'heater_bed' in status:
                            printer_info['bed_temp'] = status['heater_bed'].get('temperature', 0)
                            printer_info['bed_target'] = status['heater_bed'].get('target', 0)

                        # Print progress
                        if 'print_stats' in status:
                            print_stats = status['print_stats']
                            state = print_stats.get('state', '')
                            if state == 'printing':
                                printer_info['state'] = 'printing'
                                printer_info['progress'] = print_stats.get('progress', 0)
                            elif state == 'complete':
                                printer_info['state'] = 'complete'
                            elif state == 'standby':
                                printer_info['state'] = 'ready'

            except Exception as e:
                self.logger.debug(f"Failed to get temperature data from {ip}: {e}")

            return printer_info

        except Exception as e:
            self.logger.error(f"Failed to get Klipper info from {ip}: {e}")
            return None

    def _get_books(self):
        """Get list of EPUB files"""
        books = []
        if os.path.exists(self.books_dir):
            for filename in sorted(os.listdir(self.books_dir)):
                if filename.lower().endswith('.epub'):
                    filepath = os.path.join(self.books_dir, filename)
                    size = os.path.getsize(filepath) / (1024 * 1024)  # MB
                    progress = None
                    manager = getattr(
                        self.app_instance,
                        'progress_manager',
                        None,
                    )

                    if manager is not None:
                        try:
                            progress = manager.get_progress_details(filepath)
                        except Exception as exc:
                            self.logger.debug(
                                "Could not load lifecycle for %s: %s",
                                filename,
                                exc,
                            )

                    if progress is None:
                        status = 'unread'
                        status_label = 'Não lido'
                        current_page = 0
                        total_pages = 0
                        rating = None
                        times_finished = 0
                        reading_seconds = 0.0
                    else:
                        status = progress.get('status', 'reading')
                        times_finished = int(
                            progress.get('times_finished', 0) or 0
                        )

                        if status == 'finished':
                            status_label = 'Lido'
                        elif status == 'reading' and times_finished > 0:
                            status_label = 'A reler'
                        elif status == 'reading':
                            status_label = 'A ler'
                        else:
                            status_label = 'Não lido'

                        current_page = int(
                            progress.get('current_page', 0) or 0
                        )
                        total_pages = int(
                            progress.get('total_pages', 0) or 0
                        )
                        rating = progress.get('rating')
                        reading_seconds = float(
                            progress.get(
                                'total_reading_seconds',
                                0.0,
                            ) or 0.0
                        )

                    if status == 'finished':
                        progress_percent = 100
                    elif total_pages > 0:
                        progress_percent = max(
                            0,
                            min(
                                100,
                                int(
                                    round(
                                        ((current_page + 1) / total_pages)
                                        * 100
                                    )
                                ),
                            ),
                        )
                    else:
                        progress_percent = 0

                    # Presentation fields for the Web Library.
                    if progress is None:
                        reading_time_label = "—"
                    else:
                        history = progress.get('reading_history') or []
                        legacy_unknown = bool(
                            status == 'finished'
                            and history
                            and history[-1].get('reading_seconds') is None
                            and reading_seconds == 0
                        )

                        if legacy_unknown:
                            reading_time_label = "Desconhecido"
                        else:
                            seconds = max(0, int(round(reading_seconds)))
                            hours, remainder = divmod(seconds, 3600)
                            minutes, seconds = divmod(remainder, 60)

                            if hours:
                                reading_time_label = (
                                    f"{hours} h {minutes} min"
                                )
                            elif minutes:
                                reading_time_label = f"{minutes} min"
                            elif seconds:
                                reading_time_label = f"{seconds} s"
                            else:
                                reading_time_label = "0 min"

                    rating_value = None
                    try:
                        if rating is not None:
                            rating_value = max(
                                0,
                                min(10, int(rating)),
                            )
                    except (TypeError, ValueError):
                        rating_value = None

                    rating_stars = []
                    for index in range(5):
                        threshold = index * 2

                        if rating_value is None:
                            star_state = 'empty'
                        elif rating_value >= threshold + 2:
                            star_state = 'full'
                        elif rating_value == threshold + 1:
                            star_state = 'half'
                        else:
                            star_state = 'empty'

                        rating_stars.append(star_state)

                    rating_label = (
                        f"{rating_value}/10"
                        if rating_value is not None
                        else "Sem avaliação"
                    )

                    if status == 'finished':
                        open_label = 'Ver resumo'
                    elif status == 'reading':
                        open_label = 'Continuar'
                    else:
                        open_label = 'Abrir'

                    books.append({
                        'filename': filename,
                        'path': os.path.abspath(filepath),
                        'title': get_epub_title(filepath),
                        'size': f"{size:.2f} MB",
                        'reading_status': status,
                        'reading_status_label': status_label,
                        'current_page': current_page,
                        'current_page_display': (
                            current_page + 1
                            if total_pages > 0
                            else 0
                        ),
                        'total_pages': total_pages,
                        'progress_percent': progress_percent,
                        'rating': rating_value,
                        'rating_label': rating_label,
                        'rating_stars': rating_stars,
                        'times_finished': times_finished,
                        'reading_seconds': reading_seconds,
                        'reading_time_label': reading_time_label,
                        'open_label': open_label,
                        'can_reset_position': (
                            status == 'reading'
                            and current_page > 0
                        ),
                        'can_reread': (
                            status == 'finished'
                        ),
                    })
        return books


    def _load_settings(self, settings_file: str = None) -> dict:
        """Load settings through the shared SettingsManager."""
        manager = SettingsManager(
            str(self.settings_path),
            logger=self.logger,
        )
        settings = manager.get_all()
        settings.pop('boot_cores', None)
        return settings

    def _save_settings(self, settings_data):
        """Save settings atomically to the PiBook project directory."""
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.settings_path.with_suffix('.json.tmp')
        temp_path.write_text(
            json.dumps(settings_data, indent=2, ensure_ascii=False) + '\n',
            encoding='utf-8'
        )
        temp_path.replace(self.settings_path)

    def _reload_library(self, render: bool = True):
        """Reload the EPUB list and optionally refresh the e-paper library."""
        self.app_instance.library_screen.load_books(self.books_dir)
        if (
            render
            and getattr(self.app_instance, 'running', False)
            and getattr(self.app_instance.navigation.current_screen, 'value', '') == 'library'
        ):
            self.app_instance._render_current_screen()

    def run(self):
        """Start the web server in a separate thread"""
        import threading
        thread = threading.Thread(target=self._run_server, daemon=True)
        thread.start()
        self.logger.info(f"Web server started on port {self.port}")

    def _run_server(self):
        """Internal method to run Flask server"""
        self.flask_app.run(host='0.0.0.0', port=self.port, debug=False, use_reloader=False, threaded=True)

