# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Protocol

import fitz  # PyMuPDF


# =========================================================
# EXCEPTIONS
# =========================================================
class OCParseError(Exception):
    """Error controlado cuando no se puede extraer información clave desde el PDF."""
    pass


# =========================================================
# DTOs
# =========================================================
@dataclass
class OCItem:
    codigo: str
    descripcion: str
    cantidad: int


@dataclass
class OCParsed:
    cliente: str
    numero_oc: str
    fecha: datetime
    monto: int
    descripcion: str
    items: List[OCItem]


# =========================================================
# SINGLETON META
# =========================================================
class SingletonMeta(type):
    _instances: dict[type, object] = {}
    _lock = threading.Lock()

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with cls._lock:
                if cls not in cls._instances:
                    cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


# =========================================================
# FACTORIES / HELPERS
# =========================================================
class OCRawTextFactory:
    """Encapsula la lectura de texto desde PDF."""

    @staticmethod
    def read_pdf_text(pdf_path: str) -> str:
        try:
            with fitz.open(pdf_path) as doc:
                parts: List[str] = []

                for page in doc:
                    t = (page.get_text("text") or "").strip()
                    if t:
                        parts.append(t)
                        continue

                    blocks = page.get_text("blocks") or []
                    btxt: List[str] = []
                    for b in blocks:
                        if len(b) >= 5 and isinstance(b[4], str):
                            s = b[4].strip()
                            if s:
                                btxt.append(s)

                    if btxt:
                        parts.append("\n".join(btxt))

                text = "\n".join(parts).strip()
                if not text:
                    raise OCParseError("Texto vacío extraído del PDF (posible PDF escaneado sin OCR).")
                return text

        except OCParseError:
            raise
        except Exception as e:
            raise OCParseError(f"No se pudo leer el PDF: {e}") from e


class NormalizeTextFactory:
    @staticmethod
    def normalize_ws(text: str) -> str:
        return re.sub(r"[ \t]+", " ", (text or "").replace("\r", ""))

    @staticmethod
    def norm_lines(text: str) -> List[str]:
        return [ln.strip() for ln in (text or "").splitlines() if ln and ln.strip()]


class DateFactory:
    @staticmethod
    def parse_date_any(date_str: str) -> datetime:
        value = (date_str or "").strip()
        for fmt in ("%d/%m/%Y", "%d/%m/%y"):
            try:
                return datetime.strptime(value, fmt)
            except Exception:
                pass
        raise OCParseError(f"Fecha inválida/no soportada: '{date_str}'")


class MoneyFactory:
    @staticmethod
    def parse_money_to_int(value: str) -> int:
        s = (value or "").strip()
        if not s:
            raise OCParseError("Monto vacío")

        s = s.replace("$", "").replace("CLP", "").replace("(", "").replace(")", "").strip()

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
        if not s:
            raise OCParseError(f"No se pudo interpretar monto desde: '{value}'")
        return int(s)


class OCParsedFactory:
    @staticmethod
    def build(
        cliente: str,
        numero_oc: str,
        fecha: datetime,
        monto: int,
        descripcion: str,
        items: List[OCItem],
    ) -> OCParsed:
        return OCParsed(
            cliente=cliente,
            numero_oc=numero_oc,
            fecha=fecha,
            monto=monto,
            descripcion=descripcion,
            items=items,
        )


# =========================================================
# PARSER PROTOCOL
# =========================================================
class OCParser(Protocol):
    name: str

    def matches(self, text: str) -> bool:
        ...

    def parse(self, text: str) -> OCParsed:
        ...


# =========================================================
# BASE PARSER
# =========================================================
class BaseOCParser:
    name = "base"

    def __init__(self) -> None:
        self.text_factory = NormalizeTextFactory()
        self.date_factory = DateFactory()
        self.money_factory = MoneyFactory()
        self.parsed_factory = OCParsedFactory()

    def matches(self, text: str) -> bool:
        raise NotImplementedError

    def parse(self, text: str) -> OCParsed:
        raise NotImplementedError


