# backend/routes/pagos.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Any, Dict, Set

import mysql.connector
from fastapi import APIRouter, HTTPException, Query, Body
from pydantic import BaseModel, Field


# ==========================================================
# Routers
# ==========================================================
router = APIRouter(prefix="/api/pagos", tags=["pagos"])
admin_router = APIRouter(prefix="/api/admin/pagos", tags=["admin-pagos"])

# Mantener compatibilidad sin tocar app.py
router.include_router(admin_router)


# ==========================================================
# SINGLETON META
# ==========================================================
class SingletonMeta(type):
    _instances: Dict[type, object] = {}
    _lock = threading.Lock()

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with cls._lock:
                if cls not in cls._instances:
                    cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


# ==========================================================
# DB
# ==========================================================
@dataclass(frozen=True)
class DBConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


class PagosDBConfigFactory:
    @staticmethod
    def from_env() -> DBConfig:
        host = os.getenv("DB_HOST", "127.0.0.1")
        port = int(os.getenv("DB_PORT", "3306"))
        user = os.getenv("DB_USER")
        password = os.getenv("DB_PASSWORD")
        database = os.getenv("DB_NAME")

        if not user or not password or not database:
            raise HTTPException(
                status_code=500,
                detail="Variables DB_* no configuradas correctamente (DB_USER, DB_PASSWORD, DB_NAME)",
            )

        return DBConfig(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
        )


class PagosDBConnectionManager(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.config = PagosDBConfigFactory.from_env()

    def connect(self):
        try:
            return mysql.connector.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
                connection_timeout=5,
            )
        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error conexion DB: {str(e)}")


def get_db_connection():
    return PagosDBConnectionManager().connect()


# ==========================================================
# MODELOS
# ==========================================================
class EstadoPagos(str, Enum):
    pendientes = "pendientes"
    archivados = "archivados"
    todas = "todas"
    pagados = "pagados"


class MarcarPagadoRequest(BaseModel):
    archivado: int = Field(default=1, ge=0, le=1)


class SyncDesdeCediblesRequest(BaseModel):
    solo_pendientes: bool = True
    force_update: bool = False
    limit: int = Field(default=5000, ge=1, le=50000)


