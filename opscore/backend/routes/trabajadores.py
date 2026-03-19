# -*- coding: utf-8 -*-
# backend/routes/trabajadores.py
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Any, Dict, List, Set

import mysql.connector
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/trabajadores", tags=["trabajadores"])


# =============================================================================
# SINGLETON META
# =============================================================================
class SingletonMeta(type):
    _instances: Dict[type, object] = {}
    _lock = threading.Lock()

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with cls._lock:
                if cls not in cls._instances:
                    cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


# =============================================================================
# DB
# =============================================================================
@dataclass(frozen=True)
class DBConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


class TrabajadoresDBConfigFactory:
    @staticmethod
    def from_env() -> DBConfig:
        host = os.getenv("DB_HOST", "127.0.0.1")
        port = int(os.getenv("DB_PORT", "3306"))

        user = os.getenv("DB_USER")
        password = os.getenv("DB_PASSWORD")
        database = os.getenv("DB_NAME")

        missing = [k for k, v in {
            "DB_USER": user,
            "DB_PASSWORD": password,
            "DB_NAME": database,
        }.items() if not v]

        if missing:
            raise HTTPException(
                status_code=500,
                detail=f"Error conexion DB: faltan variables en .env -> {', '.join(missing)}",
            )

        return DBConfig(
            host=host,
            port=port,
            user=user or "",
            password=password or "",
            database=database or "",
        )


class TrabajadoresDBConnectionManager(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.config = TrabajadoresDBConfigFactory.from_env()

    def connect(self):
        try:
            return mysql.connector.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
            )
        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error conexion DB: {str(e)}")


def get_db_connection():
    return TrabajadoresDBConnectionManager().connect()


