# backend/routes/admin.py
# -*- coding: utf-8 -*-
from __future__ import annotations


import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import mysql.connector
from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------
# Proyecto / paths (Debian-friendly)
# ---------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(dotenv_path=str(env_path), override=False)
except Exception:
    pass

router = APIRouter(prefix="/api/admin", tags=["admin"])

ALLOWED_TABLES: Set[str] = {
    "empresas",
    "usuarios",
    "catalogo",
    "ocs",
    "oc_items",
    "cedibles",
    "pagos",
    "trabajos",
    "notas",
    "reporte_terreno",
    "cotizacion_seg",
    "inventario",
}

FILE_COLS = {"archivo_url"}

BASE_STORAGE = Path(os.getenv("STORAGE_DIR", str(PROJECT_ROOT / "storage"))).resolve()
DEFAULT_UPLOAD_DIRS = [
    "cedibles",
    "cotizaciones",
    "evidencia",
    "imagenes_reparaciones",
    "ocs",
    "ordenes_compra",
    "otros",
    "temporales",
]

_PBKDF2_RE = re.compile(r"^pbkdf2_sha256\$(\d+)\$([0-9a-f]+)\$([0-9a-f]+)$")


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
# CONTRACT REGISTRY
# =========================================================

class AdminContractRegistry(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.allowed_tables = ALLOWED_TABLES
        self.default_upload_dirs = DEFAULT_UPLOAD_DIRS
        self.base_storage = BASE_STORAGE

        self.pagos_front_alias = {
            "RBU": "TRANSPORTES RBU",
            "TRANSPORTES RBU": "TRANSPORTES RBU",
            "METROPOL": "METROPOL CHILE",
            "METROPOL CHILE": "METROPOL CHILE",
            "METBUS": "METBUS",
            "GRAN AMERICA": "GRAN AMERICA",
            "GRAN AMERICA": "GRAN AMERICA",
        }

    def normalize_empresa_alias(self, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        key = self._strip_accents(raw).upper()
        return self.pagos_front_alias.get(key, raw)

    @staticmethod
    def _strip_accents(s: str) -> str:
        tmp = unicodedata.normalize("NFKD", s)
        return "".join(ch for ch in tmp if not unicodedata.combining(ch))


# =========================================================
# DB FACTORY / SINGLETON
# =========================================================

@dataclass(frozen=True)
class DBConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


class DBConfigFactory:
    @staticmethod
    def from_env() -> DBConfig:
        host = os.getenv("DB_HOST", "127.0.0.1")
        port = int(os.getenv("DB_PORT", "3306"))
        user = os.getenv("DB_USER")
        password = os.getenv("DB_PASSWORD")
        database = os.getenv("DB_NAME")

        missing = [k for k, v in {"DB_USER": user, "DB_PASSWORD": password, "DB_NAME": database}.items() if not v]
        if missing:
            raise HTTPException(
                status_code=500,
                detail=f"Error conexion DB: faltan variables en .env / EnvironmentFile -> {', '.join(missing)}",
            )

        return DBConfig(
            host=host,
            port=port,
            user=user or "",
            password=password or "",
            database=database or "",
        )


class DBConnectionManager(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.config = DBConfigFactory.from_env()

    def connect(self):
        try:
            return mysql.connector.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
                connection_timeout=10,
            )
        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error conexion DB: {str(e)}")


def get_db_connection():
    return DBConnectionManager().connect()


# =========================================================
# BASIC HELPERS
# =========================================================

class BasicHelperFactory:
    @staticmethod
    def is_pbkdf2_hash(value: str) -> bool:
        return bool(value and _PBKDF2_RE.match(value.strip()))

    @staticmethod
    def hash_password_pbkdf2(password: str, iterations: int = 210_000) -> str:
        salt = secrets.token_bytes(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"

    @staticmethod
    def sha256_hex(s: str) -> str:
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    @staticmethod
    def table_missing_error(e: mysql.connector.Error) -> bool:
        return getattr(e, "errno", None) == 1146

    @staticmethod
    def column_missing_error(e: mysql.connector.Error) -> bool:
        return getattr(e, "errno", None) == 1054

    @staticmethod
    def safe_table_name(tabla: str) -> str:
        tabla = str(tabla or "").strip()
        if tabla not in AdminContractRegistry().allowed_tables:
            # __BYMETAL_ALIAS_VALIDATE__
            if isinstance(tabla, str) and tabla.strip().lower() == "inventario":
                tabla = "catalogo"
            raise HTTPException(status_code=400, detail=f"Tabla no permitida: {tabla}")
        return tabla

    @staticmethod
    def normalize_str(v: Any) -> str:
        return str(v or "").strip()

    @staticmethod
    def coerce_int_or_none(v: Any) -> Optional[int]:
        if v is None:
            return None
        s = str(v).strip()
        if s == "":
            return None
        try:
            return int(s)
        except Exception:
            return None

    @staticmethod
    def normalize_optional_url(v: Any) -> Optional[str]:
        s = str(v or "").strip()
        if not s:
            return None
        if s.startswith("http://") or s.startswith("https://"):
            return s
        if s.startswith("/storage/"):
            return s
        if s.startswith("storage/"):
            return "/" + s
        if "/" in s and not s.startswith("/"):
            return "/" + s
        return s

    @staticmethod
    def ensure_catalogo_urls(row: Dict[str, Any]) -> Dict[str, Any]:
        for k in ("archivo_url", "imagen_url", "pdf_url"):
            if k in row:
                row[k] = BasicHelperFactory.normalize_optional_url(row.get(k))
        return row

    @staticmethod
    def is_pdf(filename: str) -> bool:
        return (filename or "").lower().endswith(".pdf")

    @staticmethod
    def is_image(filename: str) -> bool:
        low = (filename or "").lower()
        return low.endswith(".jpg") or low.endswith(".jpeg") or low.endswith(".png") or low.endswith(".webp")

    @staticmethod
    def normalize_name(s: str) -> str:
        return " ".join((s or "").strip().upper().split())

    @staticmethod
    def parse_date(value) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            try:
                return datetime.strptime(value[:10], "%Y-%m-%d").date()
            except Exception:
                return None
        return None


# =========================================================
# DB INTROSPECTION FACTORY
# =========================================================

class DBIntrospectionFactory:
    @staticmethod
    def describe_table(cursor, tabla: str) -> List[str]:
        cursor.execute(f"DESCRIBE {tabla}")
        rows = cursor.fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def get_table_columns(conn, tabla: str) -> Set[str]:
        cur = conn.cursor()
        try:
            return set(DBIntrospectionFactory.describe_table(cur, tabla))
        finally:
            try:
                cur.close()
            except Exception:
                pass

    @staticmethod
    def table_exists(conn, table_name: str) -> bool:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = %s
                """,
                (table_name,),
            )
            row = cur.fetchone()
            return bool(row and int(row[0]) > 0)
        finally:
            try:
                cur.close()
            except Exception:
                pass

    @staticmethod
    def require_usuario_columns(cols: Set[str], required: List[str], hint: str) -> None:
        missing = [c for c in required if c not in cols]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Falta migracion en tabla usuarios. Faltan columnas: {', '.join(missing)}. {hint}",
            )

    @staticmethod
    def filter_payload_to_columns(data: Dict[str, Any], columnas: List[str]) -> Dict[str, Any]:
        if not data:
            return {}
        allowed = set(columnas)
        return {k: v for k, v in data.items() if k in allowed and k != "id"}


# =========================================================
# STORAGE FACTORY
# =========================================================

class StorageFactory(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.base = AdminContractRegistry().base_storage

    def ensure_storage_dirs(self) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        for d in AdminContractRegistry().default_upload_dirs:
            (self.base / d).mkdir(parents=True, exist_ok=True)

    def guess_folder_for_upload(self, tabla: str, filename: str) -> str:
        ext = (filename.split(".")[-1] if "." in filename else "").lower()

        if tabla == "cedibles":
            return "cedibles"
        if tabla == "pagos":
            return "otros" if ext == "pdf" else "evidencia"
        if tabla in {"ocs", "oc_items"}:
            return "ocs" if ext == "pdf" else "evidencia"
        if tabla == "trabajos":
            return "evidencia"
        if tabla == "catalogo":
            return "otros"
        if tabla == "reporte_terreno":
            return "evidencia"
        if tabla == "cotizacion_seg":
            return "cotizaciones" if ext == "pdf" else "otros"
        if ext == "pdf":
            return "otros"
        return "evidencia"

    def save_upload_file(self, tabla: str, archivo: UploadFile) -> str:
        self.ensure_storage_dirs()

        folder = self.guess_folder_for_upload(tabla, archivo.filename or "archivo")
        safe_name = (archivo.filename or "archivo").replace(" ", "_")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_name = f"{stamp}_{safe_name}"

        disk_path = (self.base / folder / final_name).resolve()
        disk_path.parent.mkdir(parents=True, exist_ok=True)

        with open(disk_path, "wb") as buffer:
            shutil.copyfileobj(archivo.file, buffer)

        return f"/storage/{folder}/{final_name}"


# =========================================================
# EMPRESA RESOLVER FACTORY
# =========================================================

class EmpresaResolverFactory:
    @staticmethod
    def resolve_empresa_id(
        conn,
        empresa_id: Optional[int] = None,
        empresa_codigo: Optional[str] = None,
        empresa_nombre: Optional[str] = None,
    ) -> Optional[int]:
        if empresa_id is not None:
            try:
                return int(empresa_id)
            except Exception:
                return None

        code = (empresa_codigo or "").strip().upper()
        nombre = (empresa_nombre or "").strip()

        cur = conn.cursor(dictionary=True)
        try:
            if code:
                try:
                    cur.execute("SELECT id FROM empresas WHERE UPPER(codigo)=UPPER(%s) LIMIT 1", (code,))
                    row = cur.fetchone()
                    if row:
                        return int(row["id"])
                except Exception:
                    pass

            if nombre:
                cur.execute(
                    "SELECT id FROM empresas WHERE TRIM(nombre_fantasia)=TRIM(%s) LIMIT 1",
                    (nombre,),
                )
                row = cur.fetchone()
                if row:
                    return int(row["id"])

                cur.execute(
                    "SELECT id FROM empresas WHERE UPPER(nombre_fantasia) LIKE UPPER(%s) ORDER BY id ASC LIMIT 1",
                    (f"%{nombre}%",),
                )
                row = cur.fetchone()
                if row:
                    return int(row["id"])

            return None
        finally:
            try:
                cur.close()
            except Exception:
                pass

    @staticmethod
    def empresas_tiene_dias_vencimiento(conn) -> bool:
        try:
            cols = DBIntrospectionFactory.get_table_columns(conn, "empresas")
            return "dias_vencimiento" in cols
        except Exception:
            return False

    @staticmethod
    def fallback_dias_vencimiento(empresa_nombre: str) -> int:
        n = BasicHelperFactory.normalize_name(empresa_nombre)
        if "RBU" in n:
            return 30
        if "METROPOL" in n or "METBUS" in n:
            return 45
        return 60

    @staticmethod
    def resolver_empresa_por_cliente(conn, cliente: str) -> Optional[Tuple[int, str, Optional[int]]]:
        cli_norm = BasicHelperFactory.normalize_name(cliente)
        if not cli_norm:
            return None

        tiene_dias = EmpresaResolverFactory.empresas_tiene_dias_vencimiento(conn)
        cur = conn.cursor(dictionary=True)
        try:
            if tiene_dias:
                cur.execute(
                    """
                    SELECT id, nombre_fantasia, dias_vencimiento
                    FROM empresas
                    WHERE UPPER(TRIM(nombre_fantasia)) = %s
                    LIMIT 1
                    """,
                    (cli_norm,),
                )
            else:
                cur.execute(
                    """
                    SELECT id, nombre_fantasia, NULL AS dias_vencimiento
                    FROM empresas
                    WHERE UPPER(TRIM(nombre_fantasia)) = %s
                    LIMIT 1
                    """,
                    (cli_norm,),
                )

            row = cur.fetchone()
            if row:
                return int(row["id"]), row["nombre_fantasia"], row.get("dias_vencimiento")

            like = f"%{cli_norm}%"
            if tiene_dias:
                cur.execute(
                    """
                    SELECT id, nombre_fantasia, dias_vencimiento
                    FROM empresas
                    WHERE UPPER(nombre_fantasia) LIKE %s
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (like,),
                )
            else:
                cur.execute(
                    """
                    SELECT id, nombre_fantasia, NULL AS dias_vencimiento
                    FROM empresas
                    WHERE UPPER(nombre_fantasia) LIKE %s
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (like,),
                )

            row = cur.fetchone()
            if row:
                return int(row["id"]), row["nombre_fantasia"], row.get("dias_vencimiento")

            return None
        finally:
            try:
                cur.close()
            except Exception:
                pass


# =========================================================
# PAGOS SERVICE (SINGLETON)
# =========================================================

class AdminPagosService(metaclass=SingletonMeta):
    def sync_pago_desde_cedible(self, cedible_id: int) -> None:
        conn = get_db_connection()
        cur = None
        try:
            cur = conn.cursor(dictionary=True)

            try:
                cur.execute(
                    """
                    SELECT id, cliente, numero_factura, fecha_factura, monto_total, archivo_url
                    FROM cedibles
                    WHERE id = %s
                    LIMIT 1
                    """,
                    (cedible_id,),
                )
            except mysql.connector.Error as e:
                if BasicHelperFactory.column_missing_error(e):
                    return
                raise

            ced = cur.fetchone()
            if not ced:
                return

            numero_factura = (ced.get("numero_factura") or "").strip()
            fecha_emision = BasicHelperFactory.parse_date(ced.get("fecha_factura"))
            cliente = (ced.get("cliente") or "").strip()
            archivo_url = ced.get("archivo_url")

            if not numero_factura or not fecha_emision or not cliente:
                return

            resolved = EmpresaResolverFactory.resolver_empresa_por_cliente(conn, cliente)
            if not resolved:
                return

            empresa_id, empresa_nombre, dias_venc = resolved
            dias = int(dias_venc) if isinstance(dias_venc, int) and dias_venc and dias_venc > 0 else EmpresaResolverFactory.fallback_dias_vencimiento(empresa_nombre)
            fecha_vencimiento = fecha_emision + timedelta(days=int(dias))

            if not fecha_vencimiento:
                fecha_vencimiento = fecha_emision + timedelta(days=30)

            try:
                cur.execute("SELECT * FROM pagos WHERE cedible_id=%s LIMIT 1", (cedible_id,))
                existing = cur.fetchone()
            except mysql.connector.Error as e:
                if BasicHelperFactory.table_missing_error(e):
                    return
                raise

            if not existing:
                cur.execute(
                    "SELECT * FROM pagos WHERE empresa_id=%s AND numero_factura=%s LIMIT 1",
                    (empresa_id, numero_factura),
                )
                existing = cur.fetchone()

            monto_inicial = 0.0
            try:
                mt = ced.get("monto_total")
                if mt is not None and float(mt) > 0:
                    monto_inicial = float(mt)
            except Exception:
                monto_inicial = 0.0

            if existing:
                estado_actual = (existing.get("estado_pago") or "pendiente")
                estado_nuevo = estado_actual if estado_actual == "pagado" else "pendiente"

                monto_actual = existing.get("monto_total")
                monto_final = monto_actual
                try:
                    if monto_actual is None or float(monto_actual) <= 0:
                        monto_final = monto_inicial
                except Exception:
                    monto_final = monto_inicial

                cur.execute(
                    """
                    UPDATE pagos
                    SET empresa_id=%s,
                        numero_factura=%s,
                        empresa=%s,
                        fecha_emision=%s,
                        fecha_vencimiento=%s,
                        estado_pago=%s,
                        cedible_id=%s,
                        archivo_url=%s,
                        monto_total=%s
                    WHERE id=%s
                    """,
                    (
                        empresa_id,
                        numero_factura,
                        empresa_nombre,
                        fecha_emision,
                        fecha_vencimiento,
                        estado_nuevo,
                        cedible_id,
                        archivo_url,
                        monto_final,
                        existing["id"],
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO pagos (
                        numero_factura,
                        empresa,
                        monto_total,
                        empresa_id,
                        fecha_emision,
                        fecha_vencimiento,
                        estado_pago,
                        pagado_at,
                        archivado,
                        cedible_id,
                        archivo_url
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,0,%s,%s)
                    """,
                    (
                        numero_factura,
                        empresa_nombre,
                        monto_inicial if monto_inicial else 0,
                        empresa_id,
                        fecha_emision,
                        fecha_vencimiento,
                        "pendiente",
                        cedible_id,
                        archivo_url,
                    ),
                )

            conn.commit()
        finally:
            try:
                if cur:
                    cur.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def listar_pagos_pendientes(
        self,
        empresa_id: Optional[int] = None,
        empresa: Optional[str] = None,
        limit: int = 5000,
    ) -> Dict[str, Any]:
        conn = get_db_connection()
        cur = None
        try:
            cur = conn.cursor(dictionary=True)

            where = ["p.archivado=0", "p.estado_pago <> 'pagado'"]
            params: List[Any] = []

            empresa_id_filtro = BasicHelperFactory.coerce_int_or_none(empresa_id)
            if empresa_id_filtro is not None:
                where.append("p.empresa_id=%s")
                params.append(int(empresa_id_filtro))

            emp_raw = (empresa or "").strip()
            emp = AdminContractRegistry().normalize_empresa_alias(emp_raw)
            if emp:
                where.append(
                    """
                    (
                        UPPER(TRIM(p.empresa)) = UPPER(TRIM(%s))
                        OR UPPER(TRIM(e.nombre_fantasia)) = UPPER(TRIM(%s))
                        OR UPPER(TRIM(p.empresa)) LIKE UPPER(TRIM(%s))
                        OR UPPER(TRIM(e.nombre_fantasia)) LIKE UPPER(TRIM(%s))
                    )
                    """
                )
                params.extend([emp, emp, f"%{emp}%", f"%{emp}%"])

            sql = f"""
                SELECT
                  p.id,
                  p.numero_factura,
                  p.empresa,
                  p.empresa_id,
                  e.nombre_fantasia AS empresa_nombre,
                  p.monto_total,
                  p.fecha_emision,
                  p.fecha_vencimiento,
                  p.estado_pago,
                  p.pagado_at,
                  p.archivado,
                  p.cedible_id,
                  p.archivo_url,
                  p.created_at
                FROM pagos p
                LEFT JOIN empresas e ON e.id = p.empresa_id
                WHERE {' AND '.join(where)}
                ORDER BY
                  CASE WHEN p.fecha_vencimiento IS NULL THEN 1 ELSE 0 END,
                  p.fecha_vencimiento ASC,
                  p.id DESC
                LIMIT %s
            """
            params.append(int(limit))
            cur.execute(sql, params)
            rows = cur.fetchall() or []
            return {"datos": rows, "items": rows}
        except mysql.connector.Error as e:
            if BasicHelperFactory.table_missing_error(e):
                return {"datos": [], "items": []}
            raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
        finally:
            try:
                if cur:
                    cur.close()
            except Exception:
                pass
            conn.close()

    

    def listar_pagos_pagados(self, limit: int = 5000):
        conn = get_db_connection()
        cur = None
        try:
            cur = conn.cursor(dictionary=True)

            cur.execute(
                '''
                SELECT
                    p.id,
                    p.numero_factura,
                    p.empresa,
                    p.empresa_id,
                    e.nombre_fantasia AS empresa_nombre,
                    p.monto_total,
                    p.fecha_emision,
                    p.fecha_vencimiento,
                    p.estado_pago,
                    p.pagado_at,
                    p.archivado,
                    p.cedible_id,
                    p.archivo_url,
                    p.created_at
                FROM pagos p
                LEFT JOIN empresas e ON e.id = p.empresa_id
                WHERE p.estado_pago='pagado' AND p.archivado=0
                ORDER BY p.pagado_at DESC
                LIMIT %s
                ''',
                (limit,)
            )

            rows = cur.fetchall() or []
            return {"datos": rows, "items": rows}

        finally:
            try:
                if cur: cur.close()
            except:
                pass
            conn.close()


    def listar_pagos_archivados(self, limit: int = 5000):
        conn = get_db_connection()
        cur = None
        try:
            cur = conn.cursor(dictionary=True)

            cur.execute(
                '''
                SELECT
                    p.id,
                    p.numero_factura,
                    p.empresa,
                    p.empresa_id,
                    e.nombre_fantasia AS empresa_nombre,
                    p.monto_total,
                    p.fecha_emision,
                    p.fecha_vencimiento,
                    p.estado_pago,
                    p.pagado_at,
                    p.archivado,
                    p.cedible_id,
                    p.archivo_url,
                    p.created_at
                FROM pagos p
                LEFT JOIN empresas e ON e.id = p.empresa_id
                WHERE p.archivado=1
                ORDER BY p.pagado_at DESC
                LIMIT %s
                ''',
                (limit,)
            )

            rows = cur.fetchall() or []
            return {"datos": rows, "items": rows}

        finally:
            try:
                if cur: cur.close()
            except:
                pass
            conn.close()


    def sync_pagos_desde_cedibles(
        self,
        solo_pendientes: bool = True,
        force_update: bool = False,
        limit: int = 5000,
    ) -> Dict[str, Any]:
        conn = get_db_connection()
        cur = None
        creados = 0
        actualizados = 0
        omitidos = 0
        errores: List[Dict[str, Any]] = []

        try:
            if not DBIntrospectionFactory.table_exists(conn, "cedibles") or not DBIntrospectionFactory.table_exists(conn, "pagos"):
                return {
                    "ok": True,
                    "creados": 0,
                    "actualizados": 0,
                    "omitidos": 0,
                    "errores": [],
                    "mensaje": "Tablas cedibles/pagos no existen",
                }

            cur = conn.cursor(dictionary=True)

            cols_ced = DBIntrospectionFactory.get_table_columns(conn, "cedibles")
            has_archivado = "archivado" in cols_ced

            where = []
            params: List[Any] = []

            if solo_pendientes and has_archivado:
                where.append("c.archivado=0")

            sql = f"""
                SELECT
                  c.id,
                  c.empresa_id,
                  c.cliente,
                  c.numero_factura,
                  c.fecha_factura,
                  c.monto_total,
                  c.archivo_url
                FROM cedibles c
                {"WHERE " + " AND ".join(where) if where else ""}
                ORDER BY c.id ASC
                LIMIT %s
            """
            params.append(int(limit))
            cur.execute(sql, params)
            cedibles = cur.fetchall() or []

            empresas_cols = DBIntrospectionFactory.get_table_columns(conn, "empresas") if DBIntrospectionFactory.table_exists(conn, "empresas") else set()
            has_dias = "dias_vencimiento" in empresas_cols

            emp_cache: Dict[int, Dict[str, Any]] = {}
            if DBIntrospectionFactory.table_exists(conn, "empresas"):
                cur_emp = conn.cursor(dictionary=True)
                try:
                    if has_dias:
                        cur_emp.execute("SELECT id, nombre_fantasia, dias_vencimiento FROM empresas")
                    else:
                        cur_emp.execute("SELECT id, nombre_fantasia, NULL AS dias_vencimiento FROM empresas")
                    for r in (cur_emp.fetchall() or []):
                        try:
                            emp_cache[int(r["id"])] = r
                        except Exception:
                            continue
                finally:
                    try:
                        cur_emp.close()
                    except Exception:
                        pass

            curw = conn.cursor(dictionary=True)
            try:
                for c in cedibles:
                    try:
                        cid = int(c["id"])
                        numero_factura = (c.get("numero_factura") or "").strip()
                        fecha_emision = BasicHelperFactory.parse_date(c.get("fecha_factura"))
                        monto_total = c.get("monto_total")
                        archivo_url = c.get("archivo_url")
                        cliente = (c.get("cliente") or "").strip()

                        if not numero_factura or not fecha_emision:
                            omitidos += 1
                            continue

                        empresa_id = BasicHelperFactory.coerce_int_or_none(c.get("empresa_id"))
                        empresa_nombre = None
                        dias_venc = None

                        if empresa_id and empresa_id in emp_cache:
                            empresa_nombre = (emp_cache[empresa_id].get("nombre_fantasia") or "").strip()
                            dias_venc = emp_cache[empresa_id].get("dias_vencimiento")
                        else:
                            resolved = EmpresaResolverFactory.resolver_empresa_por_cliente(conn, cliente)
                            if resolved:
                                empresa_id, empresa_nombre, dias_venc = resolved

                        if not empresa_id or not empresa_nombre:
                            omitidos += 1
                            continue

                        dias = None
                        try:
                            if dias_venc is not None and int(dias_venc) > 0:
                                dias = int(dias_venc)
                        except Exception:
                            dias = None
                        if dias is None:
                            dias = EmpresaResolverFactory.fallback_dias_vencimiento(empresa_nombre)

                        fecha_vencimiento = fecha_emision + timedelta(days=int(dias))
                        if not fecha_vencimiento:
                            fecha_vencimiento = fecha_emision + timedelta(days=30)

                        monto_final = 0.0
                        try:
                            if monto_total is not None and float(monto_total) > 0:
                                monto_final = float(monto_total)
                        except Exception:
                            monto_final = 0.0

                        estado_nuevo = "pendiente"

                        sql_upsert = """
                        INSERT INTO pagos (
                          empresa_id, numero_factura, empresa,
                          fecha_emision, fecha_vencimiento,
                          estado_pago, cedible_id,
                          archivo_url, monto_total,
                          pagado_at, archivado
                        ) VALUES (
                          %s,%s,%s,
                          %s,%s,
                          %s,%s,
                          %s,%s,
                          NULL,0
                        )
                        ON DUPLICATE KEY UPDATE
                          estado_pago = CASE
                            WHEN pagos.estado_pago = 'pagado' THEN 'pagado'
                            ELSE VALUES(estado_pago)
                          END,
                          empresa = IF(COALESCE(pagos.empresa,'') <> COALESCE(VALUES(empresa),''), VALUES(empresa), pagos.empresa),
                          fecha_emision = IF(COALESCE(pagos.fecha_emision,'1000-01-01') <> COALESCE(VALUES(fecha_emision),'1000-01-01'), VALUES(fecha_emision), pagos.fecha_emision),
                          fecha_vencimiento = IF(COALESCE(pagos.fecha_vencimiento,'1000-01-01') <> COALESCE(VALUES(fecha_vencimiento),'1000-01-01'), VALUES(fecha_vencimiento), pagos.fecha_vencimiento),
                          cedible_id = IF(COALESCE(pagos.cedible_id,0) <> COALESCE(VALUES(cedible_id),0), VALUES(cedible_id), pagos.cedible_id),
                          archivo_url = IF(COALESCE(pagos.archivo_url,'') <> COALESCE(VALUES(archivo_url),''), VALUES(archivo_url), pagos.archivo_url),
                          monto_total = CASE
                            WHEN pagos.monto_total IS NULL OR pagos.monto_total <= 0 THEN VALUES(monto_total)
                            ELSE pagos.monto_total
                          END
                        """

                        curw.execute(
                            sql_upsert,
                            (
                                int(empresa_id),
                                numero_factura,
                                empresa_nombre,
                                fecha_emision,
                                fecha_vencimiento,
                                estado_nuevo,
                                cid,
                                archivo_url,
                                monto_final,
                            ),
                        )

                        rc = int(getattr(curw, "rowcount", 0) or 0)
                        if rc == 1:
                            creados += 1
                        elif rc == 2:
                            actualizados += 1
                        else:
                            omitidos += 1

                    except mysql.connector.Error as e:
                        errores.append({"cedible_id": c.get("id"), "error": f"DB: {str(e)}"})
                    except Exception as e:
                        errores.append({"cedible_id": c.get("id"), "error": str(e)})

                conn.commit()
            finally:
                try:
                    curw.close()
                except Exception:
                    pass

            return {
                "ok": True,
                "creados": creados,
                "actualizados": actualizados,
                "omitidos": omitidos,
                "errores": errores[:100],
            }
        except mysql.connector.Error as e:
            try:
                conn.rollback()
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
        finally:
            try:
                if cur:
                    cur.close()
            except Exception:
                pass
            conn.close()


# =========================================================
# REQUEST MODELS
# =========================================================

class SyncDesdeCediblesRequest(BaseModel):
    solo_pendientes: bool = True
    force_update: bool = False
    limit: int = Field(default=5000, ge=1, le=50000)


class PagoMarcarPagadoRequest(BaseModel):
    archivado: int = Field(default=1, ge=0, le=1)


class PasswordChangeRequest(BaseModel):
    password: str


class ToggleActivoRequest(BaseModel):
    activo: int


class ForceResetRequest(BaseModel):
    ttl_minutes: int = 30


# =========================================================
# TABLAS DINAMICAS (LEGACY)
# =========================================================

@router.get("/datos/{tabla}")
def obtener_datos_tabla(
    tabla: str,
    empresa_id: Optional[int] = Query(default=None),
    filtro: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=2000),
):
    tabla_raw = str(tabla or "").strip()
    empresa_codigo_from_path = None

    tabla_raw_low = tabla_raw.lower()

    # Alias lógicos de frontend -> tabla física real
    # inventario            -> catalogo
    # inventario_RBU        -> catalogo + empresa_codigo_from_path=RBU
    # catalogo_RBU          -> catalogo + empresa_codigo_from_path=RBU
    if tabla_raw_low == "inventario":
        tabla_raw = "catalogo"
    elif tabla_raw_low.startswith("inventario_"):
        empresa_codigo_from_path = (tabla_raw.split("_", 1)[1] or "").strip().upper() or None
        tabla_raw = "catalogo"
    elif tabla_raw_low.startswith("catalogo_"):
        empresa_codigo_from_path = (tabla_raw.split("_", 1)[1] or "").strip().upper() or None
        tabla_raw = "catalogo"

    tabla = BasicHelperFactory.safe_table_name(tabla_raw)

    conn = get_db_connection()
    cur = None
    curd = None
    try:
        cur = conn.cursor()
        columnas = DBIntrospectionFactory.describe_table(cur, tabla)

        where = []
        params: List[Any] = []

        empresa_id_eff = None

        if empresa_id is not None and "empresa_id" in columnas:
            try:
                empresa_id_eff = int(empresa_id)
            except Exception:
                empresa_id_eff = None

        if empresa_id_eff is None and tabla == "catalogo" and empresa_codigo_from_path and "empresa_id" in columnas:
            try:
                cur.execute(
                    "SELECT id FROM empresas WHERE UPPER(codigo)=UPPER(%s) LIMIT 1",
                    (empresa_codigo_from_path,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    empresa_id_eff = int(row[0])
            except Exception:
                empresa_id_eff = None

        if empresa_id_eff is not None and "empresa_id" in columnas:
            where.append("empresa_id = %s")
            params.append(int(empresa_id_eff))

        if filtro and filtro != "Todas":
            filtro_raw = (filtro or "").strip()

            f = unicodedata.normalize("NFKD", filtro_raw)
            f = "".join(ch for ch in f if not unicodedata.combining(ch))
            f = f.upper().replace(" ", "_")

            alias = {
                "GA": "GRAN_AMERICA",
                "GRANAMERICA": "GRAN_AMERICA",
                "GRAN_AMERICA": "GRAN_AMERICA",
            }
            f = alias.get(f, f)

            filtro_conds = []
            filtro_vals: List[Any] = []

            if tabla == "catalogo":
                if "empresa_id" in columnas:
                    try:
                        cur.execute("SELECT id FROM empresas WHERE UPPER(codigo)=UPPER(%s) LIMIT 1", (f,))
                        r = cur.fetchone()
                        if r and r[0]:
                            filtro_conds.append("empresa_id = %s")
                            filtro_vals.append(int(r[0]))
                    except Exception:
                        pass

                if "cliente" in columnas:
                    filtro_conds.append("UPPER(cliente) = UPPER(%s)")
                    filtro_vals.append(filtro_raw)
                    filtro_conds.append("UPPER(cliente) = UPPER(%s)")
                    filtro_vals.append(f)

                if "empresa" in columnas:
                    filtro_conds.append("UPPER(empresa) = UPPER(%s)")
                    filtro_vals.append(filtro_raw)
                    filtro_conds.append("UPPER(empresa) = UPPER(%s)")
                    filtro_vals.append(f)
            else:
                if "cliente" in columnas:
                    filtro_conds.append("cliente = %s")
                    filtro_vals.append(filtro_raw)
                elif "empresa" in columnas:
                    filtro_conds.append("empresa = %s")
                    filtro_vals.append(filtro_raw)

            if filtro_conds:
                where.append("(" + " OR ".join(filtro_conds) + ")")
                params.extend(filtro_vals)

        sql = f"SELECT * FROM {tabla}"
        if where:
            sql += " WHERE " + " AND ".join(where)

        if "id" in columnas:
            sql += " ORDER BY id DESC"

        sql += " LIMIT %s"
        params.append(int(limit))

        curd = conn.cursor(dictionary=True)
        curd.execute(sql, params)
        datos = curd.fetchall() or []

        if tabla == "catalogo":
            datos = [BasicHelperFactory.ensure_catalogo_urls(dict(r)) for r in datos]

        return {"columnas": columnas, "datos": datos}

    except mysql.connector.Error as e:
        raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
    finally:
        try:
            if curd:
                curd.close()
        except Exception:
            pass
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        conn.close()


@router.post("/insertar-con-archivo")
async def insertar_con_archivo(
    tabla: str = Form(...),
    datos_json: str = Form(...),
    archivo: UploadFile = File(None),
):
    tabla = BasicHelperFactory.safe_table_name(tabla)

    try:
        data: Dict[str, Any] = json.loads(datos_json) if datos_json else {}
        if not isinstance(data, dict):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="datos_json invalido")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        columnas = DBIntrospectionFactory.describe_table(cur, tabla)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()

    if tabla == "catalogo" and "categoria" in columnas:
        cat = BasicHelperFactory.normalize_str(data.get("categoria"))
        if not cat:
            data["categoria"] = "General"

    if archivo is not None:
        try:
            data["archivo_url"] = StorageFactory().save_upload_file(tabla, archivo)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error guardando archivo: {str(e)}")

    data = DBIntrospectionFactory.filter_payload_to_columns(data, columnas)
    if not data:
        raise HTTPException(status_code=400, detail="No hay datos para insertar")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cols = ", ".join(data.keys())
        placeholders = ", ".join(["%s"] * len(data))
        sql = f"INSERT INTO {tabla} ({cols}) VALUES ({placeholders})"
        cur.execute(sql, list(data.values()))
        conn.commit()
        new_id = cur.lastrowid

        if tabla == "cedibles" and new_id:
            AdminPagosService().sync_pago_desde_cedible(int(new_id))

        return {"mensaje": "Registro guardado correctamente", "id": new_id}
    except mysql.connector.Error as e:
        raise HTTPException(status_code=500, detail=f"Error DB insertar: {str(e)}")
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()


@router.post("/actualizar")
async def actualizar_registro(
    tabla: str = Form(...),
    id_registro: int = Form(...),
    datos_json: str = Form(...),
    archivo: UploadFile = File(None),
):
    tabla = BasicHelperFactory.safe_table_name(tabla)

    try:
        data: Dict[str, Any] = json.loads(datos_json) if datos_json else {}
        if not isinstance(data, dict):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="datos_json invalido")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        columnas = DBIntrospectionFactory.describe_table(cur, tabla)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()

    if tabla == "catalogo" and "categoria" in columnas and "categoria" in data:
        if not BasicHelperFactory.normalize_str(data.get("categoria")):
            data.pop("categoria", None)

    if archivo is not None:
        try:
            data["archivo_url"] = StorageFactory().save_upload_file(tabla, archivo)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error guardando archivo: {str(e)}")

    data = DBIntrospectionFactory.filter_payload_to_columns(data, columnas)
    if not data:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        set_clause = ", ".join([f"{k} = %s" for k in data.keys()])
        valores = list(data.values()) + [id_registro]
        sql = f"UPDATE {tabla} SET {set_clause} WHERE id = %s"
        cur.execute(sql, valores)
        conn.commit()

        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Registro no encontrado")

        if tabla == "cedibles":
            AdminPagosService().sync_pago_desde_cedible(int(id_registro))

        return {"mensaje": "Actualizado"}
    except mysql.connector.Error as e:
        raise HTTPException(status_code=500, detail=f"Error DB actualizar: {str(e)}")
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()


# =========================================================
# CATALOGO
# =========================================================

@router.get("/catalogo/meta")
def admin_catalogo_meta():
    conn = get_db_connection()
    cur = None
    try:
        cur = conn.cursor(dictionary=True)

        try:
            cur.execute("SELECT id, codigo, nombre_fantasia FROM empresas ORDER BY id ASC")
            empresas = cur.fetchall() or []
        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error DB empresas: {str(e)}")

        try:
            cur.execute(
                """
                SELECT empresa_id, categoria
                FROM catalogo
                WHERE categoria IS NOT NULL AND TRIM(categoria) <> ''
                GROUP BY empresa_id, categoria
                ORDER BY categoria ASC
                """
            )
            rows = cur.fetchall() or []
        except mysql.connector.Error as e:
            if BasicHelperFactory.table_missing_error(e):
                return {"empresas": empresas, "categorias_por_empresa": {}}
            raise

        cat_map: Dict[int, List[str]] = {}
        for r in rows:
            eid = int(r.get("empresa_id") or 0)
            cat = (r.get("categoria") or "").strip()
            if not eid or not cat:
                continue
            cat_map.setdefault(eid, []).append(cat)

        return {"empresas": empresas, "categorias_por_empresa": cat_map}
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        conn.close()


@router.get("/catalogo/categorias")
def admin_catalogo_categorias(
    empresa_id: Optional[int] = Query(default=None),
    empresa_codigo: Optional[str] = Query(default=None),
    empresa_nombre: Optional[str] = Query(default=None),
):
    conn = get_db_connection()
    cur = None
    eid: Optional[int] = None
    try:
        eid = EmpresaResolverFactory.resolve_empresa_id(
            conn,
            empresa_id=empresa_id,
            empresa_codigo=empresa_codigo,
            empresa_nombre=empresa_nombre,
        )
        if not eid:
            return {"empresa_id": None, "categorias": []}

        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT categoria AS nombre
            FROM catalogo
            WHERE empresa_id=%s
              AND categoria IS NOT NULL AND TRIM(categoria) <> ''
            GROUP BY categoria
            ORDER BY categoria ASC
            """,
            (eid,),
        )
        rows = cur.fetchall() or []
        categorias = [str(r.get("nombre") or "").strip() for r in rows if (r.get("nombre") or "").strip()]
        return {"empresa_id": int(eid), "categorias": categorias}
    except mysql.connector.Error as e:
        if BasicHelperFactory.table_missing_error(e):
            return {"empresa_id": int(eid) if eid else None, "categorias": []}
        raise HTTPException(status_code=500, detail=f"Error DB categorias catalogo: {str(e)}")
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        conn.close()


@router.get("/catalogo/listar")
def admin_catalogo_listar(
    empresa_id: Optional[int] = Query(default=None),
    empresa_codigo: Optional[str] = Query(default=None),
    empresa_nombre: Optional[str] = Query(default=None),
    categoria: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    limit: int = Query(default=5000, ge=1, le=5000),
):
    conn = get_db_connection()
    cur = None
    eid: Optional[int] = None
    try:
        eid = EmpresaResolverFactory.resolve_empresa_id(
            conn,
            empresa_id=empresa_id,
            empresa_codigo=empresa_codigo,
            empresa_nombre=empresa_nombre,
        )
        if not eid:
            return {"empresa_id": None, "items": []}

        where = ["empresa_id=%s"]
        params: List[Any] = [int(eid)]

        if categoria is not None and str(categoria).strip() != "":
            where.append("categoria=%s")
            params.append(str(categoria).strip())

        if q is not None and str(q).strip() != "":
            where.append("UPPER(descripcion) LIKE UPPER(%s)")
            params.append(f"%{str(q).strip()}%")

        sql = f"""
            SELECT
              id, empresa_id, categoria, descripcion, descripcion_doc,
              precio, cliente, archivo_url, imagen_url, pdf_url
            FROM catalogo
            WHERE {' AND '.join(where)}
            ORDER BY categoria ASC, descripcion ASC, id ASC
            LIMIT %s
        """
        params.append(int(limit))

        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params)
        rows = cur.fetchall() or []
        rows = [BasicHelperFactory.ensure_catalogo_urls(dict(r)) for r in rows]
        return {"empresa_id": int(eid), "items": rows}
    except mysql.connector.Error as e:
        if BasicHelperFactory.table_missing_error(e):
            return {"empresa_id": int(eid) if eid else None, "items": []}
        raise HTTPException(status_code=500, detail=f"Error DB listar catalogo: {str(e)}")
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        conn.close()


@router.get("/catalogo/items")
def admin_catalogo_items(
    empresa_id: Optional[int] = Query(default=None),
    empresa_codigo: Optional[str] = Query(default=None),
    empresa_nombre: Optional[str] = Query(default=None),
    categoria: str = Query(..., min_length=1),
    limit: int = Query(default=2000, ge=1, le=5000),
):
    conn = get_db_connection()
    cur = None
    eid: Optional[int] = None
    try:
        eid = EmpresaResolverFactory.resolve_empresa_id(
            conn,
            empresa_id=empresa_id,
            empresa_codigo=empresa_codigo,
            empresa_nombre=empresa_nombre,
        )
        if not eid:
            return {"empresa_id": None, "categoria": categoria, "items": []}

        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT
              id, empresa_id, categoria, descripcion, descripcion_doc,
              precio, cliente, archivo_url, imagen_url, pdf_url
            FROM catalogo
            WHERE empresa_id=%s AND categoria=%s
            ORDER BY descripcion ASC, id ASC
            LIMIT %s
            """,
            (int(eid), categoria.strip(), int(limit)),
        )
        rows = cur.fetchall() or []
        rows = [BasicHelperFactory.ensure_catalogo_urls(dict(r)) for r in rows]
        return {"empresa_id": int(eid), "categoria": categoria.strip(), "items": rows}
    except mysql.connector.Error as e:
        if BasicHelperFactory.table_missing_error(e):
            return {"empresa_id": int(eid) if eid else None, "categoria": categoria.strip(), "items": []}
        raise HTTPException(status_code=500, detail=f"Error DB items catalogo: {str(e)}")
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        conn.close()


