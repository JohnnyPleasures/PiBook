"""
EPUB Cover Extractor
Extracts cover images from EPUB files and caches them as thumbnails
"""

import os
import zipfile
import posixpath
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote
from typing import Optional
import logging
from PIL import Image
import io

logger = logging.getLogger(__name__)


class CoverExtractor:
    """Extract and cache book covers from EPUB files"""
    
    def __init__(self, cache_dir: str = "data/covers"):
        """
        Initialize cover extractor
        
        Args:
            cache_dir: Directory to cache cover thumbnails
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
    def get_cover(self, epub_path: str, size: tuple = (60, 90)) -> Optional[Image.Image]:
        """
        Get cover image for EPUB, using cache if available
        
        Args:
            epub_path: Path to EPUB file
            size: Thumbnail size (width, height)
            
        Returns:
            PIL Image or None if no cover found
        """
        # Generate cache filename
        epub_name = Path(epub_path).stem
        cache_file = self.cache_dir / f"{epub_name}_{size[0]}x{size[1]}.png"
        
        # Check cache first
        if cache_file.exists():
            try:
                return Image.open(cache_file)
            except Exception as e:
                logger.warning(f"Failed to load cached cover: {e}")
        
        # Extract cover from EPUB
        cover_image = self._extract_cover(epub_path)
        
        if cover_image:
            # Create thumbnail
            thumbnail = self._create_thumbnail(cover_image, size)
            
            # Cache it
            try:
                thumbnail.save(cache_file)
                logger.info(f"Cached cover for {epub_name}")
            except Exception as e:
                logger.warning(f"Failed to cache cover: {e}")
            
            return thumbnail
        
        return None
    
    def _read_epub_image(
        self,
        epub: zipfile.ZipFile,
        image_path: str,
    ) -> Optional[Image.Image]:
        """Read an image from the EPUB and detach it from the ZIP buffer."""
        try:
            image_data = epub.read(image_path)
            with Image.open(io.BytesIO(image_data)) as image:
                image.load()
                return image.copy()
        except (KeyError, OSError, ValueError):
            return None

    def _extract_declared_cover(
        self,
        epub: zipfile.ZipFile,
    ) -> Optional[Image.Image]:
        """Extract a cover declared by EPUB2/EPUB3 package metadata."""
        try:
            container_xml = epub.read("META-INF/container.xml")
            container = ET.fromstring(container_xml)

            rootfile = next(
                (
                    elem
                    for elem in container.iter()
                    if elem.tag.rsplit("}", 1)[-1] == "rootfile"
                    and elem.attrib.get("full-path")
                ),
                None,
            )
            if rootfile is None:
                return None

            opf_path = rootfile.attrib["full-path"]
            package = ET.fromstring(epub.read(opf_path))

            manifest = {}
            for elem in package.iter():
                if elem.tag.rsplit("}", 1)[-1] != "item":
                    continue
                item_id = elem.attrib.get("id")
                if item_id:
                    manifest[item_id] = elem

            cover_item = None

            # EPUB3: the cover image is identified directly in the manifest.
            for item in manifest.values():
                properties = item.attrib.get("properties", "").split()
                if "cover-image" in properties:
                    cover_item = item
                    break

            # EPUB2: metadata points to the manifest ID of the cover image.
            if cover_item is None:
                cover_id = None
                for elem in package.iter():
                    if elem.tag.rsplit("}", 1)[-1] != "meta":
                        continue
                    if elem.attrib.get("name", "").strip().lower() == "cover":
                        cover_id = elem.attrib.get("content", "").strip()
                        if cover_id:
                            break

                if cover_id:
                    cover_item = manifest.get(cover_id)

            if cover_item is None:
                return None

            href = cover_item.attrib.get("href", "").strip()
            media_type = cover_item.attrib.get("media-type", "").lower()

            if not href or not media_type.startswith("image/"):
                return None

            # Hrefs in an OPF are relative to the directory containing it.
            href = unquote(href.split("#", 1)[0].split("?", 1)[0])
            opf_dir = posixpath.dirname(opf_path)
            image_path = posixpath.normpath(
                posixpath.join(opf_dir, href)
            )

            image = self._read_epub_image(epub, image_path)
            if image is not None:
                logger.debug(
                    "Cover found from OPF metadata: %s",
                    image_path,
                )
            return image

        except (KeyError, ET.ParseError, OSError, ValueError) as e:
            logger.debug("Could not resolve cover from OPF metadata: %s", e)
            return None

    def _extract_cover(self, epub_path: str) -> Optional[Image.Image]:
        """
        Extract cover image from EPUB file.

        Prefer the cover declared by EPUB2/EPUB3 metadata, then retain the
        legacy filename-based discovery as a compatibility fallback.
        """
        try:
            with zipfile.ZipFile(epub_path, 'r') as epub:
                declared_cover = self._extract_declared_cover(epub)
                if declared_cover is not None:
                    return declared_cover

                # Common cover image filenames
                cover_names = [
                    'cover.jpg', 'cover.jpeg', 'cover.png',
                    'Cover.jpg', 'Cover.jpeg', 'Cover.png',
                    'OEBPS/cover.jpg', 'OEBPS/cover.jpeg', 'OEBPS/cover.png',
                    'OEBPS/Images/cover.jpg', 'OEBPS/Images/cover.jpeg', 'OEBPS/Images/cover.png',
                    'OPS/images/cover.jpg', 'OPS/images/cover.jpeg', 'OPS/images/cover.png',
                ]

                # Legacy fallback: try known filenames.
                for name in cover_names:
                    image = self._read_epub_image(epub, name)
                    if image is not None:
                        return image

                # Legacy fallback: any image whose filename contains "cover".
                for file_info in epub.filelist:
                    filename = file_info.filename.lower()
                    if (
                        'cover' in filename
                        and filename.endswith(('.jpg', '.jpeg', '.png'))
                    ):
                        image = self._read_epub_image(
                            epub,
                            file_info.filename,
                        )
                        if image is not None:
                            return image

                logger.debug(
                    f"No cover found in {Path(epub_path).name}"
                )
                return None

        except Exception as e:
            logger.error(f"Error extracting cover from {epub_path}: {e}")
            return None

    def _create_thumbnail(self, image: Image.Image, size: tuple) -> Image.Image:
        """
        Create thumbnail from image, optimized for e-ink display
        
        Args:
            image: Source PIL Image
            size: Target size (width, height)
            
        Returns:
            Thumbnail PIL Image (1-bit for e-ink)
        """
        from PIL import ImageEnhance
        
        # Convert to grayscale
        if image.mode != 'L':
            image = image.convert('L')
        
        # Enhance contrast for better e-ink rendering
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(1.5)  # Increase contrast
        
        # Resize maintaining aspect ratio
        image.thumbnail(size, Image.Resampling.LANCZOS)
        
        # Create new image with exact size (add padding if needed)
        thumbnail = Image.new('L', size, 255)  # White background
        
        # Center the resized image
        offset = ((size[0] - image.size[0]) // 2, (size[1] - image.size[1]) // 2)
        thumbnail.paste(image, offset)
        
        # Convert to 1-bit with better dithering for e-ink
        thumbnail = thumbnail.convert('1', dither=Image.Dither.FLOYDSTEINBERG)
        
        return thumbnail
    
    def create_fallback_cover(self, size: tuple = (60, 90)) -> Image.Image:
        """
        Create a generic book icon for books without covers
        
        Args:
            size: Image size (width, height)
            
        Returns:
            PIL Image (1-bit)
        """
        from PIL import ImageDraw, ImageFont
        
        # Create white background
        image = Image.new('1', size, 1)
        draw = ImageDraw.Draw(image)
        
        # Draw book outline
        margin = 5
        draw.rectangle(
            [(margin, margin), (size[0] - margin, size[1] - margin)],
            outline=0,
            width=2
        )
        
        # Draw book spine
        spine_x = size[0] // 4
        draw.line([(spine_x, margin), (spine_x, size[1] - margin)], fill=0, width=2)
        
        # Draw horizontal lines (pages)
        for i in range(3):
            y = margin + 15 + (i * 20)
            draw.line([(spine_x + 5, y), (size[0] - margin - 5, y)], fill=0, width=1)
        
        return image
