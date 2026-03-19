# backend/routes/ocs.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Dict, Any, Optional, List

from fastapi import APIRouter, HTTPException, UploadFile, File, Query, Request
from backend.config import get_db_connection

# =========================================================
# OCR / PARSE STACK
# =========================================================
try:
    from backend.services.oc_reader.extract import extract_text_from_pdf
except Exception:
    extract_text_from_pdf = None

try:
    from backend.services.oc_reader.detect import detect_oc_type
except Exception:
    detect_oc_type = None

try:
    from backend.services.oc_reader.parsers.rbu import parse_rbu
except Exception:
    parse_rbu = None

try:
    from backend.services.oc_reader.parsers.metropol import parse_metropol
except Exception:
    parse_metropol = None

try:
    from backend.services.oc_reader.parsers.gran_america import parse_gran_america
except Exception:
    parse_gran_america = None

# fallback legacy
try:
    from backend.services.lector_ocs import leer_oc_pdf, OCParseError
except Exception:
    leer_oc_pdf = None
    class OCParseError(Exception):
        pass

try:
    from zoneinfo import ZoneInfo
    TZ_CL = ZoneInfo("America/Santiago")
except Exception:
    TZ_CL = None

router = APIRouter(prefix="/api/ocs", tags=["OCS"])


# =========================================================
# SINGLETON META
# =========================================================
class SingletonMeta(type):
    _instances: Dict[type, object] = {}
    _lock = threading.Lock()

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with cls._lock:
                if cls not in cls._instances:
                    cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


# =========================================================
# CONFIG / CONTRACT
# =========================================================
@dataclass(frozen=True)
class OCSConfig:
    upload_dir: str
    upload_public_prefix: str
    allowed_ext: frozenset[str]


class OCSConfigFactory:
    @staticmethod
    def from_env() -> OCSConfig:
        return OCSConfig(
            upload_dir=os.getenv("OCS_UPLOAD_DIR", "frontend/static/uploads/ocs"),
            upload_public_prefix=os.getenv("OCS_UPLOAD_PUBLIC_PREFIX", "/static/uploads/ocs"),
            allowed_ext=frozenset({".pdf"}),
        )


