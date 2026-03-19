# -*- coding: utf-8 -*-
# backend/routes/rrhh.py
from __future__ import annotations

import os
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Any, Dict, List, Set

import mysql.connector
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/rrhh", tags=["rrhh"])


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


class RRHHDBConfigFactory:
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


class RRHHDBConnectionManager(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.config = RRHHDBConfigFactory.from_env()

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
    return RRHHDBConnectionManager().connect()


# =============================================================================
# CONTRACT REGISTRY
# =============================================================================
class RRHHContractRegistry(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.estados_persona_validos: Set[str] = {"ACTIVA", "INACTIVA"}
        self.categorias_equivalentes_trabajador = {"TRABAJADOR", "OPERARIO", "MAESTRANZA"}
        self.rol_usuario_trabajador = "Trabajador"


# =============================================================================
# HELPERS / FACTORIES
# =============================================================================
class RRHHHelperFactory:
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
    def rows_to_json(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [RRHHHelperFactory.row_to_json_compatible(r) for r in (rows or [])]

    @staticmethod
    def normalize_estado_persona(estado: Optional[str]) -> Optional[str]:
        if estado is None:
            return None
        e = (estado or "").strip().upper()
        if e == "":
            return ""
        if e not in RRHHContractRegistry().estados_persona_validos:
            raise HTTPException(status_code=400, detail="estado debe ser ACTIVA o INACTIVA")
        return e

    @staticmethod
    def slug(s: str) -> str:
        s = (s or "").strip().lower()
        s = re.sub(r"[^a-z0-9]+", "", s)
        return s

    @staticmethod
    def build_username(nombres: str, apellidos: str) -> str:
        n = (nombres or "").strip().split()
        a = (apellidos or "").strip().split()
        first = n[0][0] if n and n[0] else "u"
        last = a[0] if a and a[0] else "user"
        base = RRHHHelperFactory.slug(f"{first}{last}")
        return base if base else "user"

    @staticmethod
    def column_exists(cur, table: str, column: str) -> bool:
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
        try:
            return int(row.get("c", 0)) > 0
        except Exception:
            return False


class RRHHResponseFactory:
    @staticmethod
    def ok(**data) -> Dict[str, Any]:
        out = {"ok": True}
        out.update(data)
        return out


# =============================================================================
# SCHEMAS
# =============================================================================
class CategoriaCreate(BaseModel):
    nombre: str = Field(..., min_length=2, max_length=50)


class CategoriaPatch(BaseModel):
    nombre: Optional[str] = Field(default=None, min_length=2, max_length=50)
    activa: Optional[int] = Field(default=None, ge=0, le=1)


class PersonaCreate(BaseModel):
    categoria_id: int = Field(..., ge=1)
    nombres: str = Field(..., min_length=2, max_length=100)
    apellidos: str = Field(..., min_length=2, max_length=100)
    rut: str = Field(..., min_length=3, max_length=20)
    fecha_nacimiento: Optional[date] = None


class PersonaUpdate(BaseModel):
    categoria_id: int = Field(..., ge=1)
    nombres: str = Field(..., min_length=2, max_length=100)
    apellidos: str = Field(..., min_length=2, max_length=100)
    rut: str = Field(..., min_length=3, max_length=20)
    fecha_nacimiento: Optional[date] = None


class PersonaEstadoPatch(BaseModel):
    estado: str


# =============================================================================
# SERVICE
# =============================================================================
class RRHHService(metaclass=SingletonMeta):
    def ensure_unique_username(self, conn, base_username: str) -> str:
        cur = conn.cursor(dictionary=True)
        u = base_username
        i = 1
        while True:
            cur.execute("SELECT id FROM usuarios WHERE username=%s LIMIT 1", (u,))
            if not cur.fetchone():
                return u
            i += 1
            u = f"{base_username}{i}"

    def categoria_es_trabajador(self, conn, categoria_id: int) -> bool:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT nombre FROM rrhh_categorias WHERE id=%s LIMIT 1", (categoria_id,))
        row = cur.fetchone()
        if not row:
            return False
        nombre = (row.get("nombre") or "").strip().upper()

        if nombre in RRHHContractRegistry().categorias_equivalentes_trabajador:
            return True
        return "TRABAJADOR" in nombre

    def crear_o_activar_usuario_trabajador(self, conn, nombres: str, apellidos: str) -> Dict[str, Any]:
        base = RRHHHelperFactory.build_username(nombres, apellidos)
        username = self.ensure_unique_username(conn, base)

        temp_password = secrets.token_urlsafe(8)
        password_to_store = temp_password

        cur = conn.cursor(dictionary=True)

        cur.execute("SELECT id FROM usuarios WHERE username=%s LIMIT 1", (username,))
        existing = cur.fetchone()
        if existing:
            user_id = existing["id"]
            cur2 = conn.cursor()
            if RRHHHelperFactory.column_exists(cur, "usuarios", "activo"):
                cur2.execute(
                    "UPDATE usuarios SET activo=1, rol=%s WHERE id=%s",
                    (RRHHContractRegistry().rol_usuario_trabajador, user_id),
                )
            else:
                cur2.execute(
                    "UPDATE usuarios SET rol=%s WHERE id=%s",
                    (RRHHContractRegistry().rol_usuario_trabajador, user_id),
                )
            return {
                "accion": "activado",
                "usuario_id": int(user_id),
                "username": username,
                "password_temporal": None,
            }

        cur2 = conn.cursor()
        if RRHHHelperFactory.column_exists(cur, "usuarios", "activo") and RRHHHelperFactory.column_exists(cur, "usuarios", "must_change_password"):
            cur2.execute(
                """
                INSERT INTO usuarios (username, password, rol, nombre_completo, activo, must_change_password)
                VALUES (%s, %s, %s, %s, 1, 1)
                """,
                (
                    username,
                    password_to_store,
                    RRHHContractRegistry().rol_usuario_trabajador,
                    f"{nombres} {apellidos}".strip(),
                ),
            )
        else:
            cur2.execute(
                """
                INSERT INTO usuarios (username, password, rol, nombre_completo)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    username,
                    password_to_store,
                    RRHHContractRegistry().rol_usuario_trabajador,
                    f"{nombres} {apellidos}".strip(),
                ),
            )

        user_id = cur2.lastrowid

        return {
            "accion": "creado",
            "usuario_id": int(user_id),
            "username": username,
            "password_temporal": temp_password,
        }

    # -----------------------------------------------------------------
    # Categorias
    # -----------------------------------------------------------------
    def listar_categorias(self) -> List[Dict[str, Any]]:
        conn = get_db_connection()
        try:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                """
                SELECT id, nombre, activa, created_at
                FROM rrhh_categorias
                ORDER BY nombre ASC
                """
            )
            rows = cur.fetchall() or []
            return RRHHHelperFactory.rows_to_json(rows)
        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error DB listar_categorias: {str(e)}")
        finally:
            conn.close()

    def crear_categoria(self, payload: CategoriaCreate) -> Dict[str, Any]:
        nombre = (payload.nombre or "").strip()
        if not nombre:
            raise HTTPException(status_code=400, detail="nombre es requerido")

        conn = get_db_connection()
        try:
            cur = conn.cursor()

            cur.execute("SELECT id FROM rrhh_categorias WHERE nombre=%s LIMIT 1", (nombre,))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="Ya existe una categoria con ese nombre")

            cur.execute(
                "INSERT INTO rrhh_categorias (nombre, activa) VALUES (%s, 1)",
                (nombre,),
            )
            conn.commit()
            return RRHHResponseFactory.ok(id=cur.lastrowid)
        except mysql.connector.Error as e:
            if "Duplicate" in str(e) or "duplicate" in str(e):
                raise HTTPException(status_code=409, detail="Ya existe una categoria con ese nombre")
            raise HTTPException(status_code=500, detail=f"Error DB crear_categoria: {str(e)}")
        finally:
            conn.close()

    def patch_categoria(self, categoria_id: int, payload: CategoriaPatch) -> Dict[str, Any]:
        if categoria_id <= 0:
            raise HTTPException(status_code=400, detail="categoria_id invalido")

        patch_nombre = (payload.nombre.strip() if payload.nombre is not None else None)
        patch_activa = payload.activa

        if patch_nombre is None and patch_activa is None:
            raise HTTPException(status_code=400, detail="Nada para actualizar")

        conn = get_db_connection()
        try:
            cur = conn.cursor(dictionary=True)

            cur.execute("SELECT id, nombre, activa FROM rrhh_categorias WHERE id=%s LIMIT 1", (categoria_id,))
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Categoria no existe")

            if patch_nombre is not None:
                if patch_nombre == "":
                    raise HTTPException(status_code=400, detail="nombre no puede estar vacio")

                cur.execute(
                    "SELECT id FROM rrhh_categorias WHERE nombre=%s AND id<>%s LIMIT 1",
                    (patch_nombre, categoria_id),
                )
                if cur.fetchone():
                    raise HTTPException(status_code=409, detail="Ya existe una categoria con ese nombre")

            sets = []
            params: List[Any] = []

            if patch_nombre is not None:
                sets.append("nombre=%s")
                params.append(patch_nombre)

            if patch_activa is not None:
                sets.append("activa=%s")
                params.append(int(patch_activa))

            params.append(categoria_id)

            sql = f"UPDATE rrhh_categorias SET {', '.join(sets)} WHERE id=%s"
            cur2 = conn.cursor()
            cur2.execute(sql, tuple(params))
            conn.commit()
            return RRHHResponseFactory.ok()
        except mysql.connector.Error as e:
            if "Duplicate" in str(e) or "duplicate" in str(e):
                raise HTTPException(status_code=409, detail="Ya existe una categoria con ese nombre")
            raise HTTPException(status_code=500, detail=f"Error DB patch_categoria: {str(e)}")
        finally:
            conn.close()

    # -----------------------------------------------------------------
    # Personas
    # -----------------------------------------------------------------
    def listar_personas(
        self,
        *,
        q: Optional[str],
        estado: Optional[str],
        categoria_id: Optional[int],
        limit: int,
    ) -> List[Dict[str, Any]]:
        q_norm = (q or "").strip()
        estado_norm = RRHHHelperFactory.normalize_estado_persona(estado)

        conn = get_db_connection()
        try:
            cur = conn.cursor(dictionary=True)

            sql = """
                SELECT
                  p.id,
                  p.categoria_id,
                  p.nombres,
                  p.apellidos,
                  p.rut,
                  p.fecha_nacimiento,
                  p.estado,
                  p.created_at,
                  p.updated_at
                FROM rrhh_personas p
                WHERE 1=1
            """
            params: List[Any] = []

            if q_norm:
                like = f"%{q_norm}%"
                sql += " AND (p.nombres LIKE %s OR p.apellidos LIKE %s OR p.rut LIKE %s) "
                params.extend([like, like, like])

            if estado_norm in {"ACTIVA", "INACTIVA"}:
                sql += " AND p.estado = %s "
                params.append(estado_norm)

            if categoria_id:
                sql += " AND p.categoria_id = %s "
                params.append(categoria_id)

            sql += " ORDER BY p.apellidos ASC, p.nombres ASC "
            sql += " LIMIT %s "
            params.append(limit)

            cur.execute(sql, tuple(params))
            rows = cur.fetchall() or []
            return RRHHHelperFactory.rows_to_json(rows)
        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error DB listar_personas: {str(e)}")
        finally:
            conn.close()

    def crear_persona(self, payload: PersonaCreate) -> Dict[str, Any]:
        nombres = (payload.nombres or "").strip()
        apellidos = (payload.apellidos or "").strip()
        rut = (payload.rut or "").strip()

        if not nombres or not apellidos or not rut:
            raise HTTPException(status_code=400, detail="nombres, apellidos y rut son requeridos")

        conn = get_db_connection()
        try:
            cur = conn.cursor(dictionary=True)

            cur.execute("SELECT id FROM rrhh_categorias WHERE id=%s LIMIT 1", (payload.categoria_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Categoria no existe")

            cur.execute("SELECT id FROM rrhh_personas WHERE rut=%s LIMIT 1", (rut,))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="Ya existe una persona con ese RUT")

            cur2 = conn.cursor()
            cur2.execute(
                """
                INSERT INTO rrhh_personas
                  (categoria_id, nombres, apellidos, rut, fecha_nacimiento, estado)
                VALUES
                  (%s, %s, %s, %s, %s, 'ACTIVA')
                """,
                (payload.categoria_id, nombres, apellidos, rut, payload.fecha_nacimiento),
            )
            persona_id = cur2.lastrowid

            user_info = None
            if self.categoria_es_trabajador(conn, payload.categoria_id):
                user_info = self.crear_o_activar_usuario_trabajador(conn, nombres, apellidos)

            conn.commit()
            return RRHHResponseFactory.ok(id=persona_id, usuario=user_info)

        except mysql.connector.Error as e:
            if "Duplicate" in str(e) or "duplicate" in str(e):
                raise HTTPException(status_code=409, detail="Ya existe una persona con ese RUT")
            raise HTTPException(status_code=500, detail=f"Error DB crear_persona: {str(e)}")
        finally:
            conn.close()

    def actualizar_persona(self, persona_id: int, payload: PersonaUpdate) -> Dict[str, Any]:
        if persona_id <= 0:
            raise HTTPException(status_code=400, detail="persona_id invalido")

        nombres = (payload.nombres or "").strip()
        apellidos = (payload.apellidos or "").strip()
        rut = (payload.rut or "").strip()

        if not nombres or not apellidos or not rut:
            raise HTTPException(status_code=400, detail="nombres, apellidos y rut son requeridos")

        conn = get_db_connection()
        try:
            cur = conn.cursor(dictionary=True)

            cur.execute("SELECT id FROM rrhh_personas WHERE id=%s LIMIT 1", (persona_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Persona no existe")

            cur.execute("SELECT id FROM rrhh_categorias WHERE id=%s LIMIT 1", (payload.categoria_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Categoria no existe")

            cur.execute(
                "SELECT id FROM rrhh_personas WHERE rut=%s AND id<>%s LIMIT 1",
                (rut, persona_id),
            )
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="Ya existe otra persona con ese RUT")

            cur2 = conn.cursor()
            cur2.execute(
                """
                UPDATE rrhh_personas
                SET categoria_id=%s,
                    nombres=%s,
                    apellidos=%s,
                    rut=%s,
                    fecha_nacimiento=%s
                WHERE id=%s
                """,
                (payload.categoria_id, nombres, apellidos, rut, payload.fecha_nacimiento, persona_id),
            )
            conn.commit()
            return RRHHResponseFactory.ok()
        except mysql.connector.Error as e:
            if "Duplicate" in str(e) or "duplicate" in str(e):
                raise HTTPException(status_code=409, detail="Ya existe una persona con ese RUT")
            raise HTTPException(status_code=500, detail=f"Error DB actualizar_persona: {str(e)}")
        finally:
            conn.close()

    def patch_estado_persona(self, persona_id: int, payload: PersonaEstadoPatch) -> Dict[str, Any]:
        if persona_id <= 0:
            raise HTTPException(status_code=400, detail="persona_id invalido")

        estado = (payload.estado or "").strip().upper()
        if estado not in RRHHContractRegistry().estados_persona_validos:
            raise HTTPException(status_code=400, detail="estado debe ser ACTIVA o INACTIVA")

        conn = get_db_connection()
        try:
            cur = conn.cursor(dictionary=True)

            cur.execute("SELECT id FROM rrhh_personas WHERE id=%s LIMIT 1", (persona_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Persona no existe")

            cur2 = conn.cursor()
            cur2.execute(
                "UPDATE rrhh_personas SET estado=%s WHERE id=%s",
                (estado, persona_id),
            )
            conn.commit()
            return RRHHResponseFactory.ok()
        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error DB patch_estado_persona: {str(e)}")
        finally:
            conn.close()



# __BYMETAL_RRHH_CATEGORIA_SYNC_BACKEND_V2__
def _categoria_nombre(cur, categoria_id):
    cur.execute(
        "SELECT nombre FROM rrhh_categorias WHERE id=%s LIMIT 1",
        (categoria_id,)
    )
    row = cur.fetchone()
    if not row:
        return ""

    if isinstance(row, dict):
        return str(row.get("nombre") or "").strip()

    return str(row[0] or "").strip()

def _categoria_es_laboral(nombre):
    n = str(nombre or "").strip().upper()
    return n in ("TRABAJADOR", "GERENTE")

def _sync_trabajador_por_categoria(conn, persona_id, categoria_id):
    cur = conn.cursor(dictionary=True)
    try:
        nombre_cat = _categoria_nombre(cur, categoria_id)

        if not _categoria_es_laboral(nombre_cat):
            return {
                "categoria": nombre_cat,
                "sync_trabajador": False,
                "trabajador_creado": False
            }

        cur.execute(
            "SELECT id FROM trabajadores WHERE persona_id=%s LIMIT 1",
            (persona_id,)
        )
        row = cur.fetchone()
        if row:
            return {
                "categoria": nombre_cat,
                "sync_trabajador": True,
                "trabajador_creado": False
            }

        cargo_default = "gerente" if nombre_cat.upper() == "GERENTE" else ""
        area_default = "gerencia" if nombre_cat.upper() == "GERENTE" else ""

        cur2 = conn.cursor()
        cur2.execute(
            """
            INSERT INTO trabajadores
                (persona_id, usuario_id, fecha_ingreso, cargo, area, estado)
            VALUES
                (%s, %s, CURDATE(), %s, %s, %s)
            """,
            (persona_id, None, cargo_default, area_default, "ACTIVO")
        )

        return {
            "categoria": nombre_cat,
            "sync_trabajador": True,
            "trabajador_creado": True
        }
    finally:
        cur.close()


# =============================================================================
# ENDPOINTS
# =============================================================================
@router.get("/categorias")
def listar_categorias():
    return RRHHService().listar_categorias()


@router.post("/categorias")
def crear_categoria(payload: CategoriaCreate):
    return RRHHService().crear_categoria(payload)


@router.patch("/categorias/{categoria_id}")
def patch_categoria(categoria_id: int, payload: CategoriaPatch):
    return RRHHService().patch_categoria(categoria_id, payload)


@router.get("/personas")
def listar_personas(
    q: Optional[str] = Query(default=None, description="Buscar por nombres/apellidos/rut"),
    estado: Optional[str] = Query(default="ACTIVA", description="ACTIVA/INACTIVA o vacio para todas"),
    categoria_id: Optional[int] = Query(default=None, ge=1),
    limit: int = Query(default=500, ge=1, le=2000),
):
    return RRHHService().listar_personas(
        q=q,
        estado=estado,
        categoria_id=categoria_id,
        limit=limit,
    )


@router.post("/personas")
def crear_persona(payload: PersonaCreate):
    result = RRHHService().crear_persona(payload)

    persona_id = None
    if isinstance(result, dict):
        persona_id = result.get("id") or result.get("persona_id")

    if not persona_id:
        return result

    conn = get_db_connection()
    try:
        sync_info = _sync_trabajador_por_categoria(conn, persona_id, payload.categoria_id)
        conn.commit()

        if isinstance(result, dict):
            result["categoria_sync"] = sync_info

        return result
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error sync trabajador post-crear: {str(e)}")
    finally:
        conn.close()


@router.put("/personas/{persona_id}")
def actualizar_persona(persona_id: int, payload: PersonaUpdate):
    result = RRHHService().actualizar_persona(persona_id, payload)

    conn = get_db_connection()
    try:
        sync_info = _sync_trabajador_por_categoria(conn, persona_id, payload.categoria_id)
        conn.commit()

        if isinstance(result, dict):
            result["categoria_sync"] = sync_info

        return result
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error sync trabajador post-update: {str(e)}")
    finally:
        conn.close()


@router.patch("/personas/{persona_id}/estado")
def patch_estado_persona(persona_id: int, payload: PersonaEstadoPatch):
    return RRHHService().patch_estado_persona(persona_id, payload)


@router.get("/health")
def health():
    return {"status": "ok", "router": "rrhh"}