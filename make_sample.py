#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成几个样例 PDF(带页码角标)到 ./样例/ ,用于演示提交流程。"""
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

SAMPLES = [
    ("三年级数学期中试卷__50份.pdf", "三年级数学期中试卷", 2),
    ("四年级英语单元练习__45份.pdf", "四年级英语单元练习", 2),
    ("五年级语文阅读材料__30份.pdf", "五年级语文阅读材料", 4),
]


def make(path: Path, title: str, pages: int):
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    for i in range(1, pages + 1):
        c.setFont("STSong-Light", 26)
        c.drawCentredString(w / 2, h - 50 * mm, title)
        c.setFont("STSong-Light", 14)
        c.drawCentredString(w / 2, h - 70 * mm, f"(样例内容 第 {i} 页)")
        # 页码角标: 现场目视检查双张进纸/错位用
        c.setFont("STSong-Light", 18)
        c.drawRightString(w - 15 * mm, 12 * mm, f"第 {i} / {pages} 页")
        c.showPage()
    c.save()


def main():
    out = Path(__file__).parent / "样例"
    out.mkdir(exist_ok=True)
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    for name, title, pages in SAMPLES:
        p = out / name
        make(p, title, pages)
        print("已生成", p)


if __name__ == "__main__":
    main()