class OCSContractRegistry(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.config = OCSConfigFactory.from_env()
        self.day_names = ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"]


# =========================================================
# STORAGE
# =========================================================
class OCSStorageFactory(metaclass=SingletonMeta):
    def save_upload_pdf(self, file: UploadFile) -> Dict[str, str]:
        cfg = OCSContractRegistry().config
        filename = OCSHelperFactory.safe_filename(file.filename)
        ext = os.path.splitext(filename)[1].lower()

        if ext not in cfg.allowed_ext:
            raise HTTPException(status_code=400, detail="Formato no soportado. Solo PDF.")

        os.makedirs(cfg.upload_dir, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_name = f"oc_{stamp}_{filename}"
        abs_path = os.path.join(cfg.upload_dir, final_name)

        try:
            with open(abs_path, "wb") as f:
                shutil.copyfileobj(file.file, f)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"No se pudo guardar el archivo: {str(e)}")

        public_url = f"{cfg.upload_public_prefix}/{final_name}"
        return {"abs_path": abs_path, "public_url": public_url, "filename": final_name}


# =========================================================
# HELPERS
# =========================================================
class OCSHelperFactory:
    _rx_date_any = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")
    _rx_date_iso = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

    @staticmethod
    def safe_filename(name: str) -> str:
        base = os.path.basename(name or "oc.pdf")
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
        if not base.lower().endswith(".pdf"):
            base += ".pdf"
        return base

    @staticmethod
    def today_cl_date() -> date:
        if TZ_CL is None:
            return date.today()
        return datetime.now(TZ_CL).date()

    @staticmethod
    def is_business_day(d: date) -> bool:
        return d.weekday() < 5

    @staticmethod
    def add_business_days(start: date, days: int) -> date:
        d = start
        added = 0
        while added < days:
            d += timedelta(days=1)
            if OCSHelperFactory.is_business_day(d):
                added += 1
        return d

    @staticmethod
    def business_days_between(a: date, b: date) -> int:
        sign = 1
        if b < a:
            a, b = b, a
            sign = -1
        d = a
        count = 0
        while d < b:
            d += timedelta(days=1)
            if OCSHelperFactory.is_business_day(d):
                count += 1
        return count * sign

    @staticmethod
    def norm_cliente_bucket(cliente: str) -> str:
        c = (cliente or "").strip().lower()
        if "rbu" in c or "redbus" in c:
            return "RBU"
        if "metropol" in c:
            return "Metropol"
        if "gran am" in c or "gran america" in c or c == "ga":
            return "GA"
        return "Otros"

    @staticmethod
    def normalize_cliente(cliente: str) -> str:
        c = (cliente or "").strip().lower()
        if "rbu" in c or "redbus" in c:
            return "RBU"
        if "metropol" in c:
            return "Metropol"
        if "gran am" in c or "gran america" in c or c == "ga":
            return "Gran America"
        return (cliente or "Otros").strip() or "Otros"

    @staticmethod
    def has_column(cur, table: str, column: str) -> bool:
        cur.execute(
            """
            SELECT COUNT(*) AS c
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
              AND COLUMN_NAME = %s
            """,
            (table, column),
        )
        row = cur.fetchone() or {}
        return int(row.get("c", 0) or 0) > 0

    @staticmethod
    def parse_money_to_int(value: str) -> int:
        s = (value or "").strip()
        if not s:
            return 0

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
        return int(s) if s else 0

    @staticmethod
    def parse_fecha_to_date(value: str) -> date:
        s = (value or "").strip()
        if not s:
            return OCSHelperFactory.today_cl_date()

        for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt).date()
            except Exception:
                pass

        m = OCSHelperFactory._rx_date_any.search(s)
        if m:
            return OCSHelperFactory.parse_fecha_to_date(m.group(1))

        m = OCSHelperFactory._rx_date_iso.search(s)
        if m:
            return OCSHelperFactory.parse_fecha_to_date(m.group(1))

        return OCSHelperFactory.today_cl_date()

    @staticmethod
    def extract_metropol_fecha(text: str) -> Optional[str]:
        lines = [ln.strip() for ln in (text or "").splitlines() if ln and ln.strip()]
        if not lines:
            return None

        for i, ln in enumerate(lines):
            low = ln.lower()
            if "fecha" in low and "orden" in low:
                m = OCSHelperFactory._rx_date_any.search(ln)
                if m:
                    return m.group(1)
                for j in range(1, 12):
                    if i - j >= 0:
                        m2 = OCSHelperFactory._rx_date_any.search(lines[i - j])
                        if m2:
                            return m2.group(1)
                    if i + j < len(lines):
                        m3 = OCSHelperFactory._rx_date_any.search(lines[i + j])
                        if m3:
                            return m3.group(1)

        for ln in lines[:80]:
            m = OCSHelperFactory._rx_date_any.search(ln)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def extract_metropol_total(text: str) -> int:
        lines = [ln.strip() for ln in (text or "").splitlines() if ln and ln.strip()]
        for i, ln in enumerate(lines):
            up = ln.upper()
            if up.startswith("TOTAL") and "CLP" in up:
                m = re.search(r"([\d\.,]+)", ln)
                if m:
                    return OCSHelperFactory.parse_money_to_int(m.group(1))
                if i + 1 < len(lines):
                    m2 = re.search(r"([\d\.,]+)", lines[i + 1])
                    if m2:
                        return OCSHelperFactory.parse_money_to_int(m2.group(1))
                break
        return 0

    @staticmethod
    def normalize_items(items: Any) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not isinstance(items, list):
            return out

        for it in items[:500]:
            if not isinstance(it, dict):
                continue

            desc = str(it.get("descripcion") or "").strip()
            if not desc:
                continue

            try:
                qty = int(it.get("cantidad") or 1)
            except Exception:
                qty = 1

            if qty <= 0:
                qty = 1

            out.append({
                "descripcion": desc[:255],
                "cantidad": qty,
            })
        return out