@router.post("/catalogo/insertar")
async def insertar_catalogo(
    datos_json: str = Form(...),
    archivo_img: UploadFile = File(None),
    archivo_pdf: UploadFile = File(None),
):
    tabla = "catalogo"

    try:
        data: Dict[str, Any] = json.loads(datos_json) if datos_json else {}
        if not isinstance(data, dict):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="datos_json invalido")

    hay_adjunto = (archivo_img is not None) or (archivo_pdf is not None)
    if hay_adjunto and not BasicHelperFactory.normalize_str(data.get("descripcion_doc")):
        raise HTTPException(status_code=400, detail="descripcion_doc es obligatoria cuando adjuntas imagen y/o PDF")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        columnas = DBIntrospectionFactory.describe_table(cur, tabla)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()

    if archivo_img is not None:
        if not BasicHelperFactory.is_image(archivo_img.filename or ""):
            raise HTTPException(status_code=400, detail="archivo_img debe ser imagen (jpg/jpeg/png/webp)")
        url = StorageFactory().save_upload_file(tabla, archivo_img)
        data["imagen_url"] = url
        data.setdefault("archivo_url", url)

    if archivo_pdf is not None:
        if not BasicHelperFactory.is_pdf(archivo_pdf.filename or ""):
            raise HTTPException(status_code=400, detail="archivo_pdf debe ser PDF")
        url = StorageFactory().save_upload_file(tabla, archivo_pdf)
        data["pdf_url"] = url
        data.setdefault("archivo_url", url)

    if "empresa_id" in columnas:
        eid = BasicHelperFactory.coerce_int_or_none(data.get("empresa_id"))
        if eid is None:
            empresa_codigo = data.get("empresa_codigo") or data.get("codigo") or data.get("empresa_codigo_sel") or data.get("empresa")
            empresa_nombre = data.get("empresa_nombre") or data.get("nombre_fantasia") or data.get("empresa_fantasia")

            conn2 = get_db_connection()
            try:
                eid = EmpresaResolverFactory.resolve_empresa_id(
                    conn2,
                    empresa_id=None,
                    empresa_codigo=empresa_codigo,
                    empresa_nombre=empresa_nombre,
                )
            finally:
                conn2.close()

        if eid is None:
            raise HTTPException(status_code=400, detail="empresa_id es obligatorio (o envia empresa_codigo/empresa_nombre para resolverlo).")

        data["empresa_id"] = int(eid)

    if "categoria" in data:
        data["categoria"] = BasicHelperFactory.normalize_str(data.get("categoria"))
    if "descripcion" in data:
        data["descripcion"] = BasicHelperFactory.normalize_str(data.get("descripcion"))
    if "descripcion_doc" in data:
        data["descripcion_doc"] = BasicHelperFactory.normalize_str(data.get("descripcion_doc"))

    if "categoria" in columnas:
        if not BasicHelperFactory.normalize_str(data.get("categoria")):
            data["categoria"] = "General"

    data = DBIntrospectionFactory.filter_payload_to_columns(data, columnas)

    if "empresa_id" in columnas:
        if "empresa_id" not in data or BasicHelperFactory.coerce_int_or_none(data.get("empresa_id")) is None:
            raise HTTPException(status_code=400, detail="empresa_id invalido u omitido.")

    if not data:
        raise HTTPException(status_code=400, detail="No hay datos para insertar")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cols = ", ".join(data.keys())
        placeholders = ", ".join(["%s"] * len(data))
        sql = f"INSERT INTO {tabla} ({cols}) VALUES ({placeholders})"
        cur.execute(sql, list(data.values()))
        conn.commit()
        return {"mensaje": "Registro guardado correctamente", "id": cur.lastrowid}
    except mysql.connector.Error as e:
        raise HTTPException(status_code=500, detail=f"Error DB insertar catalogo: {str(e)}")
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()


