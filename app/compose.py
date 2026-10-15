# -*- coding: utf-8 -*-
"""Create one ordered duplex PDF for each current class job."""
import math
from io import BytesIO
from pathlib import Path

from pypdf import PageObject, PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont

from . import config


def pdf_page_count(pdf_path) -> int:
    return len(PdfReader(str(pdf_path)).pages)


def sheets_per_copy(pages: int, duplex: bool = None) -> int:
    """Number of sheets per copy, rounded up for duplex printing."""
    d = config.DUPLEX if duplex is None else duplex
    return math.ceil(pages / 2) if d else pages


def group_sheets(pages: int, copies: int) -> int:
    return sheets_per_copy(pages) * copies


def _cover_page(width: float, height: float, title: str, school: str, grade: str,
                class_number: int, classes: int, copies: int):
    """Draw a single monochrome page at the source PDF's physical size."""
    font = 'ClassCoverChinese'
    if font not in pdfmetrics.getRegisteredFontNames():
        font_path = Path('C:/Windows/Fonts/simhei.ttf')
        if font_path.is_file():
            pdfmetrics.registerFont(TTFont(font, str(font_path)))
        else:
            font = 'STSong-Light'
            if font not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(UnicodeCIDFont(font))
    buffer = BytesIO()
    doc = canvas.Canvas(buffer, pagesize=(width, height), pageCompression=1)
    margin = min(width * .1, 65)
    usable = width - 2 * margin
    y = height - min(height * .14, 110)
    doc.setFont(font, 17)
    doc.drawString(margin, y, '班级打印封面')
    y -= 48
    fields = [('文件名称', title), ('学校', school), ('年级', grade),
              ('班级', f'{class_number}/{classes}'), ('份数', f'{copies}份')]
    for label, value in fields:
        text = f'{label}：{value}'
        line = ''
        doc.setFont(font, 13)
        for char in text:
            if line and pdfmetrics.stringWidth(line + char, font, 13) > usable:
                doc.drawString(margin, y, line)
                y -= 25
                line = char
            else:
                line += char
        if line:
            doc.drawString(margin, y, line)
            y -= 39
    doc.showPage()
    doc.save()
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def compose_group_pdf(src_path, out_path, title: str, label: str,
                      copies: int, *, cover: dict | None = None) -> tuple[int, int]:
    """Compose one class job, optionally with one cover sheet.

    Odd page originals receive a blank back after every copy to preserve
    duplex copy boundaries. Returns (pages per copy, total sheets).
    """
    src_path, out_path = Path(src_path), Path(out_path)
    reader = PdfReader(str(src_path))
    pages = len(reader.pages)
    sheets = group_sheets(pages, copies) + int(cover is not None)
    mb = reader.pages[0].mediabox
    blank = PageObject.create_blank_page(width=float(mb.width), height=float(mb.height))
    writer = PdfWriter()
    if cover is not None:
        writer.add_page(_cover_page(float(mb.width), float(mb.height), title,
                                   cover['school'], cover['grade'],
                                   cover['class_number'], cover['classes'], copies))
        if config.DUPLEX:
            writer.add_page(blank)
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