# =========================================================
# PARSER FACTORY
# =========================================================
class OCSParserFactory(metaclass=SingletonMeta):
    def detect_type(self, text: str) -> str:
        if not text or detect_oc_type is None:
            return "UNKNOWN"
        try:
            return detect_oc_type(text) or "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    def parse_by_type(self, oc_type: str, text: str, pdf_path: str) -> Dict[str, Any]:
        t = (oc_type or "").strip().upper()

        if t == "RBU":
            if parse_rbu is not None:
                raw = parse_rbu(text) or {}
                return {
                    "numero_oc": str(raw.get("numero_oc") or "").strip(),
                    "cliente": "RBU",
                    "fecha": str(raw.get("fecha") or "").strip(),
                    "monto": int(raw.get("monto") or 0),
                    "items": OCSHelperFactory.normalize_items(raw.get("items") or []),
                }

        if t == "METROPOL":
            if parse_metropol is not None:
                raw = parse_metropol(text) or {}
                return {
                    "numero_oc": str(raw.get("numero_oc") or "").strip(),
                    "cliente": "Metropol",
                    "fecha": OCSHelperFactory.extract_metropol_fecha(text) or "",
                    "monto": int(OCSHelperFactory.extract_metropol_total(text) or 0),
                    "items": OCSHelperFactory.normalize_items(raw.get("items") or []),
                }

        if t == "GRAN_AMERICA":
            if parse_gran_america is not None:
                raw = parse_gran_america(text) or {}
                return {
                    "numero_oc": str(raw.get("numero_oc") or "").strip(),
                    "cliente": "Gran America",
                    "fecha": "",
                    "monto": 0,
                    "items": OCSHelperFactory.normalize_items(raw.get("items") or []),
                }

        if leer_oc_pdf is None:
            raise HTTPException(status_code=422, detail=f"Tipo de OC no reconocido o parser no disponible: {t}")

        try:
            ocp = leer_oc_pdf(pdf_path)
            return {
                "numero_oc": str(getattr(ocp, "numero_oc", "") or "").strip(),
                "cliente": OCSHelperFactory.normalize_cliente(str(getattr(ocp, "cliente", "") or "Otros")),
                "fecha": getattr(ocp, "fecha", None).strftime("%d/%m/%Y") if getattr(ocp, "fecha", None) else "",
                "monto": int(getattr(ocp, "monto", 0) or 0),
                "items": OCSHelperFactory.normalize_items([
                    {
                        "descripcion": getattr(it, "descripcion", ""),
                        "cantidad": getattr(it, "cantidad", 1),
                    }
                    for it in (getattr(ocp, "items", []) or [])
                ]),
            }
        except OCParseError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error parseando OC: {str(e)}")


# =========================================================
# DB FACTORY
# =========================================================
class OCSDBFactory(metaclass=SingletonMeta):
    def insert_or_get_oc(
        self,
        cur,
        numero_oc: str,
        cliente: str,
        fecha_dt: date,
        monto: int,
        public_url: str,
    ) -> int:
        cur.execute(
            "SELECT id FROM ocs WHERE numero_oc=%s AND cliente=%s ORDER BY id DESC LIMIT 1",
            (numero_oc, cliente),
        )
        ex = cur.fetchone()
        if ex and ex.get("id"):
            oc_id = int(ex["id"])
            cur.execute(
                "UPDATE ocs SET archivo_url=COALESCE(archivo_url, %s) WHERE id=%s",
                (public_url, oc_id),
            )
            return oc_id

        descripcion = f"OC {numero_oc} ({cliente})"
        cur.execute(
            """
            INSERT INTO ocs (numero_oc, descripcion, cliente, monto, estado, fecha, archivo_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (numero_oc, descripcion, cliente, int(monto or 0), "Abierta", fecha_dt, public_url),
        )
        return int(cur.lastrowid)

    def insert_items(self, cur, oc_id: int, items: List[Dict[str, Any]]) -> None:
        if not items:
            return

        has_qty_ent = OCSHelperFactory.has_column(cur, "oc_items", "cantidad_entregada")

        for it in items[:500]:
            desc = str(it.get("descripcion") or "").strip()
            if not desc:
                continue

            try:
                cant_int = int(it.get("cantidad") or 1)
            except Exception:
                cant_int = 1

            if cant_int <= 0:
                cant_int = 1

            if has_qty_ent:
                cur.execute(
                    """
                    INSERT INTO oc_items (oc_id, descripcion, cantidad, cantidad_entregada, entregado)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (oc_id, desc[:255], cant_int, 0, 0),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO oc_items (oc_id, descripcion, cantidad, entregado)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (oc_id, desc[:255], cant_int, 0),
                )