@router.post("/catalogo/actualizar")
async def actualizar_catalogo(
    id_registro: int = Form(...),
    datos_json: str = Form(...),
    archivo_img: UploadFile = File(None),
    archivo_pdf: UploadFile = File(None),
):
    tabla = "catalogo"

    try:
        data: Dict[str, Any] = json.loads(datos_json) if datos_json else {}
        if not isinstance(data, dict):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="datos_json invalido")

    hay_adjunto = (archivo_img is not None) or (archivo_pdf is not None)
    if hay_adjunto and not BasicHelperFactory.normalize_str(data.get("descripcion_doc")):
        raise HTTPException(status_code=400, detail="descripcion_doc es obligatoria cuando adjuntas imagen y/o PDF")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        columnas = DBIntrospectionFactory.describe_table(cur, tabla)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()

    if archivo_img is not None:
        if not BasicHelperFactory.is_image(archivo_img.filename or ""):
            raise HTTPException(status_code=400, detail="archivo_img debe ser imagen (jpg/jpeg/png/webp)")
        url = StorageFactory().save_upload_file(tabla, archivo_img)
        data["imagen_url"] = url
        data.setdefault("archivo_url", url)

    if archivo_pdf is not None:
        if not BasicHelperFactory.is_pdf(archivo_pdf.filename or ""):
            raise HTTPException(status_code=400, detail="archivo_pdf debe ser PDF")
        url = StorageFactory().save_upload_file(tabla, archivo_pdf)
        data["pdf_url"] = url
        data.setdefault("archivo_url", url)

    if "empresa_id" in columnas and "empresa_id" in data:
        raw = data.get("empresa_id")
        if raw is None or str(raw).strip() == "":
            data.pop("empresa_id", None)
        else:
            try:
                data["empresa_id"] = int(raw)
            except Exception:
                raise HTTPException(status_code=400, detail="empresa_id invalido (debe ser entero)")

    if "categoria" in data:
        data["categoria"] = BasicHelperFactory.normalize_str(data.get("categoria"))
        if not data["categoria"]:
            data.pop("categoria", None)

    if "descripcion" in data:
        data["descripcion"] = BasicHelperFactory.normalize_str(data.get("descripcion"))
    if "descripcion_doc" in data:
        data["descripcion_doc"] = BasicHelperFactory.normalize_str(data.get("descripcion_doc"))

    data = DBIntrospectionFactory.filter_payload_to_columns(data, columnas)
    if not data:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        set_clause = ", ".join([f"{k} = %s" for k in data.keys()])
        valores = list(data.values()) + [int(id_registro)]
        sql = f"UPDATE {tabla} SET {set_clause} WHERE id = %s"
        cur.execute(sql, valores)
        conn.commit()

        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Registro no encontrado")

        return {"mensaje": "Actualizado"}
    except mysql.connector.Error as e:
        raise HTTPException(status_code=500, detail=f"Error DB actualizar catalogo: {str(e)}")
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()


