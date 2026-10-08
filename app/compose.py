# -*- coding: utf-8 -*-
"""Create one ordered duplex PDF for each current class job."""
import math
from pathlib import Path

from pypdf import PageObject, PdfReader, PdfWriter

from . import config


def pdf_page_count(pdf_path) -> int:
    return len(PdfReader(str(pdf_path)).pages)


def sheets_per_copy(pages: int, duplex: bool = None) -> int:
    """Number of sheets per copy, rounded up for duplex printing."""
    d = config.DUPLEX if duplex is None else duplex
    return math.ceil(pages / 2) if d else pages


def group_sheets(pages: int, copies: int) -> int:
    return sheets_per_copy(pages) * copies


def compose_group_pdf(src_path, out_path, title: str, label: str,
                      copies: int) -> tuple[int, int]:
    """Compose copies without covers or tray based splitting.

    Odd page originals receive a blank back after every copy to preserve
    duplex copy boundaries. Returns (pages per copy, total sheets).
    """
    src_path, out_path = Path(src_path), Path(out_path)
    reader = PdfReader(str(src_path))
    pages = len(reader.pages)
    sheets = group_sheets(pages, copies)
    mb = reader.pages[0].mediabox
    blank = PageObject.create_blank_page(width=float(mb.width), height=float(mb.height))
    writer = PdfWriter()
    pad = config.DUPLEX and pages % 2 == 1
    for _ in range(copies):
        for page in reader.pages:
            writer.add_page(page)
        if pad:
            writer.add_page(blank)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as output:
        writer.write(output)
    return pages, sheets
