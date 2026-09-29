#!/usr/bin/env python3
"""Testa a abertura tolerante de um EPUB sem iniciar o e-paper."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import ebooklib
from src.reader.epub_repair import read_epub_tolerant


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("epub", help="Caminho do ficheiro EPUB")
    args = parser.parse_args()

    path = Path(args.epub).expanduser().resolve()
    if not path.is_file():
        print(f"ERRO: ficheiro inexistente: {path}", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s - %(message)s",
    )
    logger = logging.getLogger("epub-check")

    book = read_epub_tolerant(str(path), logger=logger)
    documents = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))

    title_values = book.get_metadata("DC", "title")
    title = title_values[0][0] if title_values else path.stem

    print()
    print(f"Título:    {title}")
    print(f"Capítulos: {len(documents)}")
    print("Resultado: EPUB aberto corretamente")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