# =========================================================
# BORRADO
# =========================================================

@router.delete("/borrar/{tabla}/{id_registro}")
def borrar_registro(tabla: str, id_registro: int, empresa_id: Optional[int] = Query(default=None)):
    tabla_raw = str(tabla or "").strip()
    empresa_codigo_from_path = None

    if tabla_raw.lower().startswith("catalogo_"):
        empresa_codigo_from_path = (tabla_raw.split("_", 1)[1] or "").strip().upper() or None
        tabla_raw = "catalogo"

    tabla = BasicHelperFactory.safe_table_name(tabla_raw)

    conn = get_db_connection()
    cur = None
    try:
        cur = conn.cursor()

        if tabla == "catalogo" and empresa_id is None and empresa_codigo_from_path:
            try:
                cur.execute("SELECT id FROM empresas WHERE UPPER(codigo)=UPPER(%s) LIMIT 1", (empresa_codigo_from_path,))
                r = cur.fetchone()
                if r and r[0]:
                    empresa_id = int(r[0])
            except Exception:
                empresa_id = None

        if tabla == "catalogo":
            if empresa_id is None:
                raise HTTPException(status_code=400, detail="Falta empresa_id para borrar en catalogo (scope obligatorio).")
            cur.execute(f"DELETE FROM {tabla} WHERE id = %s AND empresa_id = %s", (int(id_registro), int(empresa_id)))

        elif tabla == "cedibles":
            # Romper referencia desde pagos antes de borrar el cedible
            try:
                pagos_cols = DBIntrospectionFactory.get_table_columns(conn, "pagos")
            except Exception:
                pagos_cols = set()

            if "cedible_id" in pagos_cols:
                cur.execute(
                    "UPDATE pagos SET cedible_id = NULL WHERE cedible_id = %s",
                    (int(id_registro),)
                )

            cur.execute("DELETE FROM cedibles WHERE id = %s", (int(id_registro),))

        else:
            cur.execute(f"DELETE FROM {tabla} WHERE id = %s", (int(id_registro),))

        conn.commit()

        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Registro no encontrado (id no existe o no pertenece a la empresa)")

        return {"mensaje": "Eliminado"}

    except HTTPException:
        raise
    except mysql.connector.Error as e:
        raise HTTPException(status_code=500, detail=f"Error DB borrar: {str(e)}")
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        conn.close()


