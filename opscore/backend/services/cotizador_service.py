# backend/services/cotizador_service.py
# -*- coding: utf-8 -*-
"""
ByMetal - Cotizador Service
Patrones:
- Singleton: una sola instancia del servicio y del registro de nombres
- Factory: normalización de empresas, construcción de respuestas, mapeo de filas

Objetivos:
- Congruencia entre backend y frontend
- Nombres estables para evitar incongruencias
- MariaDB / Debian friendly
- Fácil integración con FastAPI
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import pymysql
from pymysql.cursors import DictCursor


# =========================================================
# SINGLETON BASE
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
# DB CONFIG / DB MANAGER
# =========================================================

@dataclass(frozen=True)
class DBConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str = "utf8mb4"
    autocommit: bool = False


class DBConfigFactory:
    @staticmethod
    def from_env() -> DBConfig:
        return DBConfig(
            host=os.getenv("DB_HOST", "127.0.0.1"),
            port=int(os.getenv("DB_PORT", "3306")),
            user=os.getenv("DB_USER", ""),
            password=os.getenv("DB_PASSWORD", ""),
            database=os.getenv("DB_NAME", ""),
            charset=os.getenv("DB_CHARSET", "utf8mb4"),
            autocommit=False,
        )


class DBManager(metaclass=SingletonMeta):
    def __init__(self, config: Optional[DBConfig] = None) -> None:
        self.config = config or DBConfigFactory.from_env()

    def connect(self):
        return pymysql.connect(
            host=self.config.host,
            port=self.config.port,
            user=self.config.user,
            password=self.config.password,
            database=self.config.database,
            charset=self.config.charset,
            cursorclass=DictCursor,
            autocommit=self.config.autocommit,
        )


# =========================================================
# HELPERS
# =========================================================

def _to_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clean_optional_str(value: Any) -> Optional[str]:
    s = _clean_str(value)
    return s if s else None


# =========================================================
# CONTRACT REGISTRY (SINGLETON)
# =========================================================

class CotizadorContractRegistry(metaclass=SingletonMeta):
    """
    Registro central para nombres canónicos y aliases.
    Evita strings sueltos duplicados en el proyecto.
    """

    def __init__(self) -> None:
        self.views = {
            "cotizador": "cotizador",
            "pagos": "pagos",
        }

        self.response_keys = {
            "ok": "ok",
            "items": "items",
            "datos": "datos",   # compat legacy
            "item": "item",
            "total": "total",
            "message": "message",
            "error": "error",
        }

        self.frontend_fields = {
            "empresa_id": "empresa_id",
            "empresa_nombre": "empresa_nombre",
            "terminal_id": "terminal_id",
            "terminal_nombre": "terminal_nombre",
            "categoria_id": "categoria_id",
            "categoria_nombre": "categoria_nombre",
            "codigo": "codigo",
            "descripcion": "descripcion",
            "unidad": "unidad",
            "cantidad": "cantidad",
            "precio_unitario": "precio_unitario",
            "descuento_pct": "descuento_pct",
            "impuesto_pct": "impuesto_pct",
            "subtotal": "subtotal",
            "total_linea": "total_linea",
            "imagen_url": "imagen_url",
            "archivo_url": "archivo_url",
            "estado": "estado",
            "observaciones": "observaciones",
        }

        # Alias de empresa para congruencia con frontend
        self.empresa_aliases = {
            "RBU": "TRANSPORTES RBU",
            "TRANSPORTES RBU": "TRANSPORTES RBU",

            "METROPOL": "METROPOL CHILE",
            "METROPOL CHILE": "METROPOL CHILE",

            "METBUS": "METBUS",

            "GRAN AMÉRICA": "GRAN AMERICA",
            "GRAN AMERICA": "GRAN AMERICA",
        }

    def normalize_empresa_nombre(self, value: Any) -> Optional[str]:
        s = _clean_str(value).upper()
        if not s:
            return None
        return self.empresa_aliases.get(s, s)


# =========================================================
# FACTORIES
# =========================================================

class CotizadorResponseFactory:
    @staticmethod
    def success_items(items: List[Dict[str, Any]], message: str = "") -> Dict[str, Any]:
        return {
            "ok": True,
            "items": items,
            "datos": items,  # compatibilidad legacy
            "total": len(items),
            "message": message,
        }

    @staticmethod
    def success_item(item: Optional[Dict[str, Any]], message: str = "") -> Dict[str, Any]:
        return {
            "ok": True,
            "item": item,
            "message": message,
        }

    @staticmethod
    def error(message: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = {
            "ok": False,
            "error": message,
            "message": message,
        }
        if extra:
            data.update(extra)
        return data


class CotizadorRowFactory:
    @staticmethod
    def empresa(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": row.get("id"),
            "empresa_id": row.get("id"),
            "empresa_nombre": row.get("nombre") or row.get("empresa_nombre") or row.get("empresa"),
            "empresa_codigo": row.get("codigo"),
            "activo": row.get("activo", 1),
        }

    @staticmethod
    def terminal(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": row.get("id"),
            "terminal_id": row.get("id"),
            "empresa_id": row.get("empresa_id"),
            "terminal_nombre": row.get("nombre") or row.get("terminal_nombre"),
            "activo": row.get("activo", 1),
        }

    @staticmethod
    def categoria(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": row.get("id"),
            "categoria_id": row.get("id"),
            "empresa_id": row.get("empresa_id"),
            "categoria_nombre": row.get("nombre") or row.get("categoria_nombre"),
            "activo": row.get("activo", 1),
        }

    @staticmethod
    def item(row: Dict[str, Any]) -> Dict[str, Any]:
        cantidad = _to_decimal(row.get("cantidad"), "0")
        precio_unitario = _to_decimal(row.get("precio_unitario"), "0")
        descuento_pct = _to_decimal(row.get("descuento_pct"), "0")
        impuesto_pct = _to_decimal(row.get("impuesto_pct"), "0")

        subtotal_bruto = cantidad * precio_unitario
        descuento_monto = subtotal_bruto * (descuento_pct / Decimal("100"))
        subtotal = subtotal_bruto - descuento_monto
        impuesto_monto = subtotal * (impuesto_pct / Decimal("100"))
        total_linea = subtotal + impuesto_monto

        return {
            "id": row.get("id"),
            "cotizacion_id": row.get("cotizacion_id"),
            "empresa_id": row.get("empresa_id"),
            "empresa_nombre": row.get("empresa_nombre") or row.get("empresa"),
            "terminal_id": row.get("terminal_id"),
            "terminal_nombre": row.get("terminal_nombre"),
            "categoria_id": row.get("categoria_id"),
            "categoria_nombre": row.get("categoria_nombre"),
            "codigo": row.get("codigo"),
            "descripcion": row.get("descripcion"),
            "unidad": row.get("unidad"),
            "cantidad": float(cantidad),
            "precio_unitario": float(precio_unitario),
            "descuento_pct": float(descuento_pct),
            "impuesto_pct": float(impuesto_pct),
            "subtotal": float(subtotal),
            "total_linea": float(total_linea),
            "imagen_url": row.get("imagen_url"),
            "archivo_url": row.get("archivo_url"),
            "estado": row.get("estado"),
            "observaciones": row.get("observaciones"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    @staticmethod
    def cotizacion(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": row.get("id"),
            "empresa_id": row.get("empresa_id"),
            "empresa_nombre": row.get("empresa_nombre") or row.get("empresa"),
            "terminal_id": row.get("terminal_id"),
            "terminal_nombre": row.get("terminal_nombre"),
            "folio": row.get("folio"),
            "titulo": row.get("titulo"),
            "descripcion": row.get("descripcion"),
            "estado": row.get("estado"),
            "moneda": row.get("moneda"),
            "total": float(_to_decimal(row.get("total"), "0")),
            "observaciones": row.get("observaciones"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }


class EmpresaNombreFactory:
    def __init__(self) -> None:
        self.registry = CotizadorContractRegistry()

    def normalize(self, empresa: Any) -> Optional[str]:
        return self.registry.normalize_empresa_nombre(empresa)


# =========================================================
# REPOSITORY
# =========================================================

class CotizadorRepository:
    """
    Repositorio SQL puro.
    Mantiene queries concentradas y explícitas.
    """

    def __init__(self, db: Optional[DBManager] = None) -> None:
        self.db = db or DBManager()
        self.empresa_factory = EmpresaNombreFactory()

    # ---------------------------
    # Empresas
    # ---------------------------
    def list_empresas(self) -> List[Dict[str, Any]]:
        sql = """
            SELECT
                e.id,
                e.nombre,
                e.codigo,
                COALESCE(e.activo, 1) AS activo
            FROM empresas e
            ORDER BY e.nombre ASC
        """
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchall()

    # ---------------------------
    # Terminales
    # ---------------------------
    def list_terminales(self, empresa_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = """
            SELECT
                t.id,
                t.empresa_id,
                t.nombre,
                COALESCE(t.activo, 1) AS activo
            FROM terminales t
            WHERE 1=1
        """
        params: List[Any] = []
        if empresa_id:
            sql += " AND t.empresa_id = %s"
            params.append(empresa_id)

        sql += " ORDER BY t.nombre ASC"

        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    # ---------------------------
    # Categorías
    # ---------------------------
    def list_categorias(self, empresa_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Soporta:
        - cotizador_categorias
        - catalogo_categorias (si existe en tu proyecto, adapta solo aquí)
        """
        sql = """
            SELECT
                c.id,
                c.empresa_id,
                c.nombre,
                COALESCE(c.activo, 1) AS activo
            FROM cotizador_categorias c
            WHERE 1=1
        """
        params: List[Any] = []
        if empresa_id:
            sql += " AND c.empresa_id = %s"
            params.append(empresa_id)

        sql += " ORDER BY c.nombre ASC"

        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    # ---------------------------
    # Cotizaciones
    # ---------------------------
    def list_cotizaciones(
        self,
        empresa_id: Optional[int] = None,
        empresa: Optional[str] = None,
        terminal_id: Optional[int] = None,
        estado: Optional[str] = None,
        q: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        empresa_normalizada = self.empresa_factory.normalize(empresa)

        sql = """
            SELECT
                c.id,
                c.empresa_id,
                e.nombre AS empresa_nombre,
                c.terminal_id,
                t.nombre AS terminal_nombre,
                c.folio,
                c.titulo,
                c.descripcion,
                c.estado,
                c.moneda,
                c.total,
                c.observaciones,
                c.created_at,
                c.updated_at
            FROM cotizaciones c
            LEFT JOIN empresas e ON e.id = c.empresa_id
            LEFT JOIN terminales t ON t.id = c.terminal_id
            WHERE 1=1
        """
        params: List[Any] = []

        if empresa_id:
            sql += " AND c.empresa_id = %s"
            params.append(empresa_id)

        if empresa_normalizada:
            sql += " AND UPPER(e.nombre) = %s"
            params.append(empresa_normalizada.upper())

        if terminal_id:
            sql += " AND c.terminal_id = %s"
            params.append(terminal_id)

        if estado:
            sql += " AND UPPER(COALESCE(c.estado,'')) = %s"
            params.append(_clean_str(estado).upper())

        if q:
            sql += """
                AND (
                    c.folio LIKE %s OR
                    c.titulo LIKE %s OR
                    c.descripcion LIKE %s OR
                    e.nombre LIKE %s OR
                    t.nombre LIKE %s
                )
            """
            like = f"%{_clean_str(q)}%"
            params.extend([like, like, like, like, like])

        sql += " ORDER BY c.id DESC"

        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    def get_cotizacion(self, cotizacion_id: int) -> Optional[Dict[str, Any]]:
        sql = """
            SELECT
                c.id,
                c.empresa_id,
                e.nombre AS empresa_nombre,
                c.terminal_id,
                t.nombre AS terminal_nombre,
                c.folio,
                c.titulo,
                c.descripcion,
                c.estado,
                c.moneda,
                c.total,
                c.observaciones,
                c.created_at,
                c.updated_at
            FROM cotizaciones c
            LEFT JOIN empresas e ON e.id = c.empresa_id
            LEFT JOIN terminales t ON t.id = c.terminal_id
            WHERE c.id = %s
            LIMIT 1
        """
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (cotizacion_id,))
                return cur.fetchone()

    def create_cotizacion(self, payload: Dict[str, Any]) -> int:
        sql = """
            INSERT INTO cotizaciones (
                empresa_id,
                terminal_id,
                folio,
                titulo,
                descripcion,
                estado,
                moneda,
                total,
                observaciones
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        params = (
            payload.get("empresa_id"),
            payload.get("terminal_id"),
            _clean_optional_str(payload.get("folio")),
            _clean_optional_str(payload.get("titulo")),
            _clean_optional_str(payload.get("descripcion")),
            _clean_str(payload.get("estado") or "borrador"),
            _clean_str(payload.get("moneda") or "CLP"),
            float(_to_decimal(payload.get("total"), "0")),
            _clean_optional_str(payload.get("observaciones")),
        )

        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                new_id = cur.lastrowid
            conn.commit()
            return int(new_id)

    def update_cotizacion(self, cotizacion_id: int, payload: Dict[str, Any]) -> int:
        sql = """
            UPDATE cotizaciones
            SET
                empresa_id = %s,
                terminal_id = %s,
                folio = %s,
                titulo = %s,
                descripcion = %s,
                estado = %s,
                moneda = %s,
                total = %s,
                observaciones = %s
            WHERE id = %s
        """
        params = (
            payload.get("empresa_id"),
            payload.get("terminal_id"),
            _clean_optional_str(payload.get("folio")),
            _clean_optional_str(payload.get("titulo")),
            _clean_optional_str(payload.get("descripcion")),
            _clean_str(payload.get("estado") or "borrador"),
            _clean_str(payload.get("moneda") or "CLP"),
            float(_to_decimal(payload.get("total"), "0")),
            _clean_optional_str(payload.get("observaciones")),
            cotizacion_id,
        )

        with self.db.connect() as conn:
            with conn.cursor() as cur:
                rows = cur.execute(sql, params)
            conn.commit()
            return int(rows)

    def delete_cotizacion(self, cotizacion_id: int) -> int:
        sql = "DELETE FROM cotizaciones WHERE id = %s"
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                rows = cur.execute(sql, (cotizacion_id,))
            conn.commit()
            return int(rows)

    # ---------------------------
    # Items
    # ---------------------------
    def list_items(
        self,
        cotizacion_id: Optional[int] = None,
        empresa_id: Optional[int] = None,
        empresa: Optional[str] = None,
        categoria_id: Optional[int] = None,
        q: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        empresa_normalizada = self.empresa_factory.normalize(empresa)

        sql = """
            SELECT
                i.id,
                i.cotizacion_id,
                i.empresa_id,
                e.nombre AS empresa_nombre,
                i.terminal_id,
                t.nombre AS terminal_nombre,
                i.categoria_id,
                c.nombre AS categoria_nombre,
                i.codigo,
                i.descripcion,
                i.unidad,
                i.cantidad,
                i.precio_unitario,
                COALESCE(i.descuento_pct, 0) AS descuento_pct,
                COALESCE(i.impuesto_pct, 0) AS impuesto_pct,
                i.imagen_url,
                i.archivo_url,
                i.estado,
                i.observaciones,
                i.created_at,
                i.updated_at
            FROM cotizacion_items i
            LEFT JOIN empresas e ON e.id = i.empresa_id
            LEFT JOIN terminales t ON t.id = i.terminal_id
            LEFT JOIN cotizador_categorias c ON c.id = i.categoria_id
            WHERE 1=1
        """
        params: List[Any] = []

        if cotizacion_id:
            sql += " AND i.cotizacion_id = %s"
            params.append(cotizacion_id)

        if empresa_id:
            sql += " AND i.empresa_id = %s"
            params.append(empresa_id)

        if empresa_normalizada:
            sql += " AND UPPER(e.nombre) = %s"
            params.append(empresa_normalizada.upper())

        if categoria_id:
            sql += " AND i.categoria_id = %s"
            params.append(categoria_id)

        if q:
            sql += """
                AND (
                    i.codigo LIKE %s OR
                    i.descripcion LIKE %s OR
                    c.nombre LIKE %s OR
                    e.nombre LIKE %s OR
                    t.nombre LIKE %s
                )
            """
            like = f"%{_clean_str(q)}%"
            params.extend([like, like, like, like, like])

        sql += " ORDER BY i.id DESC"

        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    def get_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        sql = """
            SELECT
                i.id,
                i.cotizacion_id,
                i.empresa_id,
                e.nombre AS empresa_nombre,
                i.terminal_id,
                t.nombre AS terminal_nombre,
                i.categoria_id,
                c.nombre AS categoria_nombre,
                i.codigo,
                i.descripcion,
                i.unidad,
                i.cantidad,
                i.precio_unitario,
                COALESCE(i.descuento_pct, 0) AS descuento_pct,
                COALESCE(i.impuesto_pct, 0) AS impuesto_pct,
                i.imagen_url,
                i.archivo_url,
                i.estado,
                i.observaciones,
                i.created_at,
                i.updated_at
            FROM cotizacion_items i
            LEFT JOIN empresas e ON e.id = i.empresa_id
            LEFT JOIN terminales t ON t.id = i.terminal_id
            LEFT JOIN cotizador_categorias c ON c.id = i.categoria_id
            WHERE i.id = %s
            LIMIT 1
        """
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (item_id,))
                return cur.fetchone()

    def create_item(self, payload: Dict[str, Any]) -> int:
        sql = """
            INSERT INTO cotizacion_items (
                cotizacion_id,
                empresa_id,
                terminal_id,
                categoria_id,
                codigo,
                descripcion,
                unidad,
                cantidad,
                precio_unitario,
                descuento_pct,
                impuesto_pct,
                imagen_url,
                archivo_url,
                estado,
                observaciones
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        params = (
            payload.get("cotizacion_id"),
            payload.get("empresa_id"),
            payload.get("terminal_id"),
            payload.get("categoria_id"),
            _clean_optional_str(payload.get("codigo")),
            _clean_str(payload.get("descripcion")),
            _clean_str(payload.get("unidad") or "UN"),
            float(_to_decimal(payload.get("cantidad"), "0")),
            float(_to_decimal(payload.get("precio_unitario"), "0")),
            float(_to_decimal(payload.get("descuento_pct"), "0")),
            float(_to_decimal(payload.get("impuesto_pct"), "0")),
            _clean_optional_str(payload.get("imagen_url")),
            _clean_optional_str(payload.get("archivo_url")),
            _clean_str(payload.get("estado") or "activo"),
            _clean_optional_str(payload.get("observaciones")),
        )

        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                new_id = cur.lastrowid
            conn.commit()
            return int(new_id)

    def update_item(self, item_id: int, payload: Dict[str, Any]) -> int:
        sql = """
            UPDATE cotizacion_items
            SET
                cotizacion_id = %s,
                empresa_id = %s,
                terminal_id = %s,
                categoria_id = %s,
                codigo = %s,
                descripcion = %s,
                unidad = %s,
                cantidad = %s,
                precio_unitario = %s,
                descuento_pct = %s,
                impuesto_pct = %s,
                imagen_url = %s,
                archivo_url = %s,
                estado = %s,
                observaciones = %s
            WHERE id = %s
        """
        params = (
            payload.get("cotizacion_id"),
            payload.get("empresa_id"),
            payload.get("terminal_id"),
            payload.get("categoria_id"),
            _clean_optional_str(payload.get("codigo")),
            _clean_str(payload.get("descripcion")),
            _clean_str(payload.get("unidad") or "UN"),
            float(_to_decimal(payload.get("cantidad"), "0")),
            float(_to_decimal(payload.get("precio_unitario"), "0")),
            float(_to_decimal(payload.get("descuento_pct"), "0")),
            float(_to_decimal(payload.get("impuesto_pct"), "0")),
            _clean_optional_str(payload.get("imagen_url")),
            _clean_optional_str(payload.get("archivo_url")),
            _clean_str(payload.get("estado") or "activo"),
            _clean_optional_str(payload.get("observaciones")),
            item_id,
        )

        with self.db.connect() as conn:
            with conn.cursor() as cur:
                rows = cur.execute(sql, params)
            conn.commit()
            return int(rows)

    def delete_item(self, item_id: int) -> int:
        sql = "DELETE FROM cotizacion_items WHERE id = %s"
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                rows = cur.execute(sql, (item_id,))
            conn.commit()
            return int(rows)


# =========================================================
# SERVICE (SINGLETON)
# =========================================================

class CotizadorService(metaclass=SingletonMeta):
    """
    Fachada de negocio estable para routers/controladores.
    """

    def __init__(self, repository: Optional[CotizadorRepository] = None) -> None:
        self.repository = repository or CotizadorRepository()
        self.row_factory = CotizadorRowFactory()
        self.response_factory = CotizadorResponseFactory()
        self.empresa_factory = EmpresaNombreFactory()

    # ---------------------------
    # Empresas / terminales / categorías
    # ---------------------------
    def listar_empresas(self) -> Dict[str, Any]:
        rows = self.repository.list_empresas()
        items = [self.row_factory.empresa(r) for r in rows]
        return self.response_factory.success_items(items)

    def listar_terminales(self, empresa_id: Optional[int] = None) -> Dict[str, Any]:
        rows = self.repository.list_terminales(empresa_id=empresa_id)
        items = [self.row_factory.terminal(r) for r in rows]
        return self.response_factory.success_items(items)

    def listar_categorias(self, empresa_id: Optional[int] = None) -> Dict[str, Any]:
        rows = self.repository.list_categorias(empresa_id=empresa_id)
        items = [self.row_factory.categoria(r) for r in rows]
        return self.response_factory.success_items(items)

    # ---------------------------
    # Cotizaciones
    # ---------------------------
    def listar_cotizaciones(
        self,
        empresa_id: Optional[int] = None,
        empresa: Optional[str] = None,
        terminal_id: Optional[int] = None,
        estado: Optional[str] = None,
        q: Optional[str] = None,
    ) -> Dict[str, Any]:
        rows = self.repository.list_cotizaciones(
            empresa_id=empresa_id,
            empresa=empresa,
            terminal_id=terminal_id,
            estado=estado,
            q=q,
        )
        items = [self.row_factory.cotizacion(r) for r in rows]
        return self.response_factory.success_items(items)

    def obtener_cotizacion(self, cotizacion_id: int) -> Dict[str, Any]:
        row = self.repository.get_cotizacion(cotizacion_id)
        item = self.row_factory.cotizacion(row) if row else None
        return self.response_factory.success_item(item)

    def crear_cotizacion(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_cotizacion_payload(payload)
        new_id = self.repository.create_cotizacion(normalized)
        return self.obtener_cotizacion(new_id)

    def actualizar_cotizacion(self, cotizacion_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_cotizacion_payload(payload)
        self.repository.update_cotizacion(cotizacion_id, normalized)
        return self.obtener_cotizacion(cotizacion_id)

    def eliminar_cotizacion(self, cotizacion_id: int) -> Dict[str, Any]:
        rows = self.repository.delete_cotizacion(cotizacion_id)
        return {
            "ok": rows > 0,
            "deleted": rows,
            "id": cotizacion_id,
        }

    # ---------------------------
    # Items
    # ---------------------------
    def listar_items(
        self,
        cotizacion_id: Optional[int] = None,
        empresa_id: Optional[int] = None,
        empresa: Optional[str] = None,
        categoria_id: Optional[int] = None,
        q: Optional[str] = None,
    ) -> Dict[str, Any]:
        rows = self.repository.list_items(
            cotizacion_id=cotizacion_id,
            empresa_id=empresa_id,
            empresa=empresa,
            categoria_id=categoria_id,
            q=q,
        )
        items = [self.row_factory.item(r) for r in rows]
        return self.response_factory.success_items(items)

    def obtener_item(self, item_id: int) -> Dict[str, Any]:
        row = self.repository.get_item(item_id)
        item = self.row_factory.item(row) if row else None
        return self.response_factory.success_item(item)

    def crear_item(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_item_payload(payload)
        new_id = self.repository.create_item(normalized)
        return self.obtener_item(new_id)

    def actualizar_item(self, item_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_item_payload(payload)
        self.repository.update_item(item_id, normalized)
        return self.obtener_item(item_id)

    def eliminar_item(self, item_id: int) -> Dict[str, Any]:
        rows = self.repository.delete_item(item_id)
        return {
            "ok": rows > 0,
            "deleted": rows,
            "id": item_id,
        }

    # ---------------------------
    # Normalización
    # ---------------------------
    def _normalize_cotizacion_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(payload)

        out["empresa_id"] = _to_int(payload.get("empresa_id"), 0) or None
        out["terminal_id"] = _to_int(payload.get("terminal_id"), 0) or None
        out["folio"] = _clean_optional_str(payload.get("folio"))
        out["titulo"] = _clean_optional_str(payload.get("titulo"))
        out["descripcion"] = _clean_optional_str(payload.get("descripcion"))
        out["estado"] = _clean_str(payload.get("estado") or "borrador")
        out["moneda"] = _clean_str(payload.get("moneda") or "CLP")
        out["total"] = float(_to_decimal(payload.get("total"), "0"))
        out["observaciones"] = _clean_optional_str(payload.get("observaciones"))
        return out

    def _normalize_item_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(payload)

        out["cotizacion_id"] = _to_int(payload.get("cotizacion_id"), 0) or None
        out["empresa_id"] = _to_int(payload.get("empresa_id"), 0) or None
        out["terminal_id"] = _to_int(payload.get("terminal_id"), 0) or None
        out["categoria_id"] = _to_int(payload.get("categoria_id"), 0) or None

        out["codigo"] = _clean_optional_str(payload.get("codigo"))
        out["descripcion"] = _clean_str(payload.get("descripcion"))
        out["unidad"] = _clean_str(payload.get("unidad") or "UN")

        out["cantidad"] = float(_to_decimal(payload.get("cantidad"), "0"))
        out["precio_unitario"] = float(_to_decimal(payload.get("precio_unitario"), "0"))
        out["descuento_pct"] = float(_to_decimal(payload.get("descuento_pct"), "0"))
        out["impuesto_pct"] = float(_to_decimal(payload.get("impuesto_pct"), "0"))

        out["imagen_url"] = _clean_optional_str(payload.get("imagen_url"))
        out["archivo_url"] = _clean_optional_str(payload.get("archivo_url"))
        out["estado"] = _clean_str(payload.get("estado") or "activo")
        out["observaciones"] = _clean_optional_str(payload.get("observaciones"))

        return out


# =========================================================
# FACADE HELPERS
# =========================================================

def get_cotizador_service() -> CotizadorService:
    return CotizadorService()