# backend/routes/ots.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Query
from backend.config import get_db_connection

router = APIRouter(prefix="/api/ots", tags=["OTS"])


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
# STORAGE
# =============================================================================
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVIDENCIA_DIR = os.path.join(ROOT_DIR, "storage", "evidencia")
os.makedirs(EVIDENCIA_DIR, exist_ok=True)


# =============================================================================
# CONTRACT REGISTRY
# =============================================================================
class OTSContractRegistry(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.estados_validos = ["Pendiente", "En Proceso", "Finalizado"]
        self.storage_subdir = "evidencia"


# =============================================================================
# HELPERS / FACTORIES
# =============================================================================
class OTSHelperFactory:
    @staticmethod
    def safe_filename(name: str) -> str:
        base = os.path.basename(name or "archivo")
        base = base.replace(" ", "_")
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
        return base or "archivo"

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
        try:
            return int(row.get("c", 0)) > 0
        except Exception:
            return False


class OTSStorageFactory(metaclass=SingletonMeta):
    def evidencia_dir(self) -> str:
        os.makedirs(EVIDENCIA_DIR, exist_ok=True)
        return EVIDENCIA_DIR

    def save_upload(self, archivo: UploadFile, prefix: str) -> str:
        safe = OTSHelperFactory.safe_filename(archivo.filename)
        nombre_archivo = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe}"
        ruta_archivo = os.path.join(self.evidencia_dir(), nombre_archivo)

        with open(ruta_archivo, "wb") as buffer:
            shutil.copyfileobj(archivo.file, buffer)

        return f"/storage/evidencia/{nombre_archivo}"


class OTSResponseFactory:
    @staticmethod
    def ok(message: str, **extra) -> Dict[str, Any]:
        out = {"mensaje": message}
        out.update(extra)
        return out


# =============================================================================
# SERVICE
# =============================================================================
class OTSService(metaclass=SingletonMeta):
    def crear_ot(
        self,
        *,
        descripcion: str,
        empresa: str,
        prioridad: str,
        asignado_a: str,
        archivo: Optional[UploadFile] = None,
    ) -> Dict[str, Any]:
        conn = None
        try:
            archivo_url = None

            if archivo:
                archivo_url = OTSStorageFactory().save_upload(archivo, prefix="OT")

            conn = get_db_connection()
            cursor = conn.cursor()

            cursor.execute(
                """
                INSERT INTO trabajos
                (descripcion, empresa, prioridad, asignado_a, archivo_url)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (descripcion, empresa, prioridad, asignado_a, archivo_url),
            )

            conn.commit()
            return OTSResponseFactory.ok(
                "OT creada correctamente",
                id=cursor.lastrowid,
                archivo_url=archivo_url,
            )

        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise HTTPException(status_code=500, detail=f"Error al crear OT: {str(e)}")

        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def listar_ots(self, *, estado: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            if estado:
                cursor.execute(
                    "SELECT * FROM trabajos WHERE estado = %s ORDER BY fecha_creacion DESC",
                    (estado,),
                )
            else:
                cursor.execute("SELECT * FROM trabajos ORDER BY fecha_creacion DESC")

            return cursor.fetchall() or []

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error al listar OTs: {str(e)}")

        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def cambiar_estado_ot(self, *, ot_id: int, estado: str) -> Dict[str, Any]:
        conn = None
        try:
            if estado not in OTSContractRegistry().estados_validos:
                raise HTTPException(status_code=400, detail="Estado invalido")

            conn = get_db_connection()
            cursor = conn.cursor()

            cursor.execute("UPDATE trabajos SET estado = %s WHERE id = %s", (estado, ot_id))
            conn.commit()

            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="OT no encontrada")

            return OTSResponseFactory.ok(
                "Estado de OT actualizado",
                id=ot_id,
                estado=estado,
            )

        except HTTPException:
            raise

        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise HTTPException(status_code=500, detail=f"Error al actualizar estado OT: {str(e)}")

        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def crear_reporte_terreno(
        self,
        *,
        ot_id: int,
        tecnico: str,
        observacion: str,
        archivo: Optional[UploadFile] = None,
    ) -> Dict[str, Any]:
        conn = None
        try:
            archivo_url = None

            if archivo:
                archivo_url = OTSStorageFactory().save_upload(archivo, prefix="REP")

            conn = get_db_connection()
            cur_dict = conn.cursor(dictionary=True)

            tiene_ot_id = OTSHelperFactory.has_column(cur_dict, "reporte_terreno", "ot_id")

            cur = conn.cursor()

            if tiene_ot_id:
                cur.execute(
                    """
                    INSERT INTO reporte_terreno
                    (ot_id, tecnico, observacion, archivo_url)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (ot_id, tecnico, observacion, archivo_url),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO reporte_terreno
                    (tecnico, observacion, archivo_url)
                    VALUES (%s, %s, %s)
                    """,
                    (tecnico, observacion, archivo_url),
                )

            conn.commit()
            return OTSResponseFactory.ok(
                "Reporte de terreno guardado",
                id=cur.lastrowid,
                ot_id=ot_id,
                archivo_url=archivo_url,
            )

        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise HTTPException(status_code=500, detail=f"Error al crear reporte: {str(e)}")

        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass


# =============================================================================
# ENDPOINTS
# =============================================================================
@router.post("/")
async def crear_ot(
    descripcion: str = Form(...),
    empresa: str = Form(...),
    prioridad: str = Form(...),
    asignado_a: str = Form(...),
    archivo: UploadFile = File(None),
):
    return OTSService().crear_ot(
        descripcion=descripcion,
        empresa=empresa,
        prioridad=prioridad,
        asignado_a=asignado_a,
        archivo=archivo,
    )


@router.get("/")
def listar_ots(estado: Optional[str] = Query(default=None)):
    return OTSService().listar_ots(estado=estado)


@router.put("/{ot_id}/estado")
def cambiar_estado_ot(ot_id: int, estado: str):
    return OTSService().cambiar_estado_ot(ot_id=ot_id, estado=estado)


@router.post("/{ot_id}/reporte")
async def crear_reporte_terreno(
    ot_id: int,
    tecnico: str = Form(...),
    observacion: str = Form(...),
    archivo: UploadFile = File(None),
):
    return OTSService().crear_reporte_terreno(
        ot_id=ot_id,
        tecnico=tecnico,
        observacion=observacion,
        archivo=archivo,
    )


@router.get("/health")
def health():
    return {"status": "ok", "router": "ots"}