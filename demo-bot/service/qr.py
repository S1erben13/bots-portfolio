"""Генерация наклеек с QR-кодами: PNG для одной единицы и PDF-лист A4 для объекта."""

from __future__ import annotations

import io
import textwrap
from functools import lru_cache
from typing import Iterable

import qrcode
from django.conf import settings
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_M
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .links import qr_link
from .models import Equipment

CALL_TO_ACTION = "Что-то сломалось? Наведите камеру телефона"


def qr_image(data: str, box_size: int = 10) -> Image.Image:
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=box_size, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").get_image().convert("RGB")


@lru_cache(maxsize=8)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = settings.STICKER_FONT_BOLD_PATH if bold else settings.STICKER_FONT_PATH
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default(size)


def sticker_png(equipment: Equipment) -> bytes:
    """Наклейка 600×860 px: заголовок, QR, название оборудования и код."""
    width, height = 600, 860
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([4, 4, width - 5, height - 5], outline="black", width=3)

    def centered(y: int, text: str, font, fill="black") -> int:
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(((width - (box[2] - box[0])) / 2, y), text, font=font, fill=fill)
        return y + (box[3] - box[1]) + 10

    y = centered(24, "СЕРВИСНАЯ ЗАЯВКА", _font(40, bold=True))
    y = centered(y, equipment.site.company.name, _font(26), fill="#333333")

    code_img = qr_image(qr_link(equipment)).resize((460, 460), Image.NEAREST)
    img.paste(code_img, ((width - 460) // 2, y + 6))
    y += 476

    for line in textwrap.wrap(equipment.name, 30)[:2]:
        y = centered(y, line, _font(28, bold=True))
    y = centered(y, equipment.site.name[:40], _font(22), fill="#333333")
    y = centered(y + 4, f"Код: {equipment.code}", _font(30, bold=True))
    for line in textwrap.wrap(CALL_TO_ACTION, 40):
        y = centered(y, line, _font(20), fill="#555555")
    if equipment.site.company.phone:
        centered(y, f"Диспетчер: {equipment.site.company.phone}", _font(20), fill="#555555")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@lru_cache(maxsize=1)
def _pdf_fonts() -> tuple[str, str]:
    try:
        pdfmetrics.registerFont(TTFont("Sticker", settings.STICKER_FONT_PATH))
        pdfmetrics.registerFont(TTFont("Sticker-Bold", settings.STICKER_FONT_BOLD_PATH))
        return "Sticker", "Sticker-Bold"
    except Exception:  # шрифта нет (запуск вне Docker) — кириллица не отрисуется
        return "Helvetica", "Helvetica-Bold"


# Сетка 3×3 наклейки 62×88 мм на A4
STICKER_W, STICKER_H = 62 * mm, 88 * mm
COLS, ROWS = 3, 3


def stickers_pdf(equipment_list: Iterable[Equipment], title: str = "") -> bytes:
    regular, bold = _pdf_fonts()
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    pdf.setTitle(title or "QR-наклейки")
    page_w, page_h = A4
    margin_x = (page_w - COLS * STICKER_W) / 2
    margin_y = (page_h - ROWS * STICKER_H) / 2

    items = list(equipment_list)
    per_page = COLS * ROWS
    for index, equipment in enumerate(items):
        if index and index % per_page == 0:
            pdf.showPage()
        slot = index % per_page
        col, row = slot % COLS, slot // COLS
        x = margin_x + col * STICKER_W
        y = page_h - margin_y - (row + 1) * STICKER_H
        _draw_sticker(pdf, equipment, x, y, regular, bold)

    if not items:
        pdf.setFont(regular, 14)
        pdf.drawString(30 * mm, page_h - 30 * mm, "Нет оборудования для печати")
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


def _draw_sticker(pdf: canvas.Canvas, eq: Equipment, x: float, y: float, regular: str, bold: str):
    w, h = STICKER_W, STICKER_H
    # Линии реза
    pdf.setDash(2, 2)
    pdf.setLineWidth(0.3)
    pdf.rect(x, y, w, h)
    pdf.setDash()

    cx = x + w / 2
    top = y + h - 7 * mm
    pdf.setFont(bold, 10)
    pdf.drawCentredString(cx, top, "СЕРВИСНАЯ ЗАЯВКА")
    pdf.setFont(regular, 7)
    pdf.drawCentredString(cx, top - 4 * mm, eq.site.company.name[:45])

    qr_size = 44 * mm
    qr_png = io.BytesIO()
    qr_image(qr_link(eq)).save(qr_png, format="PNG")
    qr_png.seek(0)
    pdf.drawImage(ImageReader(qr_png), cx - qr_size / 2, top - 6 * mm - qr_size, qr_size, qr_size)

    text_y = top - 10 * mm - qr_size
    pdf.setFont(bold, 8)
    for line in simpleSplit(eq.name, bold, 8, w - 6 * mm)[:2]:
        pdf.drawCentredString(cx, text_y, line)
        text_y -= 3.6 * mm
    pdf.setFont(regular, 7)
    pdf.drawCentredString(cx, text_y, eq.site.name[:45])
    text_y -= 5 * mm
    pdf.setFont(bold, 10)
    pdf.drawCentredString(cx, text_y, f"Код: {eq.code}")
    text_y -= 4 * mm
    pdf.setFont(regular, 6)
    pdf.drawCentredString(cx, text_y, CALL_TO_ACTION)
    if eq.site.company.phone:
        pdf.drawCentredString(cx, text_y - 3 * mm, f"Диспетчер: {eq.site.company.phone}")
