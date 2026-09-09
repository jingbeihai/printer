# -*- coding: utf-8 -*-
"""
PDF 合成: 把原始 PDF 拼成 [封面][空白][内容×N份] 的打印文件。

为什么自己拼而不是用驱动的 copies=50:
1. 页序/逐份/双面完全由我们控制,绕开二手固件与驱动 collate 的行为差异;
2. 封面页是"分组安全网"——托盘里多组叠放时,翻到封面即可撕开,不用数页;
3. 封面自带任务名/小组号/份数,可直接当装订分发标签用。
"""
import math
import time
from pathlib import Path

from pypdf import PdfReader, PdfWriter, PageObject
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from . import config

_FONT = "STSong-Light"   # reportlab 内置中文字体,无需外部字体文件
_font_ready = False


def _ensure_font():
    global _font_ready
    if not _font_ready:
        pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
        _font_ready = True


def pdf_page_count(pdf_path) -> int:
    return len(PdfReader(str(pdf_path)).pages)


def sheets_per_copy(pages: int, duplex: bool = None) -> int:
    """每份占多少张纸。双面向上取整。"""
    d = config.DUPLEX if duplex is None else duplex
    return math.ceil(pages / 2) if d else pages


def group_sheets(pages: int, copies: int) -> int:
    """一个小组的总张数 = 封面1张 + 内容张数。"""
    return 1 + sheets_per_copy(pages) * copies


def max_copies_per_group(pages: int, tray_limit: int = None) -> int:
    """按托盘安全线反推每组最多份数。"""
    limit = tray_limit or config.TRAY_SHEET_LIMIT
    per = sheets_per_copy(pages)
    return max(1, (limit - 1) // per)


def split_copies(pages: int, copies: int) -> list[int]:
    """把份数按托盘上限切成若干小组,返回每组份数列表。"""
    cap = max_copies_per_group(pages)
    chunks, left = [], copies
    while left > 0:
        take = min(cap, left)
        chunks.append(take)
        left -= take
    return chunks


def make_cover_pdf(out_path, title: str, label: str, copies: int,
                   pages: int, sheets: int, source: str = ""):
    """生成一页大字封面。"""
    _ensure_font()
    c = canvas.Canvas(str(out_path), pagesize=A4)
    w, h = A4
    # 醒目边框,方便在一摞纸里一眼认出
    c.setLineWidth(4)
    c.rect(15 * mm, 15 * mm, w - 30 * mm, h - 30 * mm)
    c.setFont(_FONT, 30)
    c.drawCentredString(w / 2, h - 45 * mm, "打 印 小 组 封 面")
    c.setLineWidth(1)
    c.line(30 * mm, h - 55 * mm, w - 30 * mm, h - 55 * mm)

    def row(y, k, v, size=18):
        c.setFont(_FONT, 14)
        c.drawRightString(70 * mm, y, k)
        c.setFont(_FONT, size)
        c.drawString(78 * mm, y, str(v))

    row(h - 75 * mm, "任　务", title[:28], 22)
    row(h - 95 * mm, "小　组", label, 26)
    row(h - 115 * mm, "份　数", f"{copies} 份", 22)
    row(h - 135 * mm, "每份页数", f"{pages} 页", 18)
    row(h - 155 * mm, "本组张数", f"{sheets} 张(含本封面)", 18)
    row(h - 175 * mm, "来　源", source or "-", 16)
    row(h - 195 * mm, "合成时间", time.strftime("%Y-%m-%d %H:%M"), 14)

    c.setFont(_FONT, 12)
    c.drawCentredString(w / 2, 25 * mm,
                        "本页为分隔封面,装订前请撕下;封面之后即为本组全部成品")
    c.showPage()
    c.save()


def compose_group_pdf(src_path, out_path, title: str, label: str,
                      copies: int, source: str = "") -> tuple[int, int]:
    """
    合成打印文件: [封面][空白][(内容+奇数补白)×copies]。
    双面模式下封面独占一张纸;内容为奇数页时每份末尾补一张空白,
    防止下一份首页印到上一份背面。
    返回 (pages每份页数, sheets本组张数)。
    """
    src_path = Path(src_path)
    out_path = Path(out_path)
    reader = PdfReader(str(src_path))
    pages = len(reader.pages)
    sheets = group_sheets(pages, copies)

    cover_path = out_path.with_suffix(".cover.pdf")
    make_cover_pdf(cover_path, title, label, copies, pages, sheets, source)
    cover = PdfReader(str(cover_path)).pages[0]

    # 用内容页尺寸生成空白页,保证双面不错位
    mb = reader.pages[0].mediabox
    blank = PageObject.create_blank_page(width=float(mb.width), height=float(mb.height))

    writer = PdfWriter()
    writer.add_page(cover)
    writer.add_page(blank)                        # 封面背面留白,封面独占一张纸
    pad = config.DUPLEX and (pages % 2 == 1)      # 奇数页内容双面时逐份补白
    for _ in range(copies):
        for p in reader.pages:
            writer.add_page(p)
        if pad:
            writer.add_page(blank)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        writer.write(f)
    cover_path.unlink(missing_ok=True)
    return pages, sheets
