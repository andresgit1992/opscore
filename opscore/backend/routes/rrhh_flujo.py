# backend/routes/rrhh_flujo.py
from __future__ import annotations

import os
import hashlib
import secrets
from datetime import date
from typing import Optional

import pymysql
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, validator

router = APIRouter(prefix="/api/rrhh", tags=["rrhh-flujo"])


# =========================================================
# DB
# =========================================================
def get_conn():
    return pymysql.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", ""),
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


# =========================================================
# Password hash compatible con formato actual pbkdf2_sha256$210000$...
# =========================================================
def make_password_hash(raw_password: str, iterations: int = 210000) -> str:
    if not raw_password:
        raise ValueError("password vacío")
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        raw_password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


# =========================================================
# Schemas
# =========================================================
class AltaIntegralPayload(BaseModel):
    categoria_id: int = Field(..., ge=1)
    nombres: str = Field(..., min_length=1, max_length=100)
    apellidos: str = Field(..., min_length=1, max_length=100)
    rut: str = Field(..., min_length=1, max_length=20)
    fecha_nacimiento: Optional[date] = None
    estado_persona: str = Field(default="ACTIVA", max_length=20)

    crear_trabajador: bool = False
    fecha_ingreso: Optional[date] = None
    cargo: Optional[str] = Field(default=None, max_length=100)
    area: Optional[str] = Field(default=None, max_length=100)
    estado_trabajador: str = Field(default="ACTIVO", max_length=20)

    crear_acceso: bool = False
    username: Optional[str] = Field(default=None, max_length=50)
    password_temporal: Optional[str] = Field(default=None, max_length=255)
    rol: Optional[str] = Field(default=None, max_length=20)
    nombre_completo_usuario: Optional[str] = Field(default=None, max_length=100)
    activo: bool = True
    must_change_password: bool = True
    empresa_id: Optional[int] = None
    archivo_url: Optional[str] = Field(default=None, max_length=255)

    @validator("nombres", "apellidos", "rut", pre=True)
    def clean_basic(cls, v):
        if v is None:
            return v
        return str(v).strip()

    @validator("username", "rol", "cargo", "area", "nombre_completo_usuario", "archivo_url", pre=True)
    def clean_optional(cls, v):
        if v is None:
            return v
        v = str(v).strip()
        return v or None


# =========================================================
# Health
# =========================================================
@router.get("/flujo-health")
def flujo_health():
    return {"status": "ok", "router": "rrhh-flujo"}


# =========================================================
# Alta integral
# =========================================================
@router.post("/personas/alta-integral")
def alta_integral(payload: AltaIntegralPayload):
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # -------------------------------------------------
            # Validaciones base
            # -------------------------------------------------
            cur.execute(
                """
                SELECT id, rut
                FROM rrhh_personas
                WHERE rut = %s
                LIMIT 1
                """,
                (payload.rut,),
            )
            dup_persona = cur.fetchone()
            if dup_persona:
                raise HTTPException(
                    status_code=409,
                    detail=f"Ya existe rrhh_personas.id={dup_persona['id']} con rut={dup_persona['rut']}",
                )

            usuario_id = None
            trabajador_id = None

            # -------------------------------------------------
            # Insert persona
            # -------------------------------------------------
            cur.execute(
                """
                INSERT INTO rrhh_personas (
                    categoria_id,
                    nombres,
                    apellidos,
                    rut,
                    fecha_nacimiento,
                    estado
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    payload.categoria_id,
                    payload.nombres,
                    payload.apellidos,
                    payload.rut,
                    payload.fecha_nacimiento,
                    payload.estado_persona,
                ),
            )
            persona_id = cur.lastrowid

            # -------------------------------------------------
            # Insert usuario/acceso si aplica
            # -------------------------------------------------
            if payload.crear_acceso:
                if not payload.username:
                    raise HTTPException(status_code=400, detail="username es obligatorio si crear_acceso=true")
                if not payload.password_temporal:
                    raise HTTPException(status_code=400, detail="password_temporal es obligatorio si crear_acceso=true")
                if not payload.rol:
                    raise HTTPException(status_code=400, detail="rol es obligatorio si crear_acceso=true")

                cur.execute(
                    """
                    SELECT id
                    FROM usuarios
                    WHERE username = %s
                    LIMIT 1
                    """,
                    (payload.username,),
                )
                dup_user = cur.fetchone()
                if dup_user:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Ya existe usuarios.id={dup_user['id']} con username={payload.username}",
                    )

                nombre_usuario = payload.nombre_completo_usuario or f"{payload.nombres} {payload.apellidos}".strip()
                password_hash = make_password_hash(payload.password_temporal)

                cur.execute(
                    """
                    INSERT INTO usuarios (
                        username,
                        password,
                        rol,
                        nombre_completo,
                        archivo_url,
                        activo,
                        must_change_password,
                        empresa_id,
                        persona_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        payload.username,
                        password_hash,
                        payload.rol,
                        nombre_usuario,
                        payload.archivo_url,
                        1 if payload.activo else 0,
                        1 if payload.must_change_password else 0,
                        payload.empresa_id,
                        persona_id,
                    ),
                )
                usuario_id = cur.lastrowid

            # -------------------------------------------------
            # Insert trabajador si aplica
            # -------------------------------------------------
            if payload.crear_trabajador:
                fecha_ingreso = payload.fecha_ingreso or date.today()
                cargo = payload.cargo or ""
                area = payload.area or ""

                cur.execute(
                    """
                    INSERT INTO trabajadores (
                        persona_id,
                        usuario_id,
                        fecha_ingreso,
                        cargo,
                        area,
                        estado
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        persona_id,
                        usuario_id,
                        fecha_ingreso,
                        cargo,
                        area,
                        payload.estado_trabajador,
                    ),
                )
                trabajador_id = cur.lastrowid

            conn.commit()

            return {
                "ok": True,
                "persona_id": persona_id,
                "trabajador_id": trabajador_id,
                "usuario_id": usuario_id,
                "dashboard_por_rol": payload.rol if payload.crear_acceso else None,
                "mensaje": "Alta integral RRHH creada correctamente",
            }

    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error alta_integral_rrhh: {str(e)}")
    finally:
        if conn:
            conn.close()