# =========================================================
# PAGOS ROUTES
# =========================================================

@router.post("/pagos/sync-desde-cedibles")

@router.get("/pagos/pagados")
def listar_pagos_pagados(limit: int = Query(default=5000, ge=1, le=5000)):
    return AdminPagosService().listar_pagos_pagados(limit)


@router.get("/pagos/archivados")
def listar_pagos_archivados(limit: int = Query(default=5000, ge=1, le=5000)):
    return AdminPagosService().listar_pagos_archivados(limit)


@router.patch("/pagos/{pago_id}/marcar-pagado")
def marcar_pago_como_pagado(
    pago_id: int,
    payload: PagoMarcarPagadoRequest = Body(default=PagoMarcarPagadoRequest()),
):
    conn = get_db_connection()
    cur = None
    try:
        cur = conn.cursor()
        archivado = 1 if int(payload.archivado) == 1 else 0

        cur.execute(
            """
            UPDATE pagos
            SET estado_pago='pagado',
                pagado_at=NOW(),
                archivado=%s
            WHERE id=%s
            """,
            (archivado, int(pago_id)),
        )
        conn.commit()

        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Pago no encontrado")

        return {"mensaje": "Pago marcado como pagado", "id": pago_id, "archivado": archivado}
    except HTTPException:
        raise
    except mysql.connector.Error as e:
        if BasicHelperFactory.table_missing_error(e):
            raise HTTPException(status_code=400, detail="Tabla pagos no existe. Crea la tabla para usar este modulo.")
        raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        conn.close()


