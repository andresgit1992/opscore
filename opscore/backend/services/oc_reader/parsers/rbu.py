# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


class SingletonMeta(type):
    _instances: Dict[type, object] = {}
    _lock = threading.Lock()

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with cls._lock:
                if cls not in cls._instances:
                    cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


@dataclass(frozen=True)
class RBURegexContract:
    rx_oc: re.Pattern
    rx_date: re.Pattern
    rx_total: re.Pattern
    rx_code_only: re.Pattern
    rx_money: re.Pattern


class RBUParserContractRegistry(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.regex = RBURegexContract(
            rx_oc=re.compile(r"orden\s+de\s+compra\s*n[°ºo*]?\s*([0-9]{4,})", re.IGNORECASE),
            rx_date=re.compile(r"\b(\d{2}/\d{2}/\d{2,4})\b"),
            rx_total=re.compile(r"\btotal\s+([\d\.,]+)\b", re.IGNORECASE),
            rx_code_only=re.compile(r"^[A-Z]{2,6}\d{4,}$"),
            rx_money=re.compile(r"[\d\.,]+"),
        )

        self.end_keys = (
            "neto", "iva", "total", "subtotal",
            "facturar a", "presentar factura",
            "observaciones", "nombre y firma",
            "guia de despacho", "guía de despacho",
            "forma de pago", "condiciones",
        )

        self.header_noise = (
            "item", "descripción", "descripcion", "moneda", "unidad",
            "cantidad", "precio unitario", "valor unitario",
            "descto", "unitario", "fax", "teléfono", "telefono",
            "señores", "senores", "atención", "atencion", "rut",
            "fecha o/c", "fecha entrega", "precio",
        )

        self.unit_words = ("unida", "unidad", "unid")


class RBUHelperFactory:
    @staticmethod
    def split_lines(text: str) -> List[str]:
        out: List[str] = []
        for ln in (text or "").splitlines():
            ln = (ln or "").replace("\u00a0", " ").strip()
            if not ln:
                continue
            ln = re.sub(r"[ \t]+", " ", ln)
            out.append(ln)
        return out

    @staticmethod
    def norm(s: str) -> str:
        return (s or "").strip().lower()

    @staticmethod
    def is_end_line(ln: str) -> bool:
        low = RBUHelperFactory.norm(ln)
        return any(k in low for k in RBUParserContractRegistry().end_keys)

    @staticmethod
    def is_header_noise(ln: str) -> bool:
        low = RBUHelperFactory.norm(ln)
        return any(k in low for k in RBUParserContractRegistry().header_noise)

    @staticmethod
    def money_to_int_cl(s: str) -> int:
        s = (s or "").strip().replace("$", "").replace("CLP", "").strip()
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "")
                s = s.split(",")[0]
            else:
                s = s.replace(",", "")
                s = s.split(".")[0]
        else:
            if "," in s:
                if s.count(",") >= 2:
                    s = s.replace(",", "")
                else:
                    s = s.split(",")[0]
            if "." in s:
                if s.count(".") >= 2:
                    s = s.replace(".", "")
                else:
                    s = s.split(".")[0]
        s = re.sub(r"[^\d]", "", s)
        return int(s) if s else 0

    @staticmethod
    def concat_code_desc(code: str, desc: str) -> str:
        code = (code or "").strip()
        desc = (desc or "").strip()
        if code and desc:
            if desc.upper().startswith(code.upper()):
                return desc
            return f"{code} - {desc}"
        return code or desc

    @staticmethod
    def extract_qty_from_price_line(line: str) -> int:
        low = line.lower()
        if "$" not in line:
            return 1
        if not any(w in low for w in RBUParserContractRegistry().unit_words):
            return 1

        tokens = re.findall(r"\d+(?:[.,]\d+)?", line)
        if not tokens:
            return 1

        # patrón típico: "$ Unida 3 109,480 328,440"
        # la cantidad suele ser el primer entero chico
        for tok in tokens[:3]:
            try:
                val = int(float(tok.replace(".", "").replace(",", ".")))
            except Exception:
                continue
            if 1 <= val <= 999:
                return val

        return 1


class RBUResponseFactory:
    @staticmethod
    def build(
        *,
        numero_oc: Optional[str],
        fecha: Optional[str],
        monto: int,
        items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "numero_oc": numero_oc,
            "cliente": "RBU",
            "fecha": fecha,
            "monto": monto,
            "items": items,
        }


class RBUHeaderParser:
    @staticmethod
    def extract_numero_oc(lines: List[str]) -> Optional[str]:
        rx = RBUParserContractRegistry().regex.rx_oc
        for ln in lines[:80]:
            m = rx.search(ln)
            if m:
                return m.group(1).strip()
        return None

    @staticmethod
    def extract_fecha(lines: List[str]) -> Optional[str]:
        rx = RBUParserContractRegistry().regex.rx_date
        for ln in lines[:60]:
            if "fecha entrega" in RBUHelperFactory.norm(ln):
                continue
            m = rx.search(ln)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def extract_total(text: str) -> int:
        rx = RBUParserContractRegistry().regex.rx_total
        m = rx.search(text or "")
        return RBUHelperFactory.money_to_int_cl(m.group(1)) if m else 0



class RBUItemsParser:

    @staticmethod
    def extract_items_vertical(lines):

        items = []
        rx_code = re.compile(r"^[A-Z]{3}\d{3,}")

        i = 0
        n = len(lines)

        while i < n:

            line = lines[i].strip()

            if not rx_code.match(line):
                i += 1
                continue

            code = line
            i += 1

            desc_parts = []

            while i < n and not re.match(r"^\d+$", lines[i].strip()):
                desc_parts.append(lines[i].strip())
                i += 1

            if i >= n:
                break

            try:
                qty = int(lines[i].strip())
            except:
                qty = 1

            i += 1

            unit_price = 0
            if i < n:
                raw = lines[i].replace(",", "").replace(".", "")
                if raw.isdigit():
                    unit_price = int(raw)
            i += 1

            total = 0
            if i < n:
                raw = lines[i].replace(",", "").replace(".", "")
                if raw.isdigit():
                    total = int(raw)
            i += 1

            desc = " ".join(desc_parts).strip()

            items.append({
                "descripcion": f"{code} - {desc}",
                "cantidad": qty,
                "precio_unitario": unit_price,
                "total": total
            })

        return items
def parse_rbu(text: str) -> Dict[str, Any]:
    lines = RBUHelperFactory.split_lines(text)

    numero_oc = RBUHeaderParser.extract_numero_oc(lines)
    fecha = RBUHeaderParser.extract_fecha(lines)
    monto = RBUHeaderParser.extract_total(text)

    items = RBUItemsParser.extract_items_vertical(lines)

    return RBUResponseFactory.build(
        numero_oc=numero_oc,
        fecha=fecha,
        monto=monto,
        items=items,
    )
