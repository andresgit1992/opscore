from __future__ import annotations

import os
import re
from typing import Optional

MIN_TEXT_LEN = 120
MIN_MEANINGFUL_LINES = 6

# Senales de tabla de items
RX_TABLE_HINT = re.compile(r"(item\s+descrip|precio\s+unitario|cantidad|moneda\s+unidad)", re.IGNORECASE)

# Senal "Unida/Unidad"
RX_UNIT_HINT = re.compile(r"\b(unida|unidad|unid)\b", re.IGNORECASE)

# Senal de codigos tipo RBU
RX_CODE_HINT = re.compile(r"\b[A-Z]{2,6}\d{6,}\b")

# Senal de montos CLP
RX_MONEY_HINT = re.compile(r"\b\d{1,3}(?:[.\s]\d{3})+(?:,\d{1,2})?\b|\b\d{4,}\b")

# Senales fuertes de totales
RX_TOTAL_HINT = re.compile(r"\b(total|neto|iva)\b", re.IGNORECASE)

# Senales METROPOL
RX_METROPOL_ANCHOR = re.compile(r"\borden\s+compra\s+nro\b", re.IGNORECASE)
RX_METROPOL_TOTAL = re.compile(r"\btotal\s*\(\s*clp\s*\)\b", re.IGNORECASE)
RX_METROPOL_SUBTOTAL = re.compile(r"\bsubtotal\s*\(\s*clp\s*\)\b", re.IGNORECASE)
RX_METROPOL_TABLE = re.compile(r"\bplanta\b.*\bpieza\b", re.IGNORECASE)
RX_METROPOL_UOM = re.compile(r"\bpcs\b", re.IGNORECASE)
RX_METROPOL_QTY_LINE = re.compile(r"^\d{1,6}$", re.IGNORECASE)


def extract_text_from_pdf(
    pdf_path: str,
    lang: str = "spa",
    poppler_path: Optional[str] = None,
    tesseract_cmd: Optional[str] = None,
    debug: bool = False,
    force_ocr: bool = False,
) -> str:
    """
    Strategy:
      - If force_ocr=True -> OCR directly.
      - Else:
          1) PyMuPDF (embedded)
          2) pdfplumber (embedded)
          3) OCR fallback only if needed
    """
    if not os.path.isfile(pdf_path):
        return ""

    if force_ocr:
        ocr_text = _extract_text_ocr(pdf_path, lang, poppler_path, tesseract_cmd)
        if debug:
            print("extract_text_from_pdf: force_ocr=True")
        return _normalize_text(ocr_text)

    embedded = _extract_text_pymupdf(pdf_path)
    if _is_text_usable(embedded) and not _needs_ocr_for_table(embedded):
        if debug:
            print("extract_text_from_pdf: usando PyMuPDF")
        return _normalize_text(embedded)

    embedded2 = _extract_text_pdfplumber(pdf_path)
    if _is_text_usable(embedded2) and not _needs_ocr_for_table(embedded2):
        if debug:
            print("extract_text_from_pdf: usando pdfplumber")
        return _normalize_text(embedded2)

    ocr_text = _extract_text_ocr(pdf_path, lang, poppler_path, tesseract_cmd)
    if debug:
        print("extract_text_from_pdf: usando OCR fallback")
    return _normalize_text(ocr_text)