@router.patch("/pagos/{pago_id}/archivar")
def archivar_pago(pago_id: int, value: int = Query(default=1, ge=0, le=1)):
    conn = get_db_connection()
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("UPDATE pagos SET archivado=%s WHERE id=%s", (int(value), int(pago_id)))
        conn.commit()

        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Pago no encontrado")

        return {"mensaje": "Archivado actualizado", "id": pago_id, "archivado": int(value)}
    except HTTPException:
        raise
    except mysql.connector.Error as e:
        if BasicHelperFactory.table_missing_error(e):
            raise HTTPException(status_code=400, detail="Tabla pagos no existe. Crea la tabla para usar este modulo.")
        raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        conn.close()


# =========================================================
# USUARIOS / ACCESOS
# =========================================================

@router.get("/usuarios")
def listar_usuarios(limit: int = Query(default=200, ge=1, le=2000)):
    conn = get_db_connection()
    try:
        cols = DBIntrospectionFactory.get_table_columns(conn, "usuarios")
        select_fields = ["id", "username", "rol", "nombre_completo", "archivo_url"]
        select_fields.append("activo" if "activo" in cols else "1 AS activo")
        select_fields.append("must_change_password" if "must_change_password" in cols else "0 AS must_change_password")
        select_fields.append("last_login_at" if "last_login_at" in cols else "NULL AS last_login_at")

        sql = f"""
            SELECT {', '.join(select_fields)}
            FROM usuarios
            ORDER BY id DESC
            LIMIT %s
        """
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(sql, (limit,))
            return {"datos": cur.fetchall() or []}
        finally:
            try:
                cur.close()
            except Exception:
                pass
    except mysql.connector.Error as e:
        raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
    finally:
        conn.close()


