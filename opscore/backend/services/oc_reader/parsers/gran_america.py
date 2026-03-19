# backend/services/oc_reader/parsers/gran_america.py
from __future__ import annotations

import re
from typing import List, Dict, Optional


def parse_gran_america(text: str) -> Dict:
    """
    Normalized return:

    {
        "numero_oc": str | None,
        "cliente": "Gran America",
        "items": [
            {"descripcion": str, "cantidad": int}
        ]
    }
    """

    if not text:
        return {"numero_oc": None, "cliente": "Gran America", "items": []}

    lines = [l.strip() for l in text.splitlines() if l and l.strip()]

    numero_oc = _extract_numero_oc(lines)
    items = _extract_items(lines)

    return {
        "numero_oc": numero_oc,
        "cliente": "Gran America",
        "items": items,
    }


# ---------------------------------------------------------
# Numero OC
# ---------------------------------------------------------
RX_OC_1 = re.compile(r"orden\s+de\s+compra\s*n[o]?\s*[:\-]?\s*([0-9]{4,})", re.IGNORECASE)
RX_OC_2 = re.compile(r"\boc\b\s*n[o]?\s*[:\-]?\s*([0-9]{4,})", re.IGNORECASE)
RX_OC_3 = re.compile(r"n[o]\s*[:\-]?\s*([0-9]{4,})$", re.IGNORECASE)


def _extract_numero_oc(lines: List[str]) -> Optional[str]:

    for line in lines[:80]:

        m = RX_OC_1.search(line)
        if m:
            return m.group(1)

        m = RX_OC_2.search(line)
        if m:
            return m.group(1)

        m = RX_OC_3.search(line)
        if m:
            return m.group(1)

    return None


# ---------------------------------------------------------
# Items
# ---------------------------------------------------------
RX_ITEM_START = re.compile(r"^\s*(\d{1,4})\s+(.+)$")
RX_ITEM_END = re.compile(r"^(.+?)\s+(\d{1,4})$")


def _extract_items(lines: List[str]) -> List[Dict]:

    items: List[Dict] = []

    for line in lines:

        low = line.lower()

        if any(k in low for k in (
            "total",
            "neto",
            "iva",
            "subtotal",
            "rut",
            "direccion",
            "telefono",
            "tel",
            "gran america",
            "orden de compra",
        )):
            continue

        cantidad: Optional[int] = None
        descripcion: Optional[str] = None

        m = RX_ITEM_START.match(line)
        if m:
            cantidad = int(m.group(1))
            descripcion = m.group(2).strip()

        else:
            m = RX_ITEM_END.match(line)
            if m:
                descripcion = m.group(1).strip()
                cantidad = int(m.group(2))

        if not descripcion or not cantidad:
            continue

        if len(descripcion) < 4:
            continue

        items.append(
            {
                "descripcion": descripcion[:500],
                "cantidad": cantidad,
            }
        )

    return items
