# -*- coding: utf-8 -*-
"""维护测试页: 每天/闲置期自动打印,防止喷嘴堵塞,同时供人工目视检查喷孔状态。"""
import time
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from . import config

_ready = False


def maintenance_pdf_path() -> Path:
    global _ready
    if not _ready:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        _ready = True
    path = config.DATA_DIR / "maintenance.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    c.setFont("STSong-Light", 22)
    c.drawCentredString(w / 2, h - 30 * mm, "打 印 机 维 护 测 试 页")
    c.setFont("STSong-Light", 12)
    c.drawCentredString(w / 2, h - 42 * mm, time.strftime("%Y-%m-%d %H:%M"))
    # 多组粗细线条与灰阶块,覆盖喷嘴检查所需图案
    y = h - 60 * mm
    for i, lw in enumerate([0.5, 1, 2, 4]):
        c.setLineWidth(lw)
        for r in range(8):
            yy = y - r * 6 * mm
            c.line(25 * mm, yy, w - 25 * mm, yy)
        y -= 60 * mm
    c.setFont("STSong-Light", 10)
    c.drawCentredString(w / 2, 20 * mm,
                        "检查线条是否连续均匀;出现断线/白线请在机队页隔离该机")
    c.showPage()
    c.save()
    return path