@router.patch("/usuarios/{user_id}/estado")
def set_activo_usuario(user_id: int, payload: ToggleActivoRequest):
    if payload.activo not in (0, 1):
        raise HTTPException(status_code=400, detail="activo debe ser 0 o 1")

    conn = get_db_connection()
    try:
        cols = DBIntrospectionFactory.get_table_columns(conn, "usuarios")
        DBIntrospectionFactory.require_usuario_columns(
            cols,
            ["activo"],
            hint="Agrega usuarios.activo (TINYINT(1) DEFAULT 1).",
        )

        cur = conn.cursor()
        try:
            cur.execute("UPDATE usuarios SET activo = %s WHERE id = %s", (payload.activo, user_id))
            conn.commit()

            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Usuario no encontrado")

            return {"mensaje": "Estado de acceso actualizado", "activo": payload.activo}
        finally:
            try:
                cur.close()
            except Exception:
                pass
    except HTTPException:
        raise
    except mysql.connector.Error as e:
        raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
    finally:
        conn.close()


@router.patch("/usuarios/{user_id}/forzar-cambio-clave")
def forzar_cambio_clave(user_id: int, value: int = Query(default=1, ge=0, le=1)):
    conn = get_db_connection()
    try:
        cols = DBIntrospectionFactory.get_table_columns(conn, "usuarios")
        DBIntrospectionFactory.require_usuario_columns(
            cols,
            ["must_change_password"],
            hint="Agrega usuarios.must_change_password (TINYINT(1) DEFAULT 0).",
        )

        cur = conn.cursor()
        try:
            cur.execute("UPDATE usuarios SET must_change_password = %s WHERE id = %s", (value, user_id))
            conn.commit()

            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Usuario no encontrado")

            return {"mensaje": "Flag actualizado", "must_change_password": value}
        finally:
            try:
                cur.close()
            except Exception:
                pass
    except HTTPException:
        raise
    except mysql.connector.Error as e:
        raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
    finally:
        conn.close()


