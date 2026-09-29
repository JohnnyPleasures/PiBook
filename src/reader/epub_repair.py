"""
Leitura tolerante de EPUBs com recursos ausentes no manifesto.

O ficheiro original nunca é alterado. Quando o EbookLib encontra um recurso
declarado no OPF mas inexistente no ZIP, é criada uma cópia corrigida na cache
do PiBook e essa cópia é usada apenas para leitura.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import posixpath
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, Optional, Set, Tuple

from ebooklib import epub


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_first(root: ET.Element, local_name: str) -> Optional[ET.Element]:
    for element in root.iter():
        if _local_name(element.tag) == local_name:
            return element
    return None


def _normalise_zip_path(path: str) -> str:
    path = path.replace("\\", "/")
    path = posixpath.normpath(path)
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def _resolve_manifest_href(opf_path: str, href: str) -> Optional[Tuple[str, str]]:
    parsed = urllib.parse.urlsplit(href)

    # Recursos externos não fazem parte do ZIP.
    if parsed.scheme or parsed.netloc:
        return None

    raw_path = parsed.path
    if not raw_path:
        return None

    opf_dir = posixpath.dirname(opf_path)

    encoded_path = _normalise_zip_path(
        posixpath.join(opf_dir, raw_path)
    )
    decoded_path = _normalise_zip_path(
        posixpath.join(opf_dir, urllib.parse.unquote(raw_path))
    )
    return encoded_path, decoded_path


def _path_exists(
    names: Set[str],
    lower_names: Set[str],
    candidates: Optional[Tuple[str, str]],
) -> bool:
    if candidates is None:
        return True

    for candidate in candidates:
        if candidate in names or candidate.lower() in lower_names:
            return True
    return False


def _discover_opf_path(zf: zipfile.ZipFile) -> str:
    names = set(zf.namelist())

    container_name = "META-INF/container.xml"
    if container_name in names:
        container_data = zf.read(container_name)
        container_root = ET.fromstring(container_data)

        for element in container_root.iter():
            if _local_name(element.tag) == "rootfile":
                full_path = element.attrib.get("full-path")
                if full_path:
                    full_path = _normalise_zip_path(full_path)
                    if full_path in names:
                        return full_path

    opf_files = [
        name for name in zf.namelist()
        if name.lower().endswith(".opf")
    ]
    if not opf_files:
        raise ValueError("O EPUB não contém um ficheiro OPF")
    return opf_files[0]


def _register_namespaces(xml_data: bytes) -> None:
    """Preserva os namespaces mais comuns ao voltar a gravar o OPF."""
    try:
        for event, value in ET.iterparse(
            __import__("io").BytesIO(xml_data),
            events=("start-ns",),
        ):
            prefix, uri = value
            try:
                ET.register_namespace(prefix or "", uri)
            except ValueError:
                pass
    except Exception:
        pass


def _remove_missing_manifest_items(
    opf_data: bytes,
    opf_path: str,
    zip_names: Iterable[str],
) -> Tuple[bytes, list[str]]:
    _register_namespaces(opf_data)

    root = ET.fromstring(opf_data)
    manifest = _find_first(root, "manifest")
    if manifest is None:
        raise ValueError("O OPF não contém um manifesto")

    names = {_normalise_zip_path(name) for name in zip_names}
    lower_names = {name.lower() for name in names}

    removed_ids: Set[str] = set()
    removed_paths: list[str] = []

    for item in list(manifest):
        if _local_name(item.tag) != "item":
            continue

        href = item.attrib.get("href")
        if not href:
            continue

        candidates = _resolve_manifest_href(opf_path, href)
        if _path_exists(names, lower_names, candidates):
            continue

        manifest.remove(item)

        item_id = item.attrib.get("id")
        if item_id:
            removed_ids.add(item_id)

        if candidates:
            removed_paths.append(candidates[-1])
        else:
            removed_paths.append(href)

    if not removed_paths:
        return opf_data, []

    # Remove referências no spine para itens que foram retirados.
    spine = _find_first(root, "spine")
    if spine is not None:
        for itemref in list(spine):
            if (
                _local_name(itemref.tag) == "itemref"
                and itemref.attrib.get("idref") in removed_ids
            ):
                spine.remove(itemref)

        for attr_name in list(spine.attrib):
            if (
                _local_name(attr_name) == "page-map"
                and spine.attrib.get(attr_name) in removed_ids
            ):
                del spine.attrib[attr_name]

    # Remove referências inválidas do guide, quando existirem.
    guide = _find_first(root, "guide")
    if guide is not None:
        for reference in list(guide):
            href = reference.attrib.get("href")
            if not href:
                continue
            candidates = _resolve_manifest_href(opf_path, href)
            if not _path_exists(names, lower_names, candidates):
                guide.remove(reference)

    return (
        ET.tostring(root, encoding="utf-8", xml_declaration=True),
        removed_paths,
    )


def repair_epub_copy(
    source_path: str,
    cache_root: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Tuple[str, list[str]]:
    source = Path(source_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)

    if cache_root is None:
        project_root = Path(__file__).resolve().parents[2]
        cache_dir = project_root / "cache" / "epub_repair"
    else:
        cache_dir = Path(cache_root)

    cache_dir.mkdir(parents=True, exist_ok=True)

    stat = source.stat()
    identity = (
        f"{source}|{stat.st_size}|{stat.st_mtime_ns}"
    ).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:20]
    repaired_path = cache_dir / f"{source.stem}-{digest}.epub"

    if repaired_path.exists():
        return str(repaired_path), []

    temporary_path = repaired_path.with_suffix(".tmp")

    try:
        with zipfile.ZipFile(source, "r") as zin:
            if zin.testzip() is not None:
                raise zipfile.BadZipFile(
                    f"Entrada ZIP danificada: {zin.testzip()}"
                )

            opf_path = _discover_opf_path(zin)
            opf_data = zin.read(opf_path)
            repaired_opf, removed_paths = _remove_missing_manifest_items(
                opf_data,
                opf_path,
                zin.namelist(),
            )

            if not removed_paths:
                raise KeyError(
                    "O recurso ausente não foi encontrado no manifesto OPF"
                )

            infos = zin.infolist()
            infos.sort(key=lambda info: 0 if info.filename == "mimetype" else 1)

            with zipfile.ZipFile(temporary_path, "w") as zout:
                for info in infos:
                    data = repaired_opf if info.filename == opf_path else zin.read(
                        info.filename
                    )
                    cloned_info = copy.copy(info)

                    # A especificação EPUB pede mimetype sem compressão.
                    if info.filename == "mimetype":
                        cloned_info.compress_type = zipfile.ZIP_STORED

                    zout.writestr(cloned_info, data)

        os.replace(temporary_path, repaired_path)

        if logger:
            logger.warning(
                "EPUB reparado na cache; recursos ausentes ignorados: %s",
                ", ".join(removed_paths),
            )

        return str(repaired_path), removed_paths

    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except Exception:
            pass


def read_epub_tolerant(
    epub_path: str,
    logger: Optional[logging.Logger] = None,
):
    """Abre normalmente; em caso de recurso ausente, usa uma cópia reparada."""
    try:
        return epub.read_epub(epub_path)
    except KeyError as original_error:
        if logger:
            logger.warning(
                "EPUB contém uma referência para um ficheiro ausente: %s",
                original_error,
            )

        repaired_path, removed = repair_epub_copy(
            epub_path,
            logger=logger,
        )

        if logger:
            logger.info(
                "A abrir cópia EPUB reparada: %s (%d recurso(s) removido(s))",
                repaired_path,
                len(removed),
            )

        return epub.read_epub(repaired_path)
