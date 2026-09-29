"""
EPUB renderer using direct Pillow text rendering with RICH TEXT support.
Extracts HTML and preserves basic formatting (Bold, Italic, Headers) 
while rendering directly with TTF fonts for maximum sharpness on e-ink.
"""

import ebooklib
from ebooklib import epub
from PIL import Image, ImageDraw, ImageFont
import logging
import os
import re
from io import BytesIO
from typing import List, NamedTuple, Union
# BeautifulSoup is imported lazily only when layout parsing is required.
from src.reader.epub_repair import read_epub_tolerant
from src.reader.layout_cache import (
    cache_path as layout_cache_path,
    layout_signature,
)

# Optional: SVG support (requires cairosvg)
# CairoSVG is imported lazily only when an actual SVG image is found.
# This avoids loading Cairo/CFFI for normal raster-only EPUBs.

# Define a token structure for rich text
class TextToken(NamedTuple):
    text: str
    style: str  # 'normal', 'bold', 'italic', 'bold_italic', 'h1', 'h2', 'center'
    new_paragraph: bool = False
    align: str = 'left'  # 'left', 'center', 'right'

# Define an image token for embedded images
class ImageToken(NamedTuple):
    image: Image.Image
    max_width: int  # Maximum width in pixels
    max_height: int  # Maximum height in pixels
    new_paragraph: bool = True

# Define a table token for HTML tables
class TableToken(NamedTuple):
    rows: List[List[str]]  # 2D array of cell contents
    max_width: int  # Maximum width for table
    new_paragraph: bool = True

class LazyPageStore:
    """Thread-safe read-only page store backed by SQLite."""

    def __init__(self, cache_path: str):
        import sqlite3
        import threading

        self.cache_path = cache_path
        self._lock = threading.RLock()

        # Read metadata with a short-lived connection. Do not keep a SQLite
        # connection tied to the thread that happened to open the book.
        with sqlite3.connect(cache_path) as conn:
            meta = dict(
                conn.execute(
                    "SELECT key, value FROM meta"
                ).fetchall()
            )

        self.layout_signature = meta.get(
            "layout_signature",
            "",
        )
        self.page_count = int(
            meta.get("page_count", "0")
        )

        # Small RAM cache for repeated/adjacent pages.
        self._page_cache = {}
        self._page_cache_order = []
        self._page_cache_limit = 4

    def __len__(self):
        return self.page_count

    def __getitem__(self, page_num):
        import pickle
        import sqlite3

        if isinstance(page_num, slice):
            start, stop, step = page_num.indices(
                self.page_count
            )
            return [
                self[i]
                for i in range(start, stop, step)
            ]

        if page_num < 0:
            page_num += self.page_count

        if not 0 <= page_num < self.page_count:
            raise IndexError("page index out of range")

        # Rendering can be requested by GPIO, web control and background
        # battery-refresh threads. Keep both the RAM cache and SQLite access
        # thread-safe.
        with self._lock:
            cached = self._page_cache.get(page_num)
            if cached is not None:
                return cached

            # Short-lived connection: SQLite objects never cross threads.
            with sqlite3.connect(self.cache_path) as conn:
                row = conn.execute(
                    "SELECT data FROM pages WHERE page_num = ?",
                    (page_num,),
                ).fetchone()

            if row is None:
                raise IndexError(
                    f"page {page_num} missing from cache"
                )

            page = pickle.loads(row[0])

            self._page_cache[page_num] = page
            self._page_cache_order.append(page_num)

            while (
                len(self._page_cache_order)
                > self._page_cache_limit
            ):
                old = self._page_cache_order.pop(0)
                self._page_cache.pop(old, None)

            return page

    def close(self):
        # There is no persistent SQLite connection to close.
        with self._lock:
            self._page_cache.clear()
            self._page_cache_order.clear()