@router.post("/usuarios/{user_id}/iniciar-recuperacion")
def iniciar_recuperacion(user_id: int, payload: ForceResetRequest):
    ttl = int(payload.ttl_minutes or 0)
    if ttl < 5 or ttl > 240:
        raise HTTPException(status_code=400, detail="ttl_minutes debe estar entre 5 y 240")

    token = secrets.token_urlsafe(32)
    token_hash = BasicHelperFactory.sha256_hex(token)
    expira = datetime.now() + timedelta(minutes=ttl)

    conn = get_db_connection()
    cur = None
    try:
        cur = conn.cursor()

        try:
            cur.execute(
                "UPDATE usuarios_password_reset SET usado = 1 WHERE usuario_id = %s AND usado = 0",
                (user_id,),
            )
        except mysql.connector.Error as e:
            if BasicHelperFactory.table_missing_error(e):
                raise HTTPException(
                    status_code=400,
                    detail="Falta migracion: crea tabla usuarios_password_reset (recuperacion).",
                )
            raise

        cur.execute(
            """
            INSERT INTO usuarios_password_reset (usuario_id, token_hash, expira_en, usado)
            VALUES (%s, %s, %s, 0)
            """,
            (user_id, token_hash, expira),
        )
        conn.commit()

        return {
            "mensaje": "Recuperacion habilitada",
            "token": token,
            "expira_en": expira.isoformat(sep=" ", timespec="seconds"),
            "ttl_minutes": ttl,
        }
    except HTTPException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    except mysql.connector.Error as e:
        try:
            conn.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        conn.close()


def _enforce_password_history_last3(cursor, usuario_id: int, new_hash: str) -> None:
    try:
        cursor.execute(
            """
            SELECT password_hash
            FROM usuarios_password_history
            WHERE usuario_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 3
            """,
            (usuario_id,),
        )
        rows = cursor.fetchall() or []
        last_hashes: List[str] = []
        for r in rows:
            if isinstance(r, dict):
                last_hashes.append(r.get("password_hash", ""))
            else:
                last_hashes.append(r[0])
        if new_hash in last_hashes:
            raise HTTPException(status_code=400, detail="No puedes reutilizar una contrasena usada recientemente (ultimas 3).")
    except mysql.connector.Error as e:
        if BasicHelperFactory.table_missing_error(e):
            return
        raise


def _push_password_history(cursor, usuario_id: int, new_hash: str) -> None:
    try:
        cursor.execute(
            "INSERT INTO usuarios_password_history (usuario_id, password_hash) VALUES (%s, %s)",
            (usuario_id, new_hash),
        )
        cursor.execute(
            """
            DELETE FROM usuarios_password_history
            WHERE usuario_id = %s
              AND id NOT IN (
                SELECT id FROM (
                  SELECT id
                  FROM usuarios_password_history
                  WHERE usuario_id = %s
                  ORDER BY created_at DESC, id DESC
                  LIMIT 3
                ) t
              )
            """,
            (usuario_id, usuario_id),
        )
    except mysql.connector.Error as e:
        if BasicHelperFactory.table_missing_error(e):
            return
        raise


@router.post("/usuarios/{user_id}/cambiar-clave")
def cambiar_clave_usuario(user_id: int, payload: PasswordChangeRequest):
    nueva = (payload.password or "").strip()
    if len(nueva) < 6:
        raise HTTPException(status_code=400, detail="La clave debe tener al menos 6 caracteres")

    conn = get_db_connection()
    try:
        cols = DBIntrospectionFactory.get_table_columns(conn, "usuarios")
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT id, password FROM usuarios WHERE id = %s", (user_id,))
            u = cur.fetchone()
            if not u:
                raise HTTPException(status_code=404, detail="Usuario no encontrado")

            new_hash = BasicHelperFactory.hash_password_pbkdf2(nueva)

            _enforce_password_history_last3(cur, user_id, new_hash)

            if "must_change_password" in cols:
                cur.execute(
                    "UPDATE usuarios SET password = %s, must_change_password = 0 WHERE id = %s",
                    (new_hash, user_id),
                )
            else:
                cur.execute("UPDATE usuarios SET password = %s WHERE id = %s", (new_hash, user_id))

            _push_password_history(cur, user_id, new_hash)

            conn.commit()
            return {"mensaje": "Clave actualizada"}
        finally:
            try:
                cur.close()
            except Exception:
                pass
    except HTTPException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    except mysql.connector.Error as e:
        try:
            conn.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
    finally:
        conn.close()


@router.delete("/usuarios/{user_id}")
def eliminar_usuario(user_id: int):
    conn = get_db_connection()
    try:
        cols = DBIntrospectionFactory.get_table_columns(conn, "usuarios")
        DBIntrospectionFactory.require_usuario_columns(
            cols,
            ["activo"],
            hint="Agrega usuarios.activo (TINYINT(1) DEFAULT 1).",
        )

        cur = conn.cursor()
        try:
            cur.execute("UPDATE usuarios SET activo = 0 WHERE id = %s", (user_id,))
            conn.commit()

            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Usuario no encontrado")

            return {"mensaje": "Usuario bloqueado (borrado logico)"}
        finally:
            try:
                cur.close()
            except Exception:
                pass
    except HTTPException:
        raise
    except mysql.connector.Error as e:
        raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
    finally:
        conn.close()


@router.delete("/usuarios/{user_id}/borrado-fisico")
def eliminar_usuario_fisico(user_id: int):
    conn = get_db_connection()
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM usuarios WHERE id = %s", (user_id,))
        conn.commit()

        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")

        return {"mensaje": "Usuario eliminado (fisico)"}
    except mysql.connector.Error as e:
        raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        conn.close()


# =========================================================
# HEALTH
# =========================================================

@router.get("/health")
def health():
    return {
        "status": "ok",
        "router": "admin",
        "project_root": str(PROJECT_ROOT),
        "storage_dir": str(BASE_STORAGE),
    }