def _normalize_text(text: str) -> str:
    t = (text or "").replace("\u00a0", " ").replace("\x0c", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\r\n?", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _is_text_usable(text: str) -> bool:
    t = _normalize_text(text)
    if not t:
        return False
    if len(t) < MIN_TEXT_LEN:
        return False
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    meaningful = [ln for ln in lines if len(ln) >= 6]
    return len(meaningful) >= MIN_MEANINGFUL_LINES


def _has_rbu_vertical_items(text: str) -> bool:
    """
    Detecta el patron real de PDFs RBU:
      - codigos tipo TCP000692 / MOT902210
      - unidad en alguna linea
      - montos tipo 171,360
    """
    t = _normalize_text(text)
    if not t:
        return False

    codes = RX_CODE_HINT.findall(t)
    if len(codes) < 1:
        return False

    if not RX_UNIT_HINT.search(t):
        return False

    if not RX_MONEY_HINT.search(t):
        return False

    return True


def _looks_like_metropol(text: str) -> bool:
    t = _normalize_text(text)
    if not t:
        return False
    return bool(
        RX_METROPOL_ANCHOR.search(t)
        or (RX_METROPOL_TOTAL.search(t) and RX_METROPOL_SUBTOTAL.search(t))
        or RX_METROPOL_TABLE.search(t)
    )


def _has_metropol_items(text: str) -> bool:
    t = _normalize_text(text)
    if not t:
        return False

    if not (RX_METROPOL_TOTAL.search(t) or RX_METROPOL_SUBTOTAL.search(t)):
        return False

    if not RX_METROPOL_UOM.search(t):
        return False

    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    qty_hits = 0
    for ln in lines:
        if RX_METROPOL_QTY_LINE.match(ln):
            qty_hits += 1
            if qty_hits >= 1:
                break
    return qty_hits >= 1


def _needs_ocr_for_table(text: str) -> bool:
    """
    Cuando forzar OCR:
      - texto vacio o inutil
      - estructura de tabla sin senales minimas de items
      - no forzar OCR si RBU verticalizado ya parece usable
      - no forzar OCR si METROPOL ya parece usable
    """
    t = _normalize_text(text)
    if not t:
        return True

    if _has_rbu_vertical_items(t):
        return False

    if _looks_like_metropol(t):
        if _has_metropol_items(t):
            return False
        if RX_METROPOL_TABLE.search(t) or RX_METROPOL_TOTAL.search(t) or RX_METROPOL_SUBTOTAL.search(t):
            return True

    if RX_TOTAL_HINT.search(t) and (RX_CODE_HINT.search(t) or RX_MONEY_HINT.search(t)):
        return False

    if RX_TABLE_HINT.search(t):
        if not RX_CODE_HINT.search(t) and not RX_UNIT_HINT.search(t):
            return True

    return False


def _extract_text_pymupdf(pdf_path: str) -> str:
    try:
        import fitz
    except Exception:
        return ""

    parts = []
    try:
        doc = fitz.open(pdf_path)
        for page in doc:
            txt = page.get_text("text") or ""
            txt = _normalize_text(txt)
            if txt:
                parts.append(txt)
        doc.close()
    except Exception:
        return ""

    return _normalize_text("\n\n".join(parts))


def _extract_text_pdfplumber(pdf_path: str) -> str:
    try:
        import pdfplumber
    except Exception:
        return ""

    parts = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                txt = page.extract_text() or ""
                txt = _normalize_text(txt)
                if txt:
                    parts.append(txt)
    except Exception:
        return ""

    return _normalize_text("\n\n".join(parts))


def _extract_text_ocr(
    pdf_path: str,
    lang: str,
    poppler_path: Optional[str],
    tesseract_cmd: Optional[str],
) -> str:
    try:
        from pdf2image import convert_from_path
        import pytesseract
        from PIL import ImageOps
    except Exception:
        return ""

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    try:
        images = convert_from_path(pdf_path, dpi=300, poppler_path=poppler_path)
    except Exception:
        return ""

    cfg = "--oem 3 --psm 6 -c preserve_interword_spaces=1"
    parts = []

    for img in images[:6]:
        try:
            g = img.convert("L")
            g = ImageOps.autocontrast(g)
            g = g.point(lambda p: 255 if p > 160 else 0)
            txt = pytesseract.image_to_string(g, lang=lang, config=cfg) or ""
            txt = _normalize_text(txt)
            if txt:
                parts.append(txt)
        except Exception:
            continue

    return _normalize_text("\n\n".join(parts))