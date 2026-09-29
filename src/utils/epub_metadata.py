"""
Lightweight EPUB metadata helpers shared by the e-paper and web UI.

The cache key includes file modification time and size, so replacing or
renaming an EPUB naturally invalidates stale metadata without a daemon or
persistent database.
"""

from functools import lru_cache
from pathlib import Path
from zipfile import ZipFile
import unicodedata
import xml.etree.ElementTree as ET


_CONTAINER_NS = (
    "urn:oasis:names:tc:opendocument:xmlns:container"
)


def _clean_text(value):
    if value is None:
        return None

    value = " ".join(str(value).split()).strip()
    if not value:
        return None

    return unicodedata.normalize("NFC", value)


@lru_cache(maxsize=128)
def _read_epub_title_cached(path_str, mtime_ns, file_size):
    del mtime_ns, file_size

    path = Path(path_str)

    try:
        with ZipFile(path) as epub:
            container = ET.fromstring(
                epub.read("META-INF/container.xml")
            )

            rootfile = container.find(
                f".//{{{_CONTAINER_NS}}}rootfile"
            )

            if rootfile is None:
                for element in container.iter():
                    if element.tag.split("}")[-1] == "rootfile":
                        rootfile = element
                        break

            if rootfile is None:
                return None

            opf_path = rootfile.attrib.get("full-path")
            if not opf_path:
                return None

            opf = ET.fromstring(epub.read(opf_path))

            # dc:title normally resolves simply to local-name "title".
            # Restrict the search to the OPF metadata element so a title
            # elsewhere in the package cannot accidentally be selected.
            metadata = None
            for element in opf.iter():
                if element.tag.split("}")[-1] == "metadata":
                    metadata = element
                    break

            if metadata is None:
                return None

            for element in metadata.iter():
                if element.tag.split("}")[-1].lower() == "title":
                    title = _clean_text(element.text)
                    if title:
                        return title

    except Exception:
        return None

    return None


def filename_title(path):
    """Human-readable fallback generated from the EPUB filename."""
    path = Path(path)
    title = path.stem.replace("_", " ").strip()
    return _clean_text(title) or path.stem


def get_epub_title(path, fallback=None):
    """Return EPUB dc:title, falling back safely when metadata is absent."""
    path = Path(path)

    if fallback is None:
        fallback = filename_title(path)

    try:
        stat = path.stat()
        title = _read_epub_title_cached(
            str(path.resolve()),
            int(stat.st_mtime_ns),
            int(stat.st_size),
        )
    except OSError:
        title = None

    return title or _clean_text(fallback) or filename_title(path)