# =========================================================
# SERVICE
# =========================================================
class OCSService(metaclass=SingletonMeta):
    def listar_ocs(self) -> List[Dict[str, Any]]:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(dictionary=True)

            hoy = OCSHelperFactory.today_cl_date()
            plazo_dias = 15
            alerta_dias = 5

            has_qty_ent = OCSHelperFactory.has_column(cur, "oc_items", "cantidad_entregada")

            cur.execute(
                """
                SELECT id, numero_oc, descripcion, cliente, monto, estado, fecha, archivo_url
                FROM ocs
                ORDER BY id DESC
                """
            )
            ocs_rows = cur.fetchall() or []

            if has_qty_ent:
                cur.execute(
                    """
                    SELECT
                      oc_id,
                      COUNT(*) AS total_items,
                      SUM(CASE WHEN COALESCE(cantidad_entregada,0) > 0 THEN 1 ELSE 0 END) AS items_con_mov,
                      SUM(
                        CASE
                          WHEN COALESCE(cantidad_entregada,0) >= COALESCE(cantidad,0)
                               AND COALESCE(cantidad,0) > 0
                          THEN 1 ELSE 0
                        END
                      ) AS items_completos,
                      SUM(GREATEST(COALESCE(cantidad,0) - COALESCE(cantidad_entregada,0), 0)) AS unidades_pendientes
                    FROM oc_items
                    GROUP BY oc_id
                    """
                )
                agg_rows = cur.fetchall() or []
            else:
                cur.execute(
                    """
                    SELECT
                      oc_id,
                      COUNT(*) AS total_items,
                      SUM(CASE WHEN entregado=1 THEN 1 ELSE 0 END) AS items_completos
                    FROM oc_items
                    GROUP BY oc_id
                    """
                )
                agg_rows = cur.fetchall() or []

            agg_map = {}
            for r in agg_rows:
                oc_id = int(r.get("oc_id") or 0)
                agg_map[oc_id] = {
                    "total_items": int(r.get("total_items") or 0),
                    "items_con_mov": int(r.get("items_con_mov") or 0),
                    "items_completos": int(r.get("items_completos") or 0),
                    "unidades_pendientes": int(r.get("unidades_pendientes") or 0),
                }

            def _to_date(v):
                if not v:
                    return None
                if hasattr(v, "date"):
                    try:
                        return v.date()
                    except Exception:
                        pass
                if isinstance(v, date):
                    return v
                try:
                    return datetime.fromisoformat(str(v).replace("Z", "")).date()
                except Exception:
                    pass
                for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
                    try:
                        return datetime.strptime(str(v), fmt).date()
                    except Exception:
                        continue
                return None

            out = []
            for oc in ocs_rows:
                oc_id = int(oc.get("id") or 0)
                agg = agg_map.get(
                    oc_id,
                    {
                        "total_items": 0,
                        "items_con_mov": 0,
                        "items_completos": 0,
                        "unidades_pendientes": 0,
                    },
                )

                total_items = int(agg.get("total_items") or 0)
                items_con_mov = int(agg.get("items_con_mov") or 0)
                items_completos = int(agg.get("items_completos") or 0)
                items_pendientes = max(0, total_items - items_completos)
                unidades_pendientes = int(agg.get("unidades_pendientes") or 0)

                estado_db = str(oc.get("estado") or "").strip()

                if estado_db.lower() == "cerrada":
                    estado_operativo = "Cerrada"
                elif total_items <= 0:
                    estado_operativo = "Observar"
                elif items_completos >= total_items:
                    estado_operativo = "Cerrada"
                elif items_con_mov > 0:
                    estado_operativo = "En proceso"
                else:
                    estado_operativo = "Pendiente"

                fecha_base = _to_date(oc.get("fecha"))
                fecha_vencimiento = None
                dias_habiles_restantes = None
                alerta = False
                atrasada = False
                dias_atraso = 0

                if fecha_base:
                    fecha_vencimiento = OCSHelperFactory.add_business_days(fecha_base, int(plazo_dias))
                    dias_habiles_restantes = OCSHelperFactory.business_days_between(hoy, fecha_vencimiento)
                    if dias_habiles_restantes < 0:
                        atrasada = True
                        dias_atraso = abs(int(dias_habiles_restantes))
                    elif dias_habiles_restantes <= int(alerta_dias):
                        alerta = True

                empresa = OCSHelperFactory.normalize_cliente(str(oc.get("cliente") or "Otros"))

                progreso_pct = 0
                if total_items > 0:
                    try:
                        progreso_pct = int(round((items_completos / total_items) * 100))
                    except Exception:
                        progreso_pct = 0

                out.append(
                    {
                        "id": oc_id,
                        "numero_oc": oc.get("numero_oc"),
                        "descripcion": oc.get("descripcion"),
                        "cliente": oc.get("cliente"),
                        "empresa": empresa,
                        "monto": int(oc.get("monto") or 0),
                        "estado": estado_operativo,
                        "estado_db": estado_db,
                        "estado_operativo": estado_operativo,
                        "fecha": fecha_base.isoformat() if fecha_base else None,
                        "fecha_vencimiento": fecha_vencimiento.isoformat() if fecha_vencimiento else None,
                        "dias_habiles_restantes": dias_habiles_restantes,
                        "atrasada": atrasada,
                        "alerta": alerta,
                        "dias_atraso": dias_atraso,
                        "archivo_url": oc.get("archivo_url"),
                        "total_items": total_items,
                        "items_completos": items_completos,
                        "items_pendientes": items_pendientes,
                        "items_con_mov": items_con_mov,
                        "unidades_pendientes": unidades_pendientes,
                        "progreso_pct": progreso_pct,
                    }
                )

            return out

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error al listar OCs: {str(e)}")
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def resumen_general(self, periodo: str, alerta_dias: int, plazo_dias: int) -> Dict[str, Any]:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(dictionary=True)

            hoy = OCSHelperFactory.today_cl_date()

            dias = []
            d = hoy
            while len(dias) < 5:
                if OCSHelperFactory.is_business_day(d):
                    dias.append(d)
                d -= timedelta(days=1)
            dias.sort()

            start_sem = dias[0]
            end_sem = dias[-1]
            start_sem_dt = datetime.combine(start_sem, datetime.min.time())
            end_sem_dt_excl = datetime.combine(end_sem + timedelta(days=1), datetime.min.time())

            has_qty_ent = OCSHelperFactory.has_column(cur, "oc_items", "cantidad_entregada")

            cur.execute(
                """
                SELECT id, numero_oc, cliente, monto, fecha, archivo_url, estado
                FROM ocs
                ORDER BY id DESC
                """
            )
            ocs_rows = cur.fetchall() or []

            if has_qty_ent:
                cur.execute(
                    """
                    SELECT
                      oc_id,
                      COUNT(*) AS total,
                      SUM(CASE WHEN COALESCE(cantidad_entregada,0) > 0 THEN 1 ELSE 0 END) AS con_mov,
                      SUM(CASE WHEN COALESCE(cantidad_entregada,0) >= COALESCE(cantidad,0)
                                AND COALESCE(cantidad,0) > 0 THEN 1 ELSE 0 END) AS completos
                    FROM oc_items
                    GROUP BY oc_id
                    """
                )
                items_map = {
                    r["oc_id"]: {
                        "total": int(r["total"] or 0),
                        "con_mov": int(r["con_mov"] or 0),
                        "completos": int(r["completos"] or 0),
                    }
                    for r in (cur.fetchall() or [])
                }
            else:
                cur.execute(
                    """
                    SELECT
                      oc_id,
                      COUNT(*) AS total,
                      SUM(CASE WHEN entregado=1 THEN 1 ELSE 0 END) AS completados
                    FROM oc_items
                    GROUP BY oc_id
                    """
                )
                items_map = {
                    r["oc_id"]: {
                        "total": int(r["total"] or 0),
                        "con_mov": int(r["completados"] or 0),
                        "completos": int(r["completados"] or 0),
                    }
                    for r in (cur.fetchall() or [])
                }

            observar = 0
            pendientes = 0
            en_proceso = 0
            cerradas_operativas = 0
            por_vencer = []
            vencidas = []
            activas_operativas = 0

            for oc in ocs_rows:
                oc_id = int(oc["id"])
                agg = items_map.get(oc_id, {"total": 0, "con_mov": 0, "completos": 0})
                total_items = int(agg["total"] or 0)

                if total_items == 0:
                    estado_op = "Observar"
                    observar += 1
                    activa = True
                else:
                    if int(agg["completos"] or 0) >= total_items:
                        estado_op = "Cerrada"
                        cerradas_operativas += 1
                        activa = False
                    elif int(agg["con_mov"] or 0) == 0:
                        estado_op = "Pendiente"
                        pendientes += 1
                        activa = True
                    else:
                        estado_op = "En proceso"
                        en_proceso += 1
                        activa = True

                if estado_op in ("Pendiente", "En proceso"):
                    activas_operativas += 1

                if activa and oc.get("fecha"):
                    f = oc["fecha"]
                    fecha_base = f.date() if hasattr(f, "date") else hoy
                    venc = OCSHelperFactory.add_business_days(fecha_base, int(plazo_dias))
                    dias_rest = OCSHelperFactory.business_days_between(hoy, venc)

                    row = {
                        "id": oc_id,
                        "numero_oc": oc.get("numero_oc"),
                        "cliente": oc.get("cliente"),
                        "monto": oc.get("monto"),
                        "fecha": fecha_base.isoformat(),
                        "fecha_vencimiento": venc.isoformat(),
                        "dias_habiles_restantes": dias_rest,
                        "estado_operativo": estado_op,
                        "archivo_url": oc.get("archivo_url"),
                    }

                    if dias_rest < 0:
                        vencidas.append(row)
                    elif dias_rest <= int(alerta_dias):
                        por_vencer.append(row)

            start_30d = hoy - timedelta(days=30)
            start_30d_dt = datetime.combine(start_30d, datetime.min.time())
            end_30d_dt_excl = datetime.combine(hoy + timedelta(days=1), datetime.min.time())

            dist = {"RBU": 0, "Metropol": 0, "GA": 0, "Otros": 0}
            cur.execute(
                """
                SELECT cliente, SUM(monto) AS total
                FROM ocs
                WHERE fecha >= %s AND fecha < %s
                GROUP BY cliente
                """,
                (start_30d_dt, end_30d_dt_excl),
            )
            for r in (cur.fetchall() or []):
                bucket = OCSHelperFactory.norm_cliente_bucket(r.get("cliente"))
                dist[bucket] += int(r.get("total") or 0)

            cur.execute(
                """
                SELECT DATE(fecha) AS f, SUM(monto) AS total
                FROM ocs
                WHERE fecha >= %s AND fecha < %s
                GROUP BY DATE(fecha)
                """,
                (start_sem_dt, end_sem_dt_excl),
            )
            week_map = {}
            for r in (cur.fetchall() or []):
                fd = r.get("f")
                if isinstance(fd, date):
                    week_map[fd] = int(r.get("total") or 0)

            labels = []
            data = []
            for dd in dias:
                labels.append(OCSContractRegistry().day_names[dd.weekday()])
                data.append(int(week_map.get(dd, 0) or 0))

            
            # ======================================================
            # INGRESOS HISTORICOS POR CLIENTE
            # ======================================================
            ingresos_por_cliente = {
                "RBU": 0,
                "Metropol": 0,
                "GA": 0,
                "Otros": 0,
            }

            cur.execute(
                """
                SELECT cliente, SUM(COALESCE(monto,0)) AS total
                FROM ocs
                GROUP BY cliente
                """
            )
            for r in (cur.fetchall() or []):
                bucket = OCSHelperFactory.norm_cliente_bucket(r.get("cliente"))
                ingresos_por_cliente[bucket] = ingresos_por_cliente.get(bucket, 0) + int(r.get("total") or 0)

            ingresos_periodo_total = int(sum(ingresos_por_cliente.values()) or 0)

            # ======================================================
            # SERIE MENSUAL ULTIMOS 12 MESES
            # ======================================================
            cur.execute(
                """
                SELECT DATE_FORMAT(fecha, '%Y-%m') AS mes, SUM(COALESCE(monto,0)) AS total
                FROM ocs
                WHERE fecha >= DATE_SUB(CURDATE(), INTERVAL 12 MONTH)
                GROUP BY DATE_FORMAT(fecha, '%Y-%m')
                ORDER BY mes ASC
                """
            )
            mensual_rows = cur.fetchall() or []
            mensual_labels = [str(r.get("mes") or "") for r in mensual_rows]
            mensual_data = [int(r.get("total") or 0) for r in mensual_rows]

            # ======================================================
            # SERIE SEMANAL REAL
            # ======================================================
            serie_labels = labels
            serie_data = data

            
            # ======================================================
            # INGRESOS HISTORICOS POR CLIENTE
            # ======================================================
            ingresos_por_cliente = {
                "RBU": 0,
                "Metropol": 0,
                "GA": 0,
                "Otros": 0,
            }

            cur.execute(
                """
                SELECT cliente, SUM(COALESCE(monto,0)) AS total
                FROM ocs
                GROUP BY cliente
                """
            )
            for r in (cur.fetchall() or []):
                bucket = OCSHelperFactory.norm_cliente_bucket(r.get("cliente"))
                if bucket not in ingresos_por_cliente:
                    bucket = "Otros"
                ingresos_por_cliente[bucket] += int(r.get("total") or 0)

            ingresos_periodo_total = int(sum(ingresos_por_cliente.values()) or 0)

            # ======================================================
            # INGRESOS POR MES (ULTIMOS 12 MESES)
            # ======================================================
            cur.execute(
                """
                SELECT DATE_FORMAT(fecha, '%Y-%m') AS mes, SUM(COALESCE(monto,0)) AS total
                FROM ocs
                WHERE fecha >= DATE_SUB(CURDATE(), INTERVAL 12 MONTH)
                GROUP BY DATE_FORMAT(fecha, '%Y-%m')
                ORDER BY mes ASC
                """
            )
            mensual_rows = cur.fetchall() or []
            mensual_labels = [str(r.get("mes") or "") for r in mensual_rows]
            mensual_data = [int(r.get("total") or 0) for r in mensual_rows]

            # ======================================================
            # SALIDA FINAL
            # ======================================================
            return {
                "activas": activas_operativas,
                "pendientes": pendientes,
                "en_proceso": en_proceso,
                "finalizadas": cerradas_operativas,

                "total_ocs_activas": activas_operativas,
                "ocs_pendientes_count": pendientes,
                "ocs_en_proceso_count": en_proceso,
                "total_ocs_cerradas": cerradas_operativas,
                "ocs_por_vencer_count": len(por_vencer),
                "ocs_atrasadas_count": len(vencidas),
                "ocs_vencidas_count": len(vencidas),
                "ocs_observar_count": observar,

                "lista_por_vencer": por_vencer,
                "lista_vencidas": vencidas,
                "lista_atrasadas": vencidas,

                "ingresos_periodo_total": ingresos_periodo_total,
                "ingresos_por_cliente": ingresos_por_cliente,

                "serie_semanal": {
                    "labels": labels,
                    "data": data,
                },

                "serie_estados_semanal": {
                    "labels": ["Pendiente", "En Proceso", "Atrasada", "Observar"],
                    "datasets": [
                        {
                            "label": "OCs",
                            "data": [pendientes, en_proceso, len(vencidas), observar]
                        }
                    ],
                },

                # compatibilidad frontend actual
                "clientes": {
                    "labels": list(ingresos_por_cliente.keys()),
                    "values": list(ingresos_por_cliente.values()),
                },
                "semanal": {
                    "labels": labels,
                    "values": data,
                },

                # bloque ERP extra
                "erp": {
                    "facturacion_total": ingresos_periodo_total,
                    "ticket_promedio": int(ingresos_periodo_total / len(ocs_rows)) if ocs_rows else 0,
                    "oc_mayor": max([int(x.get("monto") or 0) for x in ocs_rows], default=0),
                    "ingresos_por_cliente": [
                        {"cliente": k, "total": v}
                        for k, v in ingresos_por_cliente.items()
                    ],
                    "ingresos_por_mes": [
                        {"mes": mensual_labels[i], "total": mensual_data[i]}
                        for i in range(len(mensual_labels))
                    ]
                }
            }

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error en resumen_general: {str(e)}")
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    async def importar_ocr(self, archivo: UploadFile) -> Dict[str, Any]:
        if archivo is None:
            raise HTTPException(status_code=400, detail="Debe adjuntar un archivo PDF.")

        saved = OCSStorageFactory().save_upload_pdf(archivo)
        pdf_path = saved["abs_path"]
        public_url = saved["public_url"]

        if extract_text_from_pdf is None:
            raise HTTPException(status_code=500, detail="Modulo extract_text_from_pdf no disponible.")

        try:
            text = extract_text_from_pdf(pdf_path, lang="spa", debug=False, force_ocr=False)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"No se pudo extraer texto del PDF: {str(e)}")

        if not text or len(text.strip()) < 20:
            raise HTTPException(status_code=422, detail="No se pudo leer el contenido del PDF (texto vacio).")

        oc_type = OCSParserFactory().detect_type(text)

        try:
            parsed = OCSParserFactory().parse_by_type(oc_type, text, pdf_path)
        except HTTPException:
            raise
        except OCParseError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error parseando OC: {str(e)}")

        numero_oc = str(parsed.get("numero_oc") or "").strip()
        cliente = OCSHelperFactory.normalize_cliente(str(parsed.get("cliente") or "Otros"))
        fecha_dt = OCSHelperFactory.parse_fecha_to_date(str(parsed.get("fecha") or ""))
        monto = int(parsed.get("monto") or 0)
        items = OCSHelperFactory.normalize_items(parsed.get("items") or [])

        if not numero_oc:
            raise HTTPException(status_code=422, detail="No se detecto numero de OC.")

        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(dictionary=True)

            oc_id = OCSDBFactory().insert_or_get_oc(
                cur=cur,
                numero_oc=numero_oc,
                cliente=cliente,
                fecha_dt=fecha_dt,
                monto=monto,
                public_url=public_url,
            )

            cur.execute("SELECT COUNT(*) AS c FROM oc_items WHERE oc_id=%s", (oc_id,))
            row = cur.fetchone() or {}
            if int(row.get("c", 0) or 0) == 0:
                OCSDBFactory().insert_items(cur, oc_id, items)

            conn.commit()
            return {
                "ok": True,
                "id": oc_id,
                "numero_oc": numero_oc,
                "cliente": cliente,
                "tipo_detectado": oc_type,
                "items_insertados": len(items),
            }

        except HTTPException:
            raise
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise HTTPException(status_code=500, detail=f"Error guardando OC en BD: {str(e)}")
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def listar_items_oc(self, oc_id: int) -> List[Dict[str, Any]]:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(dictionary=True)

            has_qty_ent = OCSHelperFactory.has_column(cur, "oc_items", "cantidad_entregada")

            if has_qty_ent:
                cur.execute(
                    """
                    SELECT id, oc_id, descripcion, cantidad, cantidad_entregada, entregado
                    FROM oc_items
                    WHERE oc_id=%s
                    ORDER BY id ASC
                    """,
                    (oc_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT id, oc_id, descripcion, cantidad, entregado
                    FROM oc_items
                    WHERE oc_id=%s
                    ORDER BY id ASC
                    """,
                    (oc_id,),
                )

            return cur.fetchall() or []

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error al listar items: {str(e)}")
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    async def marcar_item_ok(self, item_id: int, request: Request) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        try:
            payload = await request.json()
        except Exception:
            payload = {}

        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(dictionary=True)

            has_qty_ent = OCSHelperFactory.has_column(cur, "oc_items", "cantidad_entregada")

            if has_qty_ent:
                cur.execute(
                    "SELECT id, oc_id, cantidad, cantidad_entregada, entregado FROM oc_items WHERE id=%s",
                    (item_id,),
                )
            else:
                cur.execute(
                    "SELECT id, oc_id, cantidad, entregado FROM oc_items WHERE id=%s",
                    (item_id,),
                )

            item = cur.fetchone()
            if not item:
                raise HTTPException(status_code=404, detail="Item no encontrado.")

            if has_qty_ent and ("cantidad_a_entregar" in payload or "cantidad_entregada" in payload):
                if "cantidad_entregada" in payload:
                    try:
                        new_ent = int(payload.get("cantidad_entregada") or 0)
                    except Exception:
                        new_ent = int(item.get("cantidad_entregada") or 0)
                else:
                    try:
                        delta = int(payload.get("cantidad_a_entregar") or 0)
                    except Exception:
                        delta = 0
                    new_ent = int(item.get("cantidad_entregada") or 0) + delta

                cant = int(item.get("cantidad") or 0)
                if new_ent < 0:
                    new_ent = 0
                if cant > 0 and new_ent > cant:
                    new_ent = cant

                entregado_flag = 1 if (cant > 0 and new_ent >= cant) else 0

                cur.execute(
                    "UPDATE oc_items SET cantidad_entregada=%s, entregado=%s WHERE id=%s",
                    (new_ent, entregado_flag, item_id),
                )
                conn.commit()
                return {
                    "ok": True,
                    "id": item_id,
                    "cantidad_entregada": new_ent,
                    "entregado": bool(entregado_flag),
                }

            entregado = bool(payload.get("entregado", True))

            if has_qty_ent:
                cant = int(item.get("cantidad") or 0)
                new_ent = cant if entregado else 0
                cur.execute(
                    "UPDATE oc_items SET cantidad_entregada=%s, entregado=%s WHERE id=%s",
                    (new_ent, 1 if entregado else 0, item_id),
                )
            else:
                cur.execute("UPDATE oc_items SET entregado=%s WHERE id=%s", (1 if entregado else 0, item_id))

            conn.commit()
            return {"ok": True, "id": item_id, "entregado": entregado}

        except HTTPException:
            raise
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise HTTPException(status_code=500, detail=f"No se pudo actualizar el item: {str(e)}")
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def cerrar_oc(self, oc_id: int) -> Dict[str, Any]:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(dictionary=True)
            
            cur.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(
                    CASE
                      WHEN COALESCE(cantidad_entregada,0) >= COALESCE(cantidad,0)
                           AND COALESCE(cantidad,0) > 0
                      THEN 1 ELSE 0
                    END
                  ) AS completos
                FROM oc_items
                WHERE oc_id=%s
                """,
                (oc_id,),
            )
            row_validacion = cur.fetchone() or {}
            total_items = int(row_validacion.get("total") or 0)
            items_completos = int(row_validacion.get("completos") or 0)

            if total_items <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="La OC no tiene ítems asociados."
                )

            if items_completos < total_items:
                raise HTTPException(
                    status_code=400,
                    detail=f"No se puede cerrar la OC: faltan {total_items - items_completos} ítem(s)."
                )

            cur.execute("UPDATE ocs SET estado='Cerrada' WHERE id=%s", (oc_id,))
            conn.commit()
            return {"ok": True, "id": oc_id, "estado": "Cerrada"}
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise HTTPException(status_code=500, detail=f"No se pudo cerrar la OC: {str(e)}")
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass


# =========================================================
# ENDPOINTS
# =========================================================
@router.get("/health")
def health():
    return {"status": "ok", "router": "ocs"}


@router.get("")
@router.get("/")
def listar_ocs():
    return OCSService().listar_ocs()


@router.get("/resumen-general")
def resumen_general(
    periodo: str = Query("semana", pattern="^(semana|mes)$"),
    alerta_dias: int = Query(5, ge=1, le=30),
    plazo_dias: int = Query(15, ge=1, le=60),
):
    return OCSService().resumen_general(periodo, alerta_dias, plazo_dias)


@router.post("/importar-ocr")
async def importar_ocr(archivo: UploadFile = File(...)):
    return await OCSService().importar_ocr(archivo)


@router.get("/{oc_id}/items")
def listar_items_oc(oc_id: int):
    return OCSService().listar_items_oc(oc_id)


@router.post("/items/{item_id}/ok")
async def marcar_item_ok(item_id: int, request: Request):
    return await OCSService().marcar_item_ok(item_id, request)


@router.post("/{oc_id}/cerrar")
def cerrar_oc(oc_id: int):
    return OCSService().cerrar_oc(oc_id)