# =============================================================================
# CONTRACT REGISTRY
# =============================================================================
class TrabajadoresContractRegistry(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.estados_laborales_validos: Set[str] = {"ACTIVO", "INACTIVO"}
        self.tipos_vacaciones_validos: Set[str] = {"AJUSTE", "USO"}
        self.rol_trabajador = "TRABAJADOR"
        self.vacaciones_por_mes = 1.25
        self.vacaciones_por_dia = self.vacaciones_por_mes / 30.0


# =============================================================================
# SCHEMAS
# =============================================================================
class TrabajadorUpsert(BaseModel):
    usuario_id: int = Field(..., ge=1)
    fecha_ingreso: date
    cargo: Optional[str] = None
    area: Optional[str] = None
    estado: str = "ACTIVO"


class SueldoCreate(BaseModel):
    sueldo: int = Field(..., ge=0)
    fecha_inicio: date
    observacion: Optional[str] = None


class VacacionMovimientoCreate(BaseModel):
    tipo: str
    dias: float = Field(..., gt=0)
    fecha: date
    observacion: Optional[str] = None


# =============================================================================
# HELPERS / FACTORIES
# =============================================================================
class TrabajadoresHelperFactory:
    @staticmethod
    def ensure_enum_tipo_vac(tipo: str) -> str:
        t = (tipo or "").strip().upper()
        if t not in TrabajadoresContractRegistry().tipos_vacaciones_validos:
            raise HTTPException(status_code=400, detail="tipo debe ser 'AJUSTE' o 'USO'")
        return t

    @staticmethod
    def row_to_json_compatible(row: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k, v in (row or {}).items():
            if isinstance(v, (datetime, date)):
                out[k] = v.isoformat()
            else:
                out[k] = v
        return out

    @staticmethod
    def safe_float(v: Any) -> float:
        try:
            return float(v or 0)
        except Exception:
            return 0.0

    @staticmethod
    def vacaciones_generadas_desde_ingreso(
        fecha_ingreso: Optional[date],
        hoy: Optional[date] = None,
    ) -> float:
        if not fecha_ingreso:
            return 0.0
        hoy = hoy or date.today()
        if fecha_ingreso > hoy:
            return 0.0

        days = (hoy - fecha_ingreso).days
        generadas = days * TrabajadoresContractRegistry().vacaciones_por_dia
        return round(float(generadas), 2)

    @staticmethod
    def table_columns(conn, table_name: str) -> Set[str]:
        db_name = os.getenv("DB_NAME")
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                """,
                (db_name, table_name),
            )
            rows = cur.fetchall() or []
            return {r["COLUMN_NAME"] for r in rows if r.get("COLUMN_NAME")}
        finally:
            try:
                cur.close()
            except Exception:
                pass


class TrabajadoresResponseFactory:
    @staticmethod
    def ok(message: str, **extra) -> Dict[str, Any]:
        out = {"mensaje": message}
        out.update(extra)
        return out

    @staticmethod
    def datos(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"datos": rows}


# =============================================================================
# SERVICE
# =============================================================================
class TrabajadoresService(metaclass=SingletonMeta):
    def activar_usuario_si_aplica(self, conn, usuario_id: int, estado_laboral: str) -> Dict[str, Any]:
        cols = TrabajadoresHelperFactory.table_columns(conn, "usuarios")
        sets: List[str] = []
        params: List[Any] = []

        estado_laboral = (estado_laboral or "ACTIVO").upper()

        if "activo" in cols:
            sets.append("activo = %s")
            params.append(1 if estado_laboral == "ACTIVO" else 0)

        if estado_laboral == "ACTIVO" and "rol" in cols:
            sets.append("rol = %s")
            params.append(TrabajadoresContractRegistry().rol_trabajador)

        if not sets:
            return {"usuario_actualizado": False, "motivo": "tabla usuarios sin columnas activo/rol"}

        sql = f"UPDATE usuarios SET {', '.join(sets)} WHERE id = %s"
        params.append(usuario_id)

        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        return {"usuario_actualizado": True, "filas_afectadas": cur.rowcount}

    def calcular_saldo_vacaciones(self, conn, trabajador_id: int, fecha_ingreso: Optional[date]) -> Dict[str, float]:
        generadas = TrabajadoresHelperFactory.vacaciones_generadas_desde_ingreso(fecha_ingreso)

        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT
              (SELECT COALESCE(SUM(dias),0) FROM vacaciones_movimientos WHERE trabajador_id=%s AND tipo='AJUSTE') AS ajustes,
              (SELECT COALESCE(SUM(dias),0) FROM vacaciones_movimientos WHERE trabajador_id=%s AND tipo='USO') AS tomadas
            """,
            (trabajador_id, trabajador_id),
        )
        sums = cursor.fetchone() or {"ajustes": 0, "tomadas": 0}
        ajustes = TrabajadoresHelperFactory.safe_float(sums.get("ajustes"))
        tomadas = TrabajadoresHelperFactory.safe_float(sums.get("tomadas"))
        pendientes = round((generadas + ajustes - tomadas), 2)

        return {
            "generadas": generadas,
            "ajustes": round(ajustes, 2),
            "tomadas": round(tomadas, 2),
            "pendientes": pendientes,
        }

    # -----------------------------------------------------------------
    # Ficha laboral
    # -----------------------------------------------------------------
    def listar_trabajadores(
        self,
        *,
        q: Optional[str],
        estado: Optional[str],
        limit: int,
    ) -> Dict[str, Any]:
        q_norm = (q or "").strip()
        estado_norm = (estado or "").strip().upper() if estado else None

        conn = get_db_connection()
        try:
            cursor = conn.cursor(dictionary=True)

            sql = """
                SELECT
                  u.id AS usuario_id,
                  u.username,
                  u.rol,
                  u.nombre_completo,
                  u.archivo_url,

                  t.id AS trabajador_id,
                  t.fecha_ingreso,
                  t.cargo,
                  t.area,
                  t.estado,

                  (
                    SELECT ts.sueldo
                    FROM trabajador_sueldos ts
                    WHERE ts.trabajador_id = t.id
                    ORDER BY ts.fecha_inicio DESC, ts.id DESC
                    LIMIT 1
                  ) AS sueldo_actual,

                  (
                    SELECT COALESCE(SUM(vm.dias), 0)
                    FROM vacaciones_movimientos vm
                    WHERE vm.trabajador_id = t.id AND vm.tipo = 'USO'
                  ) AS vacaciones_tomadas,

                  (
                    SELECT COALESCE(SUM(vm.dias), 0)
                    FROM vacaciones_movimientos vm
                    WHERE vm.trabajador_id = t.id AND vm.tipo = 'AJUSTE'
                  ) AS vacaciones_ajustes

                FROM usuarios u
                LEFT JOIN trabajadores t ON t.usuario_id = u.id
                WHERE 1=1
            """

            params: List[Any] = []

            if q_norm:
                like = f"%{q_norm}%"
                sql += """
                  AND (
                    u.username LIKE %s OR
                    u.rol LIKE %s OR
                    u.nombre_completo LIKE %s
                  )
                """
                params.extend([like, like, like])

            if estado_norm:
                sql += " AND t.estado = %s "
                params.append(estado_norm)

            sql += " ORDER BY u.id DESC LIMIT %s "
            params.append(limit)

            cursor.execute(sql, params)
            rows = cursor.fetchall() or []

            out_rows: List[Dict[str, Any]] = []
            for r in rows:
                rr = TrabajadoresHelperFactory.row_to_json_compatible(r)

                trabajador_id = rr.get("trabajador_id")
                fecha_ingreso = None
                try:
                    if rr.get("fecha_ingreso"):
                        fecha_ingreso = date.fromisoformat(str(rr["fecha_ingreso"])[:10])
                except Exception:
                    fecha_ingreso = None

                if trabajador_id:
                    generadas = TrabajadoresHelperFactory.vacaciones_generadas_desde_ingreso(fecha_ingreso)
                    ajustes = TrabajadoresHelperFactory.safe_float(rr.get("vacaciones_ajustes"))
                    tomadas = TrabajadoresHelperFactory.safe_float(rr.get("vacaciones_tomadas"))
                    pendientes = round((generadas + ajustes - tomadas), 2)

                    rr["vacaciones_generadas"] = generadas
                    rr["vacaciones_ajustes"] = round(ajustes, 2)
                    rr["vacaciones_tomadas"] = round(tomadas, 2)
                    rr["vacaciones_pendientes"] = pendientes
                else:
                    rr["vacaciones_generadas"] = 0.0
                    rr["vacaciones_ajustes"] = 0.0
                    rr["vacaciones_tomadas"] = 0.0
                    rr["vacaciones_pendientes"] = 0.0

                out_rows.append(rr)

            return TrabajadoresResponseFactory.datos(out_rows)

        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error DB listar_trabajadores: {str(e)}")
        finally:
            conn.close()

    def obtener_trabajador(self, trabajador_id: int) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT
                  t.id AS trabajador_id,
                  t.usuario_id,
                  t.fecha_ingreso,
                  t.cargo,
                  t.area,
                  t.estado,
                  u.username,
                  u.rol,
                  u.nombre_completo,
                  u.archivo_url
                FROM trabajadores t
                JOIN usuarios u ON u.id = t.usuario_id
                WHERE t.id = %s
                LIMIT 1
                """,
                (trabajador_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Trabajador no encontrado")

            rr = TrabajadoresHelperFactory.row_to_json_compatible(row)
            fecha_ingreso = None
            try:
                if rr.get("fecha_ingreso"):
                    fecha_ingreso = date.fromisoformat(str(rr["fecha_ingreso"])[:10])
            except Exception:
                fecha_ingreso = None

            rr["vacaciones_generadas"] = TrabajadoresHelperFactory.vacaciones_generadas_desde_ingreso(fecha_ingreso)
            return rr

        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error DB obtener_trabajador: {str(e)}")
        finally:
            conn.close()

    def upsert_ficha_trabajador(self, payload: TrabajadorUpsert) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            cur = conn.cursor(dictionary=True)

            cur.execute("SELECT id FROM usuarios WHERE id = %s LIMIT 1", (payload.usuario_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Usuario no existe")

            estado_norm = (payload.estado or "ACTIVO").upper()
            if estado_norm not in TrabajadoresContractRegistry().estados_laborales_validos:
                raise HTTPException(status_code=400, detail="estado debe ser ACTIVO o INACTIVO")

            cur.execute("SELECT id FROM trabajadores WHERE usuario_id = %s LIMIT 1", (payload.usuario_id,))
            existing = cur.fetchone()

            usuario_update_info = self.activar_usuario_si_aplica(conn, payload.usuario_id, estado_norm)

            if existing:
                trabajador_id = existing["id"]
                cur2 = conn.cursor()
                cur2.execute(
                    """
                    UPDATE trabajadores
                    SET fecha_ingreso=%s, cargo=%s, area=%s, estado=%s
                    WHERE id=%s
                    """,
                    (
                        payload.fecha_ingreso,
                        payload.cargo,
                        payload.area,
                        estado_norm,
                        trabajador_id,
                    ),
                )
                conn.commit()
                return TrabajadoresResponseFactory.ok(
                    "Ficha actualizada",
                    trabajador_id=trabajador_id,
                    usuario=usuario_update_info,
                )

            cur2 = conn.cursor()
            cur2.execute(
                """
                INSERT INTO trabajadores (usuario_id, fecha_ingreso, cargo, area, estado)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    payload.usuario_id,
                    payload.fecha_ingreso,
                    payload.cargo,
                    payload.area,
                    estado_norm,
                ),
            )
            conn.commit()
            return TrabajadoresResponseFactory.ok(
                "Ficha creada",
                trabajador_id=cur2.lastrowid,
                usuario=usuario_update_info,
            )

        except HTTPException:
            raise
        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error DB upsert_ficha_trabajador: {str(e)}")
        finally:
            conn.close()

    # -----------------------------------------------------------------
    # Sueldos
    # -----------------------------------------------------------------
    def listar_sueldos(self, trabajador_id: int, limit: int) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT id FROM trabajadores WHERE id=%s LIMIT 1", (trabajador_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Trabajador no encontrado")

            cursor.execute(
                """
                SELECT id, trabajador_id, sueldo, fecha_inicio, observacion, created_at
                FROM trabajador_sueldos
                WHERE trabajador_id = %s
                ORDER BY fecha_inicio DESC, id DESC
                LIMIT %s
                """,
                (trabajador_id, limit),
            )
            rows = cursor.fetchall() or []
            return TrabajadoresResponseFactory.datos(
                [TrabajadoresHelperFactory.row_to_json_compatible(r) for r in rows]
            )

        except HTTPException:
            raise
        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error DB listar_sueldos: {str(e)}")
        finally:
            conn.close()

    def crear_sueldo(self, trabajador_id: int, payload: SueldoCreate) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT id FROM trabajadores WHERE id=%s LIMIT 1", (trabajador_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Trabajador no encontrado")

            cur2 = conn.cursor()
            cur2.execute(
                """
                INSERT INTO trabajador_sueldos (trabajador_id, sueldo, fecha_inicio, observacion)
                VALUES (%s, %s, %s, %s)
                """,
                (trabajador_id, payload.sueldo, payload.fecha_inicio, payload.observacion),
            )
            conn.commit()
            return TrabajadoresResponseFactory.ok("Sueldo registrado", id=cur2.lastrowid)

        except HTTPException:
            raise
        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error DB crear_sueldo: {str(e)}")
        finally:
            conn.close()

    def sueldo_actual(self, trabajador_id: int) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT id FROM trabajadores WHERE id=%s LIMIT 1", (trabajador_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Trabajador no encontrado")

            cursor.execute(
                """
                SELECT sueldo, fecha_inicio, observacion, created_at
                FROM trabajador_sueldos
                WHERE trabajador_id = %s
                ORDER BY fecha_inicio DESC, id DESC
                LIMIT 1
                """,
                (trabajador_id,),
            )
            row = cursor.fetchone()
            return {
                "trabajador_id": trabajador_id,
                "sueldo_actual": TrabajadoresHelperFactory.row_to_json_compatible(row) if row else None,
            }

        except HTTPException:
            raise
        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error DB sueldo_actual: {str(e)}")
        finally:
            conn.close()

    # -----------------------------------------------------------------
    # Vacaciones
    # -----------------------------------------------------------------
    def listar_vacaciones(self, trabajador_id: int, limit: int) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT id FROM trabajadores WHERE id=%s LIMIT 1", (trabajador_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Trabajador no encontrado")

            cursor.execute(
                """
                SELECT id, trabajador_id, tipo, dias, fecha, observacion, created_at
                FROM vacaciones_movimientos
                WHERE trabajador_id = %s
                ORDER BY fecha DESC, id DESC
                LIMIT %s
                """,
                (trabajador_id, limit),
            )
            rows = cursor.fetchall() or []
            return TrabajadoresResponseFactory.datos(
                [TrabajadoresHelperFactory.row_to_json_compatible(r) for r in rows]
            )

        except HTTPException:
            raise
        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error DB listar_vacaciones: {str(e)}")
        finally:
            conn.close()

    def crear_movimiento_vacaciones(self, trabajador_id: int, payload: VacacionMovimientoCreate) -> Dict[str, Any]:
        tipo = TrabajadoresHelperFactory.ensure_enum_tipo_vac(payload.tipo)

        conn = get_db_connection()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT id, fecha_ingreso FROM trabajadores WHERE id=%s LIMIT 1", (trabajador_id,))
            tr = cursor.fetchone()
            if not tr:
                raise HTTPException(status_code=404, detail="Trabajador no encontrado")

            fecha_ingreso = tr.get("fecha_ingreso") if isinstance(tr.get("fecha_ingreso"), date) else None
            saldo = self.calcular_saldo_vacaciones(conn, trabajador_id, fecha_ingreso)

            if tipo == "USO":
                if payload.dias > saldo["pendientes"]:
                    raise HTTPException(
                        status_code=400,
                        detail=f"No puedes registrar USO por {payload.dias} dias. Pendientes: {saldo['pendientes']}",
                    )

            cur2 = conn.cursor()
            cur2.execute(
                """
                INSERT INTO vacaciones_movimientos (trabajador_id, tipo, dias, fecha, observacion)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (trabajador_id, tipo, payload.dias, payload.fecha, payload.observacion),
            )
            conn.commit()
            return TrabajadoresResponseFactory.ok("Movimiento registrado", id=cur2.lastrowid)

        except HTTPException:
            raise
        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error DB crear_movimiento_vacaciones: {str(e)}")
        finally:
            conn.close()

    def resumen_vacaciones(self, trabajador_id: int) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT id, fecha_ingreso FROM trabajadores WHERE id=%s LIMIT 1", (trabajador_id,))
            tr = cursor.fetchone()
            if not tr:
                raise HTTPException(status_code=404, detail="Trabajador no encontrado")

            fecha_ingreso = tr.get("fecha_ingreso") if isinstance(tr.get("fecha_ingreso"), date) else None
            saldo = self.calcular_saldo_vacaciones(conn, trabajador_id, fecha_ingreso)

            return {
                "trabajador_id": trabajador_id,
                "generadas": saldo["generadas"],
                "ajustes": saldo["ajustes"],
                "tomadas": saldo["tomadas"],
                "pendientes": saldo["pendientes"],
            }

        except HTTPException:
            raise
        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error DB resumen_vacaciones: {str(e)}")
        finally:
            conn.close()


# =============================================================================
# ENDPOINTS
# =============================================================================
@router.get("/health")
def health():
    return {"status": "ok", "router": "trabajadores"}

@router.get("")
def listar_trabajadores(
    q: Optional[str] = Query(default=None, description="Busqueda por nombre, username o rol"),
    estado: Optional[str] = Query(default=None, description="ACTIVO/INACTIVO"),
    limit: int = Query(default=200, ge=1, le=2000),
):
    return TrabajadoresService().listar_trabajadores(q=q, estado=estado, limit=limit)


@router.get("/{trabajador_id}")
def obtener_trabajador(trabajador_id: int):
    return TrabajadoresService().obtener_trabajador(trabajador_id)


@router.post("")
def upsert_ficha_trabajador(payload: TrabajadorUpsert):
    return TrabajadoresService().upsert_ficha_trabajador(payload)


@router.get("/{trabajador_id}/sueldos")
def listar_sueldos(trabajador_id: int, limit: int = Query(default=200, ge=1, le=2000)):
    return TrabajadoresService().listar_sueldos(trabajador_id, limit)


@router.post("/{trabajador_id}/sueldos")
def crear_sueldo(trabajador_id: int, payload: SueldoCreate):
    return TrabajadoresService().crear_sueldo(trabajador_id, payload)


@router.get("/{trabajador_id}/sueldos/actual")
def sueldo_actual(trabajador_id: int):
    return TrabajadoresService().sueldo_actual(trabajador_id)


@router.get("/{trabajador_id}/vacaciones")
def listar_vacaciones(trabajador_id: int, limit: int = Query(default=500, ge=1, le=5000)):
    return TrabajadoresService().listar_vacaciones(trabajador_id, limit)


@router.post("/{trabajador_id}/vacaciones")
def crear_movimiento_vacaciones(trabajador_id: int, payload: VacacionMovimientoCreate):
    return TrabajadoresService().crear_movimiento_vacaciones(trabajador_id, payload)


@router.get("/{trabajador_id}/vacaciones/resumen")
def resumen_vacaciones(trabajador_id: int):
    return TrabajadoresService().resumen_vacaciones(trabajador_id)