class PillowTextRenderer:
    """
    EPUB renderer using direct Pillow text drawing with Rich Text support.
    """

    def __init__(self, epub_path: str, width: int = 800, height: int = 480, zoom_factor: float = 1.0, progress_callback = None):
        self.logger = logging.getLogger(__name__)
        self.epub_path = epub_path
        self.width = width
        self.height = height
        self.zoom_factor = zoom_factor
        self.progress_callback = progress_callback
        
        # Layout settings
        self.margin_left = 30
        self.margin_right = 30
        self.margin_top = 30
        self.margin_bottom = 40
        self.line_spacing = 1.3
        self.paragraph_spacing = 5    # Reduced for book-like look
        self.paragraph_indent = 40    # Indent for new paragraphs
        
        # Font sizes
        self.base_font_size = int(18 * zoom_factor)
        self.header_font_size = int(24 * zoom_factor)
        
        # Calculate text area
        self.text_width = width - self.margin_left - self.margin_right
        self.text_height = height - self.margin_top - self.margin_bottom
        
        # Load fonts map
        self.fonts = {}
        self._load_fonts()
        
        # Book content
        self.book = None
        self.pages = []  # List of pages, each page is a list of render items (text or image)
        self.page_count = 0
        self.images = {}  # Cache for EPUB images: {src_path: PIL.Image}
        self.custom_fonts = {}  # Cache for EPUB embedded fonts: {font_name: font_path}
        
        try:
            self._load_epub()
        except Exception as e:
            self.logger.error(f"Failed to load EPUB: {e}")
            raise

    def _load_fonts(self):
        """Load specific TrueType fonts for styles"""
        # Font search paths with proper file names
        font_candidates = [
            # DejaVu Serif (common on Raspberry Pi)
            {
                'normal': '/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf',
                'bold': '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf',
                'italic': '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf',
                'bold_italic': '/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf',
            },
            # Liberation Serif
            {
                'normal': '/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf',
                'bold': '/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf',
                'italic': '/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf',
                'bold_italic': '/usr/share/fonts/truetype/liberation/LiberationSerif-BoldItalic.ttf',
            },
            # Windows fonts
            {
                'normal': 'C:/Windows/Fonts/times.ttf',
                'bold': 'C:/Windows/Fonts/timesbd.ttf',
                'italic': 'C:/Windows/Fonts/timesi.ttf',
                'bold_italic': 'C:/Windows/Fonts/timesbi.ttf',
            },
        ]

        # Find first available font family
        font_paths = None
        for candidate in font_candidates:
            if os.path.exists(candidate['normal']):
                font_paths = candidate
                self.logger.info(f"Using font: {candidate['normal']}")
                break

        if not font_paths:
            self.logger.warning("No TrueType fonts found, using default bitmap font")
            default = ImageFont.load_default()
            self.fonts = {k: default for k in ['normal', 'bold', 'italic', 'bold_italic', 'h1', 'h2']}
            return

        # Load fonts with fallback to normal if variants don't exist
        def load_font(style, size):
            path = font_paths.get(style, font_paths['normal'])
            if not os.path.exists(path):
                path = font_paths['normal']  # Fallback to normal
            try:
                return ImageFont.truetype(path, size)
            except Exception as e:
                self.logger.warning(f"Failed to load {path}: {e}")
                return ImageFont.load_default()

        self.fonts['normal'] = load_font('normal', self.base_font_size)
        self.fonts['bold'] = load_font('bold', self.base_font_size)
        self.fonts['italic'] = load_font('italic', self.base_font_size)
        self.fonts['bold_italic'] = load_font('bold_italic', self.base_font_size)
        self.fonts['h1'] = load_font('bold', self.header_font_size)
        self.fonts['h2'] = load_font('bold', int(self.header_font_size * 0.9))

    def _get_cache_path(self) -> str:
        """Get path to the cache for this exact renderer layout."""
        return str(
            layout_cache_path(
                self.epub_path,
                self.width,
                self.height,
                self.zoom_factor,
            )
        )

    def _load_cache(self) -> bool:
        """Try to load a paginated SQLite layout cache lazily."""
        cache_path = self._get_cache_path()

        if not os.path.exists(cache_path):
            return False

        try:
            # A cache older than the source EPUB is stale.
            if (
                os.path.getmtime(cache_path)
                < os.path.getmtime(self.epub_path)
            ):
                return False

            pages = LazyPageStore(cache_path)

            expected_signature = layout_signature(
                self.width,
                self.height,
                self.zoom_factor,
            )

            if pages.layout_signature != expected_signature:
                pages.close()
                self.logger.info(
                    "Ignoring incompatible layout cache: %s",
                    cache_path,
                )
                return False

            self.pages = pages
            self.page_count = pages.page_count

            self.logger.info(
                "Loaded lazy layout cache: %s",
                cache_path,
            )
            return True

        except Exception as e:
            self.logger.warning(
                f"Failed to load lazy cache: {e}"
            )

        return False

    def _save_cache(self):
        """Save layout as a paginated SQLite cache."""
        import pickle
        import sqlite3
        import tempfile

        cache_path = self._get_cache_path()

        # Each renderer gets a private temporary SQLite file. This keeps
        # concurrent cold-open/background preparation safe: only a fully
        # written database is published through the final atomic os.replace().
        temp_dir = os.path.dirname(cache_path) or "."
        os.makedirs(temp_dir, exist_ok=True)
        temp_handle = tempfile.NamedTemporaryFile(
            prefix=os.path.basename(cache_path) + ".",
            suffix=".tmp",
            dir=temp_dir,
            delete=False,
        )
        temp_path = temp_handle.name
        temp_handle.close()

        try:
            conn = sqlite3.connect(temp_path)

            try:
                # The database is disposable/rebuildable and only becomes
                # visible after an atomic os.replace(), so journaling during
                # construction would only add unnecessary work.
                conn.execute("PRAGMA journal_mode=OFF")
                conn.execute("PRAGMA synchronous=OFF")

                conn.execute(
                    "CREATE TABLE meta ("
                    "key TEXT PRIMARY KEY, "
                    "value TEXT NOT NULL"
                    ")"
                )
                conn.execute(
                    "CREATE TABLE pages ("
                    "page_num INTEGER PRIMARY KEY, "
                    "data BLOB NOT NULL"
                    ")"
                )

                conn.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    (
                        "layout_signature",
                        layout_signature(
                            self.width,
                            self.height,
                            self.zoom_factor,
                        ),
                    ),
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    (
                        "page_count",
                        str(self.page_count),
                    ),
                )

                for page_num, page in enumerate(self.pages):
                    blob = pickle.dumps(
                        page,
                        protocol=pickle.HIGHEST_PROTOCOL,
                    )
                    conn.execute(
                        "INSERT INTO pages(page_num, data) "
                        "VALUES (?, ?)",
                        (
                            page_num,
                            sqlite3.Binary(blob),
                        ),
                    )

                conn.commit()

            finally:
                conn.close()

            os.replace(temp_path, cache_path)

            self.logger.info(
                "Saved lazy layout cache: %s",
                cache_path,
            )

        except Exception as e:
            self.logger.warning(
                f"Failed to save lazy cache: {e}"
            )

            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass

    def _load_epub(self):
        if self.progress_callback:
            self.progress_callback(5.0, "Opening EPUB file...")

        # Try cache first.
        if self._load_cache():
            # Images used by cached pages are already serialized inside the
            # paginated layout cache, so reopening the EPUB just to extract
            # images would waste time.
            #
            # TTF/OTF fonts are different: render_page() still needs the actual
            # font objects. Detect those cheaply from the ZIP directory and
            # only reopen the EPUB when a loadable embedded font is present.
            from zipfile import ZipFile

            has_embedded_fonts = False

            try:
                with ZipFile(self.epub_path) as archive:
                    has_embedded_fonts = any(
                        name.lower().endswith(('.ttf', '.otf'))
                        for name in archive.namelist()
                    )
            except Exception as e:
                # Conservative fallback: if the cheap ZIP inspection fails,
                # reopen through the tolerant EPUB reader as before.
                self.logger.debug(
                    f"Could not inspect EPUB fonts cheaply: {e}"
                )
                has_embedded_fonts = True

            if has_embedded_fonts:
                self.book = read_epub_tolerant(
                    self.epub_path,
                    logger=self.logger,
                )
                self._extract_fonts()

            return

        self.book = read_epub_tolerant(self.epub_path, logger=self.logger)

        if self.progress_callback:
            self.progress_callback(10.0, "Extracting images and fonts...")

        # First pass: Extract and cache all images and fonts from EPUB
        self._extract_images()
        self._extract_fonts()

        all_tokens = []

        docs = list(self.book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
        total_docs = len(docs)
        
        for idx, item in enumerate(docs):
            if self.progress_callback:
                # Map chapter parsing from 15% to 80%
                percent_done = 15.0 + (65.0 * (idx / max(1, total_docs)))
                self.progress_callback(percent_done, f"Parsing chapter {idx + 1} of {total_docs}...")

            try:
                content = item.get_content()
                try:
                    html = content.decode('utf-8')
                except UnicodeDecodeError:
                    html = content.decode('latin-1', errors='ignore')

                tokens = self._parse_html(html)
                if tokens:
                    all_tokens.extend(tokens)
                    all_tokens.append(TextToken("", "normal", new_paragraph=True))
            except Exception as e:
                self.logger.warning(f"Chapter error: {e}")

        if self.progress_callback:
            self.progress_callback(85.0, "Formatting pages...")

        self._reflow_pages(all_tokens)
        self._save_cache()
        self.logger.info(f"Loaded EPUB: {self.page_count} pages")

    def _extract_images(self):
        """Extract all images from EPUB and cache them (including SVG conversion)"""
        for item in self.book.get_items_of_type(ebooklib.ITEM_IMAGE):
            try:
                img_name = item.get_name()
                img_data = item.get_content()

                # Check if this is an SVG file
                if img_name.lower().endswith('.svg'):
                    try:
                        import cairosvg
                    except ImportError:
                        self.logger.warning(
                            f"SVG support not available (install cairosvg): {img_name}"
                        )
                        continue

                    try:
                        png_data = cairosvg.svg2png(
                            bytestring=img_data,
                            output_width=800,
                        )
                        img = Image.open(BytesIO(png_data))
                        self.logger.debug(f"Converted SVG to raster: {img_name}")
                    except Exception as e:
                        self.logger.warning(f"Failed to convert SVG {img_name}: {e}")
                        continue
                else:
                    # Regular raster image (PNG, JPG, GIF, etc.)
                    img = Image.open(BytesIO(img_data))

                # Convert to appropriate mode for e-ink
                if img.mode == 'RGBA':
                    # Create white background for transparency
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[3])  # Use alpha channel as mask
                    img = background
                elif img.mode not in ['RGB', 'L', '1']:
                    img = img.convert('RGB')

                # Store with filename as key
                self.images[img_name] = img
                self.logger.debug(f"Extracted image: {img_name} ({img.size[0]}x{img.size[1]})")
            except Exception as e:
                self.logger.warning(f"Failed to extract image {item.get_name()}: {e}")

        self.logger.info(f"Extracted {len(self.images)} images from EPUB")

    def _extract_fonts(self):
        """Extract embedded fonts from EPUB"""
        import tempfile

        for item in self.book.get_items():
            # Check if this is a font file (TTF, OTF, WOFF, etc.)
            item_name = item.get_name().lower()
            if any(item_name.endswith(ext) for ext in ['.ttf', '.otf', '.woff', '.woff2']):
                try:
                    font_data = item.get_content()

                    # WOFF/WOFF2 fonts need conversion (skip for now - complex)
                    if item_name.endswith(('.woff', '.woff2')):
                        self.logger.debug(f"Skipping WOFF font (conversion not supported): {item.get_name()}")
                        continue

                    # Save TTF/OTF to temp file (PIL needs file path, not bytes)
                    temp_font = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(item_name)[1])
                    temp_font.write(font_data)
                    temp_font.close()

                    # Extract font family name from filename
                    font_name = os.path.splitext(os.path.basename(item_name))[0]
                    self.custom_fonts[font_name] = temp_font.name

                    self.logger.debug(f"Extracted font: {font_name} from {item.get_name()}")
                except Exception as e:
                    self.logger.warning(f"Failed to extract font {item.get_name()}: {e}")

        if self.custom_fonts:
            self.logger.info(f"Extracted {len(self.custom_fonts)} custom fonts from EPUB")
            # Try to use custom fonts if available
            self._try_load_custom_fonts()

    def _try_load_custom_fonts(self):
        """Try to load custom EPUB fonts for rendering"""
        # Look for fonts that might replace our default fonts
        for font_name, font_path in self.custom_fonts.items():
            font_name_lower = font_name.lower()

            # Try to match to our font styles
            try:
                if 'bolditalic' in font_name_lower or 'bold-italic' in font_name_lower:
                    self.fonts['bold_italic'] = ImageFont.truetype(font_path, self.base_font_size)
                    self.logger.info(f"Using custom bold-italic font: {font_name}")
                elif 'bold' in font_name_lower:
                    self.fonts['bold'] = ImageFont.truetype(font_path, self.base_font_size)
                    self.fonts['h1'] = ImageFont.truetype(font_path, self.header_font_size)
                    self.fonts['h2'] = ImageFont.truetype(font_path, int(self.header_font_size * 0.9))
                    self.logger.info(f"Using custom bold font: {font_name}")
                elif 'italic' in font_name_lower:
                    self.fonts['italic'] = ImageFont.truetype(font_path, self.base_font_size)
                    self.logger.info(f"Using custom italic font: {font_name}")
                elif 'regular' in font_name_lower or 'normal' in font_name_lower or len(self.custom_fonts) == 1:
                    # Use as default font if it's marked as regular/normal or it's the only font
                    self.fonts['normal'] = ImageFont.truetype(font_path, self.base_font_size)
                    self.logger.info(f"Using custom normal font: {font_name}")
            except Exception as e:
                self.logger.warning(f"Failed to load custom font {font_name}: {e}")

    def _parse_html(self, html: str) -> List[Union[TextToken, ImageToken, TableToken]]:
        """Parse HTML into flat list of tokens with styles."""
        # Layout parsing is only needed when the persistent layout cache misses.
        #
        # Use lxml directly rather than building an intermediate BeautifulSoup
        # tree. On the large validation EPUB this produces the same compact
        # source-token count while cutting parsing time substantially.
        from lxml import html as lxml_html

        tokens = []

        # _parse_html currently receives decoded text from the EPUB loader.
        # Re-encode as UTF-8 and explicitly tell lxml the encoding so XHTML
        # declarations in the original document cannot conflict with Python's
        # already-decoded Unicode string.
        if isinstance(html, str):
            html_bytes = html.encode('utf-8')
        else:
            html_bytes = bytes(html)

        parser = lxml_html.HTMLParser(
            encoding='utf-8',
            recover=True,
        )

        try:
            root = lxml_html.document_fromstring(
                html_bytes,
                parser=parser,
            )
        except Exception as e:
            self.logger.warning(f"Failed to parse EPUB document: {e}")
            return tokens

        def tag_name(node):
            tag = getattr(node, 'tag', '')
            if not isinstance(tag, str):
                return ''

            if '}' in tag:
                tag = tag.rsplit('}', 1)[-1]

            return tag.lower()

        # Match the previous BeautifulSoup behaviour: process the document body
        # when present rather than metadata in <head>.
        bodies = root.xpath('//body')
        start_node = bodies[0] if bodies else root

        def add_text(text, current_style, current_align):
            if text is None:
                return

            # Keep each DOM text node as one compact source token. Word/space
            # expansion happens lazily during reflow.
            text = str(text).replace('\n', ' ')
            if not text.strip():
                return

            tokens.append(
                TextToken(
                    text,
                    current_style,
                    False,
                    current_align,
                )
            )

        def mark_paragraph_end():
            if (
                tokens
                and isinstance(tokens[-1], TextToken)
                and not tokens[-1].new_paragraph
            ):
                tokens[-1] = tokens[-1]._replace(
                    new_paragraph=True
                )

        def process_node(node, current_style='normal', current_align='left'):
            name = tag_name(node)

            # Metadata/non-content elements were removed by BeautifulSoup in the
            # previous implementation. Skip them directly here.
            if name in {
                'head',
                'script',
                'style',
                'title',
                'meta',
            }:
                return

            # Handle table tags.
            if name == 'table':
                rows = []

                for tr in node.iter():
                    if tag_name(tr) != 'tr':
                        continue

                    cells = []

                    for cell in tr.iter():
                        if tag_name(cell) not in {'td', 'th'}:
                            continue

                        # Equivalent to BeautifulSoup get_text(separator=' ',
                        # strip=True): collect non-empty text fragments and join
                        # them with a single space.
                        pieces = []

                        for value in cell.itertext():
                            value = str(value).strip()
                            if value:
                                pieces.append(value)

                        cells.append(' '.join(pieces))

                    if cells:
                        rows.append(cells)

                if rows:
                    tokens.append(
                        TableToken(
                            rows,
                            self.text_width,
                        )
                    )
                    self.logger.debug(
                        f"Added table token: {len(rows)} rows"
                    )

                return

            # Handle image tags.
            if name == 'img':
                src = node.get('src', '')

                if src:
                    img_path = src.split('/')[-1]
                    img = None

                    for key in self.images.keys():
                        if (
                            key.endswith(img_path)
                            or img_path in key
                        ):
                            img = self.images[key]
                            break

                    if img is not None:
                        max_img_width = self.text_width
                        max_img_height = int(
                            self.text_height * 0.6
                        )

                        tokens.append(
                            ImageToken(
                                img,
                                max_img_width,
                                max_img_height,
                            )
                        )
                        self.logger.debug(
                            f"Added image token: {img_path}"
                        )
                    else:
                        self.logger.warning(
                            f"Image not found in EPUB: {src}"
                        )

                return

            style = current_style
            align = current_align

            is_block = name in {
                'p',
                'div',
                'h1',
                'h2',
                'h3',
                'h4',
                'br',
                'li',
            }

            # Check for CSS text-align in style attribute.
            node_style = node.get('style', '') or ''

            if 'text-align' in node_style:
                if 'center' in node_style:
                    align = 'center'
                elif 'right' in node_style:
                    align = 'right'
                elif 'left' in node_style:
                    align = 'left'

            if name == 'center':
                align = 'center'

            # Determine style.
            if name in {'b', 'strong'}:
                style = (
                    'bold_italic'
                    if 'italic' in style
                    else 'bold'
                )
            elif name in {'i', 'em'}:
                style = (
                    'bold_italic'
                    if 'bold' in style
                    else 'italic'
                )
            elif name == 'h1':
                style = 'h1'
                align = 'center'
            elif name == 'h2':
                style = 'h2'
                align = 'center'
            elif name in {'h3', 'h4'}:
                style = 'bold'

            if is_block:
                mark_paragraph_end()

            # lxml stores the first text fragment in node.text and subsequent
            # sibling text in child.tail. Processing both recreates DOM order
            # without allocating BeautifulSoup wrapper objects.
            add_text(
                node.text,
                style,
                align,
            )

            for child in node:
                process_node(
                    child,
                    style,
                    align,
                )
                add_text(
                    child.tail,
                    style,
                    align,
                )

            if is_block:
                mark_paragraph_end()

        process_node(start_node)
        return tokens

    def _reflow_pages(self, tokens: List[TextToken]):
        """Reflow tokens into pages based on width/height"""
        self.logger.info(f"Reflowing {len(tokens)} tokens...")
        self.pages = []
        current_page = []
        current_y = self.margin_top
        current_x = self.margin_left + self.paragraph_indent # Start indented
        
        # Pre-calculate font heights to avoid per-word overhead.
        font_metrics = {}
        for style, font in self.fonts.items():
            bbox = font.getbbox("Ay")
            font_metrics[style] = (
                bbox[3] - bbox[1] if bbox else self.base_font_size
            )

        # A normal book repeats the same words and whitespace thousands of
        # times. Pillow font measurement is relatively expensive on a Pi Zero,
        # so measure each (style, text) combination only once per reflow.
        width_cache = {}

        # Hot-path helpers: roughly half of the expanded layout sequence in a
        # normal novel consists of literal single spaces.
        split_whitespace = re.compile(r'(\s+)').split
        space_widths = {}
        for style, font in self.fonts.items():
            try:
                space_widths[style] = font.getlength(" ")
            except Exception:
                space_widths[style] = self.base_font_size * 0.6

        def measure_width(text, style, font):
            key = (style, text)
            cached = width_cache.get(key)
            if cached is not None:
                return cached

            try:
                value = font.getlength(text)
            except Exception:
                value = len(text) * self.base_font_size * 0.6

            width_cache[key] = value
            return value

        # Helper to finish a line
        def finish_line(line_items, y, h):
            nonlocal current_y, current_page
            if y + h > self.height - self.margin_bottom:
                self.pages.append(current_page)
                current_page = []
                current_y = self.margin_top
                y = current_y
            
            # Compact only text fragments separated by real plain-space
            # tokens already present in the reflow sequence. Adjacent
            # fragments (commonly punctuation) remain separate so FreeType
            # kerning/rasterization stays pixel-identical to the old renderer.
            run_x = None
            run_text = ""
            run_style = None
            pending_space = ""
            pending_style = None

            def flush_run():
                nonlocal run_x, run_text, run_style
                if run_x is not None:
                    current_page.append(
                        (run_x, y, run_text, run_style)
                    )
                run_x = None
                run_text = ""
                run_style = None

            for txt, style, x in line_items:
                is_single_space = txt == " "
                if is_single_space or txt.isspace():
                    # Only literal spaces are safe to fold into a draw call.
                    # Tabs/other whitespace keep the next fragment separate.
                    if (
                        run_x is not None
                        and txt
                        and (
                            is_single_space
                            or txt.strip(" ") == ""
                        )
                        and style == run_style
                        and (
                            pending_style is None
                            or pending_style == style
                        )
                    ):
                        pending_space += txt
                        pending_style = style
                    else:
                        pending_space = ""
                        pending_style = None
                    continue

                if run_x is None:
                    run_x = x
                    run_text = txt
                    run_style = style

                elif (
                    pending_space
                    and style == run_style
                    and pending_style == run_style
                ):
                    run_text += pending_space + txt

                else:
                    flush_run()
                    run_x = x
                    run_text = txt
                    run_style = style

                pending_space = ""
                pending_style = None

            flush_run()
            current_y += int(h * self.line_spacing)
            return current_y

        current_line = [] # (text, style, x)
        current_line_max_h = 0
        
        count = 0
        total_source_tokens = len(tokens)
        last_progress_source = 0

        self.logger.info(
            "Reflow source tokens: %d",
            total_source_tokens,
        )

        def report_source_progress(source_index):
            nonlocal last_progress_source

            if (
                source_index != last_progress_source
                and source_index % 25 == 0
            ):
                last_progress_source = source_index
                self.logger.debug(
                    "Reflow progress: source %d/%d, layout tokens %d",
                    source_index,
                    total_source_tokens,
                    count,
                )
                if self.progress_callback:
                    percent_done = 85.0 + (
                        14.0
                        * (
                            source_index
                            / max(1, total_source_tokens)
                        )
                    )
                    self.progress_callback(
                        percent_done,
                        f"Formatting {source_index}/{total_source_tokens}...",
                    )

        for source_index, source_token in enumerate(tokens, 1):
            # Non-text tokens are already compact and occur only once.
            if isinstance(source_token, TableToken):
                count += 1
                report_source_progress(source_index)

                # Finish current line first
                if current_line:
                    current_y = finish_line(
                        current_line,
                        current_y,
                        current_line_max_h or self.base_font_size,
                    )
                    current_line = []
                    current_line_max_h = 0

                rows = source_token.rows
                if rows:
                    num_cols = max(len(row) for row in rows)
                    col_width = source_token.max_width // num_cols
                    row_height = int(self.base_font_size * 1.5)
                    table_height = len(rows) * row_height

                    if current_y + table_height > self.height - self.margin_bottom:
                        self.pages.append(current_page)
                        current_page = []
                        current_y = self.margin_top

                    current_page.append(
                        (
                            self.margin_left,
                            current_y,
                            rows,
                            'TABLE',
                            col_width,
                            row_height,
                        )
                    )
                    current_y += (
                        table_height
                        + self.paragraph_spacing * 2
                    )

                current_x = self.margin_left + self.paragraph_indent
                continue

            if isinstance(source_token, ImageToken):
                count += 1
                report_source_progress(source_index)

                # Finish current line first
                if current_line:
                    current_y = finish_line(
                        current_line,
                        current_y,
                        current_line_max_h or self.base_font_size,
                    )
                    current_line = []
                    current_line_max_h = 0

                img = source_token.image
                img_w, img_h = img.size

                scale = min(
                    source_token.max_width / img_w,
                    source_token.max_height / img_h,
                    1.0,
                )
                new_w = int(img_w * scale)
                new_h = int(img_h * scale)

                if current_y + new_h > self.height - self.margin_bottom:
                    self.pages.append(current_page)
                    current_page = []
                    current_y = self.margin_top

                img_x = (
                    self.margin_left
                    + (self.text_width - new_w) // 2
                )
                current_page.append(
                    (
                        img_x,
                        current_y,
                        img,
                        'IMAGE',
                        new_w,
                        new_h,
                    )
                )
                current_y += (
                    new_h
                    + self.paragraph_spacing * 2
                )

                current_x = self.margin_left + self.paragraph_indent
                continue

            # Text source runs are expanded directly here. This preserves the
            # exact v4 word/whitespace sequence but removes the generator and
            # per-fragment proxy object.
            if source_token.text == "":
                parts = ("",)
            else:
                parts = [
                    part
                    for part in split_whitespace(
                        source_token.text,
                    )
                    if part
                ]

            last_index = len(parts) - 1
            progress_reported = False

            style = source_token.style
            font = self.fonts.get(style, self.fonts['normal'])
            font_h = font_metrics.get(
                style,
                self.base_font_size,
            )

            for part_index, text in enumerate(parts):
                count += 1

                if not progress_reported:
                    report_source_progress(source_index)
                    progress_reported = True

                new_paragraph = (
                    source_token.new_paragraph
                    and part_index == last_index
                )

                # Preserve v4 behaviour exactly: header handling happens for
                # every expanded fragment carrying h1/h2 style.
                if style in ['h1', 'h2']:
                    if current_line:
                        current_y = finish_line(
                            current_line,
                            current_y,
                            current_line_max_h or font_h,
                        )
                        current_line = []
                        current_line_max_h = 0
                    current_x = self.margin_left
                    current_y += self.paragraph_spacing * 2

                if text == " ":
                    width = space_widths[style]
                else:
                    width = measure_width(text, style, font)

                if current_x + width > self.width - self.margin_right:
                    current_y = finish_line(
                        current_line,
                        current_y,
                        current_line_max_h or font_h,
                    )
                    current_line = []
                    current_line_max_h = 0
                    current_x = self.margin_left

                    # Preserve original behaviour: whitespace that itself
                    # causes wrapping is discarded on the new line.
                    if text.isspace():
                        continue

                current_line.append(
                    (
                        text,
                        style,
                        current_x,
                    )
                )
                current_x += width
                if font_h > current_line_max_h:
                    current_line_max_h = font_h

                if new_paragraph:
                    current_y = finish_line(
                        current_line,
                        current_y,
                        current_line_max_h or font_h,
                    )
                    current_line = []
                    current_line_max_h = 0
                    current_x = (
                        self.margin_left
                        + self.paragraph_indent
                    )
                    current_y += self.paragraph_spacing

        self.logger.info(
            "Expanded reflow sequence: %d source tokens -> %d layout tokens",
            total_source_tokens,
            count,
        )

        # Finish last page
        if current_line:
             finish_line(current_line, current_y, current_line_max_h)
        if current_page:
            self.pages.append(current_page)
        
        if not self.pages:
            self.pages.append([])
            self.page_count = 1
        else:
            self.page_count = len(self.pages)
        self.logger.info(f"Reflow complete: {self.page_count} pages")

    def render_page(self, page_num: int, show_page_number: bool = True) -> Image.Image:
        image = Image.new('1', (self.width, self.height), 1)
        draw = ImageDraw.Draw(image)

        if 0 <= page_num < len(self.pages):
            for item in self.pages[page_num]:
                # Check if this is an image item (has 6 elements)
                if len(item) == 6:
                    x, y, data, style_marker, w, h = item

                    if style_marker == 'IMAGE':
                        # Resize and convert image to 1-bit for e-ink
                        resized_img = data.resize((w, h), Image.Resampling.LANCZOS)

                        # Convert to grayscale first, then to 1-bit with dithering for better quality
                        if resized_img.mode != 'L':
                            resized_img = resized_img.convert('L')

                        # Convert to 1-bit with Floyd-Steinberg dithering for better image quality
                        bw_img = resized_img.convert('1', dither=Image.Dither.FLOYDSTEINBERG)

                        # Paste the image onto the page
                        image.paste(bw_img, (x, y))

                    elif style_marker == 'TABLE':
                        # Render table with borders
                        rows = data  # data contains the table rows
                        col_width = w  # w contains column width
                        row_height = h  # h contains row height

                        font = self.fonts['normal']
                        num_cols = max(len(row) for row in rows) if rows else 0
                        table_width = col_width * num_cols
                        table_height = row_height * len(rows)

                        # Draw table border
                        draw.rectangle([x, y, x + table_width, y + table_height], outline=0, width=2)

                        # Draw rows and cells
                        for row_idx, row in enumerate(rows):
                            row_y = y + (row_idx * row_height)

                            # Draw horizontal line
                            if row_idx > 0:
                                draw.line([(x, row_y), (x + table_width, row_y)], fill=0, width=1)

                            for col_idx, cell in enumerate(row):
                                col_x = x + (col_idx * col_width)

                                # Draw vertical line
                                if col_idx > 0:
                                    draw.line([(col_x, y), (col_x, y + table_height)], fill=0, width=1)

                                # Draw cell text (truncate if too long)
                                cell_text = str(cell)
                                max_chars = int(col_width / (self.base_font_size * 0.6))
                                if len(cell_text) > max_chars:
                                    cell_text = cell_text[:max_chars-3] + '...'

                                # Center text in cell
                                text_x = col_x + 5  # Small padding
                                text_y = row_y + (row_height - self.base_font_size) // 2
                                draw.text((text_x, text_y), cell_text, font=font, fill=0)
                else:
                    # Text item (4 elements)
                    x, y, text, style = item
                    font = self.fonts.get(style, self.fonts['normal'])
                    draw.text((x, y), text, font=font, fill=0)

        if show_page_number:
            page_text = f"Page {page_num + 1} of {self.page_count}"
            font = self.fonts['normal']
            try:
                bbox = draw.textbbox((0, 0), page_text, font=font)
                w = bbox[2] - bbox[0]
                draw.text(((self.width - w)//2, self.height - 25), page_text, font=font, fill=0)
            except:
                pass

        return image

    def get_page_count(self): return self.page_count
    def get_metadata(self): return {'title': 'Rich Text', 'author': '?'}
    def close(self):
        """Release renderer resources."""
        if isinstance(self.pages, LazyPageStore):
            self.pages.close()

        # Remove temporary TTF/OTF files extracted from EPUBs.
        for font_path in self.custom_fonts.values():
            try:
                if os.path.exists(font_path):
                    os.remove(font_path)
            except Exception as e:
                self.logger.debug(
                    f"Failed to remove temporary font {font_path}: {e}"
                )

        self.custom_fonts.clear()
