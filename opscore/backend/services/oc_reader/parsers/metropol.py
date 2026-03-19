# backend/services/oc_reader/parsers/metropol.py
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple, Any


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
class MetropolRegexContract:
    rx_oc_anchor: re.Pattern
    rx_oc_digits: re.Pattern
    rx_table_start: re.Pattern
    rx_end: re.Pattern
    rx_planta_only: re.Pattern
    rx_has_digit: re.Pattern
    rx_dash: re.Pattern
    rx_pcs_qty_inline: re.Pattern
    rx_qty_only: re.Pattern
    rx_date_any: re.Pattern


class MetropolParserContractRegistry(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.regex = MetropolRegexContract(
            rx_oc_anchor=re.compile(r"orden\s+compra\s+nro", re.IGNORECASE),
            rx_oc_digits=re.compile(r"\b(\d{5,10})\b"),
            rx_table_start=re.compile(r"^planta$", re.IGNORECASE),
            rx_end=re.compile(r"^(subtotal\b|canales\s+de\s+consulta\b)", re.IGNORECASE),
            rx_planta_only=re.compile(r"^[A-Z0-9]{3,6}$"),
            rx_has_digit=re.compile(r".*\d.*"),
            rx_dash=re.compile(r"\s+-\s+"),
            rx_pcs_qty_inline=re.compile(r"\bpcs\b\s*(\d{1,6})\b", re.IGNORECASE),
            rx_qty_only=re.compile(r"^\d{1,6}$"),
            rx_date_any=re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
        )

        self.header_noise_exact = {
            "n.o de pieza",
            "nro de pieza",
            "detalle",
            "precio unit.",
            "total",
        }

        self.header_noise_contains = (
            "u.med",
            "cantidad",
            "f. deseada",
        )

        self.stop_words_in_detail = (
            "subtotal",
            "iva",
            "otros impuestos",
            "total",
            "canales de consulta",
        )


class MetropolHelperFactory:
    @staticmethod
    def split_lines(text: str) -> List[str]:
        return [l.strip() for l in (text or "").splitlines() if l and l.strip()]

    @staticmethod
    def normalize_spaces(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "")).strip()

    @staticmethod
    def is_header_noise(line: str) -> bool:
        low = (line or "").strip().lower()
        if low in MetropolParserContractRegistry().header_noise_exact:
            return True
        return any(k in low for k in MetropolParserContractRegistry().header_noise_contains)

    @staticmethod
    def should_skip_detail_line(line: str) -> bool:
        low = (line or "").strip().lower()
        rx = MetropolParserContractRegistry().regex
        if rx.rx_date_any.search(line or ""):
            return True
        if re.fullmatch(r"[\d\.,]+", (line or "").strip()):
            return True
        if any(k in low for k in MetropolParserContractRegistry().stop_words_in_detail):
            return True
        return False

    @staticmethod
    def build_item(codigo: str, desc_parts: List[str], qty: int) -> Optional[Dict[str, Any]]:
        codigo = MetropolHelperFactory.normalize_spaces(codigo)
        if not codigo or qty <= 0:
            return None

        desc = MetropolHelperFactory.normalize_spaces(" ".join(desc_parts))
        if not desc:
            desc = codigo

        return {
            "descripcion": f"{codigo} - {desc}",
            "cantidad": int(qty),
        }


class MetropolResponseFactory:
    @staticmethod
    def build(numero_oc: Optional[str], items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "numero_oc": numero_oc,
            "cliente": "Metropol",
            "items": items,
        }


class MetropolHeaderParser:
    @staticmethod
    def extract_numero_oc(lines: List[str]) -> Optional[str]:
        rx = MetropolParserContractRegistry().regex
        numero_oc: Optional[str] = None

        for i, ln in enumerate(lines):
            if rx.rx_oc_anchor.search(ln):
                m = rx.rx_oc_digits.search(ln)
                if m:
                    return m.group(1)

                for j in range(1, 12):
                    k = i - j
                    if k >= 0:
                        m2 = rx.rx_oc_digits.fullmatch(lines[k]) or rx.rx_oc_digits.search(lines[k])
                        if m2:
                            return m2.group(1)

                    k2 = i + j
                    if k2 < len(lines):
                        m3 = rx.rx_oc_digits.fullmatch(lines[k2]) or rx.rx_oc_digits.search(lines[k2])
                        if m3:
                            return m3.group(1)

        for i, ln in enumerate(lines):
            if re.fullmatch(r"\d{5,10}", ln):
                window = " ".join(lines[max(0, i - 8): min(len(lines), i + 8)]).lower()
                if "orden" in window and "compra" in window:
                    numero_oc = ln
                    break

        return numero_oc


class MetropolItemsParser:
    @staticmethod
    def parse_start(line: str) -> Optional[Tuple[str, str]]:
        rx = MetropolParserContractRegistry().regex

        if " - " not in line:
            return None

        parts = rx.rx_dash.split(line, maxsplit=1)
        if len(parts) != 2:
            return None

        left = parts[0].strip()
        right = parts[1].strip()
        if not right:
            return None

        toks = left.split()

        if len(toks) >= 2 and rx.rx_planta_only.match(toks[0]) and rx.rx_has_digit.match(" ".join(toks[1:])):
            codigo = " ".join(toks[1:]).strip()
        else:
            codigo = left

        codigo = MetropolHelperFactory.normalize_spaces(codigo)

        if not rx.rx_has_digit.match(codigo):
            return None

        return codigo, right

    @staticmethod
    def extract_items(lines: List[str]) -> List[Dict[str, Any]]:
        rx = MetropolParserContractRegistry().regex
        items: List[Dict[str, Any]] = []

        start_idx: Optional[int] = None
        for i, ln in enumerate(lines):
            if rx.rx_table_start.match(ln):
                start_idx = i + 1
                break

        if start_idx is None:
            return items

        cur_codigo: Optional[str] = None
        cur_desc_parts: List[str] = []
        expecting_qty_next = False

        def flush(qty: int) -> None:
            nonlocal cur_codigo, cur_desc_parts, expecting_qty_next
            item = MetropolHelperFactory.build_item(cur_codigo or "", cur_desc_parts, qty)
            if item:
                items.append(item)
            cur_codigo = None
            cur_desc_parts = []
            expecting_qty_next = False

        for ln in lines[start_idx:]:
            if rx.rx_end.search(ln):
                break

            if MetropolHelperFactory.is_header_noise(ln):
                continue

            if expecting_qty_next and rx.rx_qty_only.match(ln):
                flush(int(ln))
                continue

            if cur_codigo:
                m_inline = rx.rx_pcs_qty_inline.search(ln)
                if m_inline:
                    flush(int(m_inline.group(1)))
                    continue

                if ln.strip().lower() == "pcs":
                    expecting_qty_next = True
                    continue

            st = MetropolItemsParser.parse_start(ln)
            if st:
                cur_codigo, first = st
                cur_desc_parts = [first] if first else []
                expecting_qty_next = False
                continue

            if cur_codigo is None and rx.rx_planta_only.match(ln):
                continue

            if cur_codigo and not expecting_qty_next:
                if MetropolHelperFactory.should_skip_detail_line(ln):
                    continue
                cur_desc_parts.append(ln)

        return items


def parse_metropol(text: str) -> Dict:
    if not text:
        return MetropolResponseFactory.build(numero_oc=None, items=[])

    lines = MetropolHelperFactory.split_lines(text)
    numero_oc = MetropolHeaderParser.extract_numero_oc(lines)
    items = MetropolItemsParser.extract_items(lines)

    return MetropolResponseFactory.build(
        numero_oc=numero_oc,
        items=items,
    )