# =========================================================
# RBU PARSER
# =========================================================
class RBUOCParser(BaseOCParser):
    name = "rbu"

    def matches(self, text: str) -> bool:
        t = (text or "").upper()
        return ("REDBUS URBANO" in t) or (("ORDEN DE COMPRA" in t) and ("REDBUS" in t or "RBU" in t))

    def parse(self, text: str) -> OCParsed:
        raw = self.text_factory.normalize_ws(text)

        m_oc = re.search(r"Orden\s+de\s+Compra\s+N[°ºo]?\s*([0-9]{5,10})", raw, re.IGNORECASE)
        if not m_oc:
            m_oc = re.search(r"Orden\s+de\s+Compra.*?\b([0-9]{5,10})\b", raw, re.IGNORECASE)
        if not m_oc:
            raise OCParseError("No se detectó número de OC (RBU)")
        numero_oc = m_oc.group(1).strip()

        m_date = re.search(r"\b(\d{2}/\d{2}/\d{2,4})\b", raw)
        if not m_date:
            raise OCParseError("No se detectó fecha de la OC (RBU)")
        fecha = self.date_factory.parse_date_any(m_date.group(1))

        m_total = re.search(r"\bTotal\s+([\d\.,]+)\b", text, re.IGNORECASE)
        if not m_total:
            m_total = re.search(r"Total\s*\n\s*([\d\.,]+)", text, re.IGNORECASE)
        if not m_total:
            raise OCParseError("No se detectó monto total (RBU)")
        monto = self.money_factory.parse_money_to_int(m_total.group(1))

        cliente = "RBU"
        descripcion = f"OC {numero_oc} (RBU)"

        items: List[OCItem] = []
        for match in re.finditer(r"\n([^\n]{10,160})\n\s*(\d+)\s*\n\s*[\d\.,]+\s*\n", text):
            desc = (match.group(1) or "").strip()
            try:
                qty = int(match.group(2))
            except Exception:
                qty = 0

            if qty <= 0 or not desc:
                continue

            items.append(OCItem(codigo="N/A", descripcion=desc, cantidad=qty))
            if len(items) >= 80:
                break

        return self.parsed_factory.build(
            cliente=cliente,
            numero_oc=numero_oc,
            fecha=fecha,
            monto=monto,
            descripcion=descripcion,
            items=items,
        )