# ==========================================================
# CONTRACT REGISTRY
# ==========================================================
class PagosContractRegistry(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.empresa_aliases = {
            "RBU": "TRANSPORTES RBU",
            "TRANSPORTES RBU": "TRANSPORTES RBU",
            "METROPOL": "METROPOL CHILE",
            "METROPOL CHILE": "METROPOL CHILE",
            "METBUS": "METBUS",
            "GRAN AMERICA": "GRAN AMERICA",
            "GRAN AMERICA ": "GRAN AMERICA",
        }

    def normalize_empresa_alias(self, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        key = raw.upper()
        return self.empresa_aliases.get(key, raw)


# ==========================================================
# HELPERS / FACTORIES
# ==========================================================
class PagosHelperFactory:
    @staticmethod
    def table_columns(cur, table: str) -> Set[str]:
        cur.execute(f"SHOW COLUMNS FROM {table}")
        rows = cur.fetchall() or []
        cols = set()
        for r in rows:
            if isinstance(r, dict):
                cols.add(str(r.get("Field")))
            else:
                cols.add(str(r[0]))
        return cols

    @staticmethod
    def pick_first_col(available: Set[str], candidates: List[str]) -> Optional[str]:
        for c in candidates:
            if c in available:
                return c
        return None

    @staticmethod
    def normalize_empresa_like(value: Optional[str]) -> Optional[str]:
        if not value or not str(value).strip():
            return None
        return PagosContractRegistry().normalize_empresa_alias(value)

    @staticmethod
    def table_missing_error(e: mysql.connector.Error) -> bool:
        return getattr(e, "errno", None) == 1146

    @staticmethod
    def response_items(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "datos": rows,
            "items": rows,
        }


class PagosResponseFactory:
    @staticmethod
    def ok_message(message: str, **extra) -> Dict[str, Any]:
        out = {"mensaje": message}
        out.update(extra)
        return out


# ==========================================================
# SERVICE
# ==========================================================
class PagosService(metaclass=SingletonMeta):
    def fetch_pagos(
        self,
        *,
        estado: Optional[EstadoPagos] = None,
        solo_pendientes_no_archivados: bool = False,
        empresa_id: Optional[int] = None,
        empresa_nombre: Optional[str] = None,
        q: Optional[str] = None,
        limit: int = 1000,
    ) -> Dict[str, Any]:
        conn = get_db_connection()
        cur = None
        try:
            cur = conn.cursor(dictionary=True)

            where: List[str] = []
            params: List[Any] = []

            if solo_pendientes_no_archivados:
                where += ["estado_pago <> 'pagado'", "archivado = 0"]
            else:
                if estado == EstadoPagos.pendientes:
                    where += ["estado_pago <> 'pagado'", "archivado = 0"]
                elif estado == EstadoPagos.archivados:
                    where += ["archivado = 1"]
                elif estado == EstadoPagos.todas:
                    where += ["estado_pago <> 'pagado'"]
                elif estado == EstadoPagos.pagados:
                    where += ["estado_pago = 'pagado'", "archivado = 0"]

            if empresa_id is not None:
                where.append("empresa_id = %s")
                params.append(int(empresa_id))

            empresa_norm = PagosHelperFactory.normalize_empresa_like(empresa_nombre)
            if empresa_norm:
                where.append("COALESCE(empresa,'') LIKE %s")
                params.append(f"%{empresa_norm}%")

            if q and q.strip():
                qq = f"%{q.strip()}%"
                where.append("(COALESCE(empresa,'') LIKE %s OR CAST(numero_factura AS CHAR) LIKE %s)")
                params.extend([qq, qq])

            sql = f"""
                SELECT
                    id,
                    cedible_id,
                    numero_factura,
                    empresa,
                    empresa_id,
                    monto_total,
                    fecha_emision,
                    fecha_vencimiento,
                    estado_pago,
                    archivo_url,
                    archivado,
                    pagado_at,
                    created_at
                FROM pagos
                {"WHERE " + " AND ".join(where) if where else ""}
                ORDER BY
                    CASE WHEN fecha_vencimiento IS NULL THEN 1 ELSE 0 END,
                    fecha_vencimiento ASC,
                    id DESC
                LIMIT %s
            """
            params.append(int(limit))
            cur.execute(sql, params)
            rows = cur.fetchall() or []
            return PagosHelperFactory.response_items(rows)

        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
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

    def update_pagado(self, pago_id: int, archivado: int) -> Dict[str, Any]:
        conn = get_db_connection()
        cur = None
        try:
            conn.start_transaction()
            cur = conn.cursor(dictionary=True)

            archivado_val = 1 if int(archivado) == 1 else 0

            cur.execute("SELECT id, cedible_id FROM pagos WHERE id = %s LIMIT 1", (int(pago_id),))
            pago = cur.fetchone()
            if not pago:
                raise HTTPException(status_code=404, detail="Pago no encontrado")

            cedible_id = pago.get("cedible_id")

            cur.execute(
                """
                UPDATE pagos
                SET estado_pago = 'pagado',
                    pagado_at = NOW(),
                    archivado = %s
                WHERE id = %s
                """,
                (archivado_val, int(pago_id)),
            )

            if cedible_id is not None:
                if archivado_val == 1:
                    cur.execute(
                        """
                        UPDATE cedibles c
                        JOIN pagos p ON p.cedible_id = c.id
                        SET c.estado = 'Pagado',
                            c.pagado_at = COALESCE(c.pagado_at, p.pagado_at, NOW()),
                            c.archivado = 1
                        WHERE p.id = %s
                        """,
                        (int(pago_id),),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE cedibles c
                        JOIN pagos p ON p.cedible_id = c.id
                        SET c.estado = 'Pagado',
                            c.pagado_at = COALESCE(c.pagado_at, p.pagado_at, NOW())
                        WHERE p.id = %s
                        """,
                        (int(pago_id),),
                    )

            conn.commit()
            return PagosResponseFactory.ok_message(
                "Pago marcado como pagado",
                id=int(pago_id),
                archivado=archivado_val,
            )

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
            try:
                conn.close()
            except Exception:
                pass

    def update_archivado(self, pago_id: int, value: int) -> Dict[str, Any]:
        conn = get_db_connection()
        cur = None
        try:
            conn.start_transaction()
            cur = conn.cursor(dictionary=True)

            val = 1 if int(value) == 1 else 0

            cur.execute(
                "SELECT id, cedible_id, estado_pago, pagado_at FROM pagos WHERE id=%s LIMIT 1",
                (int(pago_id),),
            )
            p = cur.fetchone()
            if not p:
                raise HTTPException(status_code=404, detail="Pago no encontrado")

            cur.execute("UPDATE pagos SET archivado = %s WHERE id = %s", (val, int(pago_id)))

            if val == 1 and str(p.get("estado_pago") or "").strip().lower() == "pagado" and p.get("cedible_id") is not None:
                cur.execute(
                    """
                    UPDATE cedibles
                    SET estado = 'Pagado',
                        pagado_at = COALESCE(pagado_at, %s, NOW()),
                        archivado = 1
                    WHERE id = %s
                    """,
                    (p.get("pagado_at"), int(p.get("cedible_id"))),
                )

            conn.commit()
            return PagosResponseFactory.ok_message(
                "Estado archivado actualizado",
                id=int(pago_id),
                archivado=val,
            )

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
            try:
                conn.close()
            except Exception:
                pass

    def sync_pagos_desde_cedibles(
        self,
        *,
        solo_pendientes: bool,
        force_update: bool,
        limit: int,
    ) -> Dict[str, Any]:
        conn = get_db_connection()
        cur = None
        wcur = None
        try:
            cur = conn.cursor(dictionary=True)

            ced_cols = PagosHelperFactory.table_columns(cur, "cedibles")

            col_num_fact = PagosHelperFactory.pick_first_col(ced_cols, ["numero_factura", "num_factura", "factura_numero", "nro_factura"])
            col_empresa = PagosHelperFactory.pick_first_col(ced_cols, ["cliente", "empresa", "razon_social", "cliente_nombre"])
            col_emp_id = PagosHelperFactory.pick_first_col(ced_cols, ["empresa_id", "cliente_id"])
            col_monto = PagosHelperFactory.pick_first_col(ced_cols, ["monto_total", "total", "monto", "importe_total"])
            col_fecha_emision = PagosHelperFactory.pick_first_col(ced_cols, ["fecha_factura", "fecha_emision", "fecha", "fecha_doc"])
            col_fecha_venc = PagosHelperFactory.pick_first_col(ced_cols, ["fecha_vencimiento", "vencimiento", "fecha_vence", "fecha_pago"])
            col_archivo = PagosHelperFactory.pick_first_col(ced_cols, ["archivo_url", "archivo", "url_archivo"])
            col_estado = PagosHelperFactory.pick_first_col(ced_cols, ["estado", "estado_cedible"])
            col_archivado = PagosHelperFactory.pick_first_col(ced_cols, ["archivado"])
            col_pagado_at = PagosHelperFactory.pick_first_col(ced_cols, ["pagado_at"])

            if not col_num_fact:
                return {
                    "mensaje": "Sync abortado: cedibles no tiene columna de numero_factura compatible",
                    "cedibles_leidos": 0,
                    "nuevos": 0,
                    "actualizados": 0,
                    "solo_pendientes": solo_pendientes,
                    "force_update": force_update,
                    "limit": limit,
                }

            where: List[str] = [f"({col_num_fact} IS NOT NULL AND {col_num_fact} <> '')"]
            params: List[Any] = []

            if solo_pendientes and col_estado:
                where.append(f"LOWER({col_estado}) = 'pendiente'")

            select_parts = [
                "id AS id",
                (f"{col_emp_id} AS empresa_id" if col_emp_id else "NULL AS empresa_id"),
                (f"{col_empresa} AS empresa" if col_empresa else "NULL AS empresa"),
                f"{col_num_fact} AS numero_factura",
                (f"{col_fecha_emision} AS fecha_emision" if col_fecha_emision else "NULL AS fecha_emision"),
                (f"{col_fecha_venc} AS fecha_vencimiento" if col_fecha_venc else "NULL AS fecha_vencimiento"),
                (f"{col_monto} AS monto_total" if col_monto else "NULL AS monto_total"),
                (f"{col_archivo} AS archivo_url" if col_archivo else "NULL AS archivo_url"),
                (f"{col_archivado} AS archivado" if col_archivado else "0 AS archivado"),
                (f"{col_pagado_at} AS pagado_at" if col_pagado_at else "NULL AS pagado_at"),
            ]

            sql_ced = f"""
                SELECT {", ".join(select_parts)}
                FROM cedibles
                WHERE {" AND ".join(where)}
                ORDER BY id DESC
                LIMIT %s
            """
            params.append(int(limit))

            cur.execute(sql_ced, params)
            cedibles = cur.fetchall() or []

            nuevos = 0
            actualizados = 0

            wcur = conn.cursor()

            for c in cedibles:
                cedible_id = int(c["id"])
                empresa_id = c.get("empresa_id")
                empresa = (c.get("empresa") or "").strip()
                numero_factura = str(c.get("numero_factura") or "").strip()

                monto_total = c.get("monto_total")
                fecha_emision = c.get("fecha_emision")
                fecha_vencimiento = c.get("fecha_vencimiento")
                archivo_url = c.get("archivo_url")

                archivado_val = int(c.get("archivado") or 0)
                pagado_at = c.get("pagado_at")
                estado_pago = "pagado" if pagado_at else "pendiente"

                cur.execute("SELECT id, estado_pago FROM pagos WHERE cedible_id = %s LIMIT 1", (cedible_id,))
                existing = cur.fetchone()

                if not existing:
                    wcur.execute(
                        """
                        INSERT INTO pagos
                            (cedible_id, numero_factura, empresa, empresa_id, monto_total,
                             fecha_emision, fecha_vencimiento, estado_pago, archivo_url,
                             archivado, pagado_at, created_at)
                        VALUES
                            (%s, %s, %s, %s, %s,
                             %s, %s, %s, %s,
                             %s, %s, NOW())
                        """,
                        (
                            cedible_id,
                            numero_factura,
                            empresa if empresa else None,
                            empresa_id,
                            monto_total,
                            fecha_emision,
                            fecha_vencimiento,
                            estado_pago,
                            archivo_url,
                            archivado_val,
                            pagado_at,
                        ),
                    )
                    nuevos += 1
                    continue

                if (str(existing.get("estado_pago") or "").lower() == "pagado") and (not force_update):
                    continue

                wcur.execute(
                    """
                    UPDATE pagos
                    SET
                        numero_factura = %s,
                        empresa = %s,
                        empresa_id = %s,
                        monto_total = %s,
                        fecha_emision = %s,
                        fecha_vencimiento = %s,
                        archivo_url = %s,
                        archivado = %s,
                        pagado_at = %s,
                        estado_pago = %s
                    WHERE cedible_id = %s
                    """,
                    (
                        numero_factura,
                        empresa if empresa else None,
                        empresa_id,
                        monto_total,
                        fecha_emision,
                        fecha_vencimiento,
                        archivo_url,
                        archivado_val,
                        pagado_at,
                        estado_pago,
                        cedible_id,
                    ),
                )
                if wcur.rowcount > 0:
                    actualizados += 1

            conn.commit()

            return {
                "mensaje": "Sync OK",
                "cedibles_leidos": len(cedibles),
                "nuevos": nuevos,
                "actualizados": actualizados,
                "solo_pendientes": bool(solo_pendientes),
                "force_update": bool(force_update),
                "limit": int(limit),
            }

        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
        finally:
            try:
                if wcur:
                    wcur.close()
            except Exception:
                pass
            try:
                if cur:
                    cur.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


# ==========================================================
# /api/pagos (frontend)
# ==========================================================
@router.get("")
@router.get("/")
def listar_pagos(
    estado: EstadoPagos = Query(default=EstadoPagos.pendientes),
    empresa_id: Optional[int] = Query(default=None),
    q: Optional[str] = Query(default=None, description="Busqueda por empresa o numero_factura"),
    limit: int = Query(default=1000, ge=1, le=5000),
):
    return PagosService().fetch_pagos(
        estado=estado,
        empresa_id=empresa_id,
        q=q,
        limit=limit,
    )


@router.get("/pendientes")
def listar_pendientes(
    empresa_id: Optional[int] = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=5000),
):
    return PagosService().fetch_pagos(
        solo_pendientes_no_archivados=True,
        empresa_id=empresa_id,
        limit=limit,
    )


@router.get("/resumen")
def resumen_por_empresa():
    conn = get_db_connection()
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT
                empresa_id,
                COALESCE(empresa,'') AS empresa,
                COUNT(*) AS cantidad,
                SUM(monto_total) AS total_pendiente
            FROM pagos
            WHERE estado_pago <> 'pagado'
              AND archivado = 0
            GROUP BY empresa_id, COALESCE(empresa,'')
            ORDER BY COALESCE(empresa,'')
            """
        )
        rows = cur.fetchall() or []
        return {"datos": rows, "items": rows}
    except mysql.connector.Error as e:
        raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
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


@router.patch("/{pago_id}/marcar-pagado")
def marcar_pagado(pago_id: int, payload: MarcarPagadoRequest):
    return PagosService().update_pagado(pago_id, payload.archivado)


@router.patch("/{pago_id}/archivar")
def archivar_pago(pago_id: int, value: int = Query(default=1, ge=0, le=1)):
    return PagosService().update_archivado(pago_id, value)


@router.get("/resumen-mensual")
def resumen_mensual_pagos(
    year: Optional[int] = Query(default=None),
    month: Optional[int] = Query(default=None, ge=1, le=12),
    empresa: Optional[str] = Query(default=None),
):
    conn = get_db_connection()
    cur = None
    try:
        cur = conn.cursor(dictionary=True)

        where = []
        params = []

        if year is not None:
            where.append("YEAR(COALESCE(p.fecha_emision, DATE(p.created_at))) = %s")
            params.append(int(year))

        if month is not None:
            where.append("MONTH(COALESCE(p.fecha_emision, DATE(p.created_at))) = %s")
            params.append(int(month))

        if empresa:
            where.append("COALESCE(p.empresa,'') LIKE %s")
            params.append(f"%{empresa.strip()}%")

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        sql = f"""
            SELECT
                p.id,
                p.numero_factura,
                COALESCE(p.empresa, '') AS empresa,
                p.empresa_id,
                p.monto_total,
                DATE(COALESCE(p.fecha_emision, DATE(p.created_at))) AS fecha_base,
                COALESCE(e.modo_iva, '') AS modo_iva
            FROM pagos p
            LEFT JOIN empresas e ON e.id = p.empresa_id
            {where_sql}
            ORDER BY fecha_base DESC, COALESCE(p.empresa,''), p.numero_factura
        """
        cur.execute(sql, params)
        rows = cur.fetchall() or []

        month_names = {
            1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
            5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
            9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
        }

        def as_money_parts(total, modo_iva, iva_incl):
            total = float(total or 0)
            modo_iva = str(modo_iva or "").strip().upper()

            # En este servidor usamos solo modo_iva.
            # Si el modo es IVA_INCLUIDO, se separa desde el total.
            # En cualquier otro caso mantenemos el mismo criterio para no romper el histórico.
            if modo_iva == "IVA_INCLUIDO":
                iva = round(total * 19 / 119, 2)
                neto = round(total - iva, 2)
                bruto = total
            else:
                bruto = total
                iva = round(total * 19 / 119, 2)
                neto = round(total - iva, 2)

            return bruto, iva, neto

        data = {}
        years_set = set()
        empresas_set = set()

        total_general = 0.0
        iva_general = 0.0
        ganancia_general = 0.0
        documentos_general = 0

        for r in rows:
            fecha_base = r.get("fecha_base")
            if not fecha_base:
                continue

            y = int(fecha_base.year)
            m = int(fecha_base.month)
            empresa_nombre = (r.get("empresa") or "Sin empresa").strip() or "Sin empresa"

            years_set.add(y)
            empresas_set.add(empresa_nombre)

            bruto, iva, neto = as_money_parts(
                r.get("monto_total"),
                r.get("modo_iva"),
                0
            )

            total_general += bruto
            iva_general += iva
            ganancia_general += neto
            documentos_general += 1

            data.setdefault(y, {
                "year": y,
                "documentos": 0,
                "total": 0.0,
                "iva": 0.0,
                "ganancia": 0.0,
                "meses": {}
            })

            yref = data[y]
            yref["documentos"] += 1
            yref["total"] += bruto
            yref["iva"] += iva
            yref["ganancia"] += neto

            yref["meses"].setdefault(m, {
                "year": y,
                "month": m,
                "mes_nombre": month_names.get(m, "mes"),
                "documentos": 0,
                "total": 0.0,
                "iva": 0.0,
                "ganancia": 0.0,
                "empresas": {}
            })

            mref = yref["meses"][m]
            mref["documentos"] += 1
            mref["total"] += bruto
            mref["iva"] += iva
            mref["ganancia"] += neto

            mref["empresas"].setdefault(empresa_nombre, {
                "empresa": empresa_nombre,
                "documentos": 0,
                "total": 0.0,
                "iva": 0.0,
                "ganancia": 0.0,
                "items": []
            })

            eref = mref["empresas"][empresa_nombre]
            eref["documentos"] += 1
            eref["total"] += bruto
            eref["iva"] += iva
            eref["ganancia"] += neto
            eref["items"].append({
                "id": r.get("id"),
                "numero_factura": r.get("numero_factura"),
                "fecha": fecha_base.strftime("%d-%m-%Y"),
                "total": round(bruto, 2),
                "iva": round(iva, 2),
                "ganancia": round(neto, 2),
            })

        anios = []
        for y in sorted(data.keys(), reverse=True):
            yref = data[y]
            meses = []
            for m in sorted(yref["meses"].keys(), reverse=True):
                mref = yref["meses"][m]
                empresas_arr = []
                for empresa_nombre in sorted(mref["empresas"].keys()):
                    eref = mref["empresas"][empresa_nombre]
                    empresas_arr.append({
                        "empresa": eref["empresa"],
                        "documentos": eref["documentos"],
                        "total": round(eref["total"], 2),
                        "iva": round(eref["iva"], 2),
                        "ganancia": round(eref["ganancia"], 2),
                        "items": eref["items"],
                    })

                iva_by_metal = 0.0
                iva_total = round(mref["iva"], 2)
                iva_compra = 0.0
                iva_pagar = round(iva_total - iva_compra, 2)

                meses.append({
                    "year": y,
                    "month": m,
                    "mes_nombre": mref["mes_nombre"],
                    "documentos": mref["documentos"],
                    "total": round(mref["total"], 2),
                    "iva": round(mref["iva"], 2),
                    "ganancia": round(mref["ganancia"], 2),
                    "empresas": empresas_arr,
                    "cuadro_amarillo": {
                        "mes_nombre": mref["mes_nombre"],
                        "total": round(mref["total"], 2),
                        "iva": round(mref["iva"], 2),
                        "ganancia": round(mref["ganancia"], 2),
                        "iva_by_metal": round(iva_by_metal, 2),
                        "iva_total": round(iva_total, 2),
                        "iva_compra": round(iva_compra, 2),
                        "iva_pagar": round(iva_pagar, 2),
                    }
                })

            anios.append({
                "year": y,
                "documentos": yref["documentos"],
                "total": round(yref["total"], 2),
                "iva": round(yref["iva"], 2),
                "ganancia": round(yref["ganancia"], 2),
                "meses": meses,
            })

        return {
            "meta": {
                "documentos": documentos_general,
                "total_general": round(total_general, 2),
                "iva_general": round(iva_general, 2),
                "ganancia_general": round(ganancia_general, 2),
            },
            "filtros": {
                "years": sorted(list(years_set), reverse=True),
                "empresas": sorted(list(empresas_set)),
            },
            "anios": anios,
        }
    except mysql.connector.Error as e:
        raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
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


@router.get("/pagados")
def listar_pagados(limit: int = Query(default=1000, ge=1, le=5000)):
    return PagosService().fetch_pagos(
        estado=EstadoPagos.pagados,
        limit=limit,
    )



# ==========================================================
# /api/admin/pagos
# ==========================================================
@admin_router.get("/pendientes")
def admin_pendientes(
    empresa_id: Optional[int] = Query(default=None),
    empresa: Optional[str] = Query(default=None, description="Fallback por nombre"),
    limit: int = Query(default=1000, ge=1, le=5000),
):
    return PagosService().fetch_pagos(
        solo_pendientes_no_archivados=True,
        empresa_id=empresa_id,
        empresa_nombre=empresa,
        limit=limit,
    )


@admin_router.patch("/{pago_id}/marcar-pagado")
def admin_marcar_pagado(
    pago_id: int,
    payload: MarcarPagadoRequest = Body(default=MarcarPagadoRequest()),
):
    return PagosService().update_pagado(pago_id, payload.archivado)


@admin_router.patch("/{pago_id}/archivar")
def admin_archivar(
    pago_id: int,
    value: int = Query(default=1, ge=0, le=1),
):
    return PagosService().update_archivado(pago_id, value)


@admin_router.post("/sync-desde-cedibles")
def admin_sync_desde_cedibles(payload: SyncDesdeCediblesRequest = Body(default=SyncDesdeCediblesRequest())):
    return PagosService().sync_pagos_desde_cedibles(
        solo_pendientes=bool(payload.solo_pendientes),
        force_update=bool(payload.force_update),
        limit=int(payload.limit),
    )