# =========================================================
# METROPOL PARSER
# =========================================================
class MetropolOCParser(BaseOCParser):
    name = "metropol"

    def matches(self, text: str) -> bool:
        t = (text or "").upper()
        return (
            ("ORDEN COMPRA NRO" in t)
            or ("ORDEN DE COMPRA NRO" in t)
            or ("TOTAL ( CLP" in t)
            or ("GRUPO" in t and "METROPOL" in t)
            or ("GRUPOMETROPOL" in t)
        )

    def parse(self, text: str) -> OCParsed:
        lines = self.text_factory.norm_lines(text)

        rx_oc_anchor = re.compile(r"orden\s+(de\s+)?compra\s+nro", re.IGNORECASE)
        rx_oc_digits = re.compile(r"\b(\d{5,10})\b")
        numero_oc: Optional[str] = None

        for i, ln in enumerate(lines):
            if rx_oc_anchor.search(ln):
                m = rx_oc_digits.search(ln)
                if m:
                    numero_oc = m.group(1)
                    break

                for j in range(1, 12):
                    k = i - j
                    if k >= 0:
                        m2 = rx_oc_digits.fullmatch(lines[k]) or rx_oc_digits.search(lines[k])
                        if m2:
                            val = m2.group(1)
                            if len(val) >= 5:
                                numero_oc = val
                                break

                    k2 = i + j
                    if k2 < len(lines):
                        m3 = rx_oc_digits.fullmatch(lines[k2]) or rx_oc_digits.search(lines[k2])
                        if m3:
                            val = m3.group(1)
                            if len(val) >= 5:
                                numero_oc = val
                                break

                if numero_oc:
                    break

        if not numero_oc:
            for i, ln in enumerate(lines):
                if re.fullmatch(r"\d{5,10}", ln):
                    window = " ".join(lines[max(0, i - 8): min(len(lines), i + 8)]).lower()
                    if "orden" in window and "compra" in window:
                        numero_oc = ln
                        break

        if not numero_oc:
            raise OCParseError("No se detectó número de OC (Metropol)")

        rx_date = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")
        fecha_str: Optional[str] = None

        for i, ln in enumerate(lines):
            low = ln.lower()
            if "fecha" in low and "orden" in low:
                m = rx_date.search(ln)
                if m:
                    fecha_str = m.group(1)
                    break

                for j in range(1, 12):
                    k = i - j
                    if k >= 0:
                        m2 = rx_date.search(lines[k])
                        if m2:
                            fecha_str = m2.group(1)
                            break

                    k2 = i + j
                    if k2 < len(lines):
                        m3 = rx_date.search(lines[k2])
                        if m3:
                            fecha_str = m3.group(1)
                            break

                if fecha_str:
                    break

        if not fecha_str:
            raise OCParseError("No se detectó fecha de la OC (Metropol)")
        fecha = self.date_factory.parse_date_any(fecha_str)

        monto: Optional[int] = None
        for i, ln in enumerate(lines):
            up = ln.upper()
            if up.startswith("TOTAL") and "CLP" in up:
                m = re.search(r"([\d\.,]+)", ln)
                if m:
                    monto = self.money_factory.parse_money_to_int(m.group(1))
                    break

                if i + 1 < len(lines):
                    m2 = re.search(r"([\d\.,]+)", lines[i + 1])
                    if m2:
                        monto = self.money_factory.parse_money_to_int(m2.group(1))
                        break
                break

        if monto is None:
            raise OCParseError("No se detectó TOTAL (CLP) (Metropol)")

        cliente = "Metropol"
        descripcion = f"OC {numero_oc} (Metropol)"
        items: List[OCItem] = []

        return self.parsed_factory.build(
            cliente=cliente,
            numero_oc=numero_oc,
            fecha=fecha,
            monto=monto,
            descripcion=descripcion,
            items=items,
        )


# =========================================================
# REGISTRY
# =========================================================
class OCParserRegistry(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self._parsers: List[OCParser] = []
        self._bootstrapped = False
        self._bootstrap()

    def _bootstrap(self) -> None:
        if self._bootstrapped:
            return
        self.register(RBUOCParser())
        self.register(MetropolOCParser())
        self._bootstrapped = True

    def register(self, parser: OCParser) -> None:
        self._parsers.append(parser)

    def get_parser_for_text(self, text: str) -> OCParser:
        for parser in self._parsers:
            if parser.matches(text):
                return parser
        raise OCParseError("Tipo de OC no reconocido (no coincide RBU ni Metropol)")

    def list_parser_names(self) -> List[str]:
        return [getattr(p, "name", p.__class__.__name__) for p in self._parsers]


# =========================================================
# SERVICE
# =========================================================
class OCLectorService(metaclass=SingletonMeta):
    def __init__(
        self,
        registry: Optional[OCParserRegistry] = None,
        raw_text_factory: Optional[OCRawTextFactory] = None,
    ) -> None:
        self.registry = registry or OCParserRegistry()
        self.raw_text_factory = raw_text_factory or OCRawTextFactory()

    def leer_pdf(self, pdf_path: str) -> OCParsed:
        text = self.raw_text_factory.read_pdf_text(pdf_path)
        parser = self.registry.get_parser_for_text(text)
        return parser.parse(text)


# =========================================================
# API LEGACY COMPATIBLE
# =========================================================
def leer_oc_pdf(pdf_path: str) -> OCParsed:
    return OCLectorService().leer_pdf(pdf_path)
