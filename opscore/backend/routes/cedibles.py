# backend/routes/cedibles.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import shutil
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, List, Any, Dict

import mysql.connector
from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form
from pydantic import BaseModel

router = APIRouter(prefix="/api/cedibles", tags=["cedibles"])


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
# DB
# =========================================================
@dataclass(frozen=True)
class DBConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


class CediblesDBConfigFactory:
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
                detail=f"Variables DB_* no configuradas correctamente: {', '.join(missing)}",
            )

        return DBConfig(
            host=host,
            port=port,
            user=user or "",
            password=password or "",
            database=database or "",
        )


class CediblesDBConnectionManager(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.config = CediblesDBConfigFactory.from_env()

    def connect(self):
        try:
            return mysql.connector.connect(
                host=self.config.host,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
                port=self.config.port,
                autocommit=False,
                charset="utf8mb4",
                use_unicode=True,
            )
        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error conexion DB: {str(e)}")


def get_db_connection():
    return CediblesDBConnectionManager().connect()


# =========================================================
# SCHEMAS
# =========================================================
class CedibleCreate(BaseModel):
    empresa_id: Optional[int] = None
    folio: Optional[str] = None
    cliente: Optional[str] = None
    estado: Optional[str] = "Pendiente"
    archivo_url: Optional[str] = None
    monto_total: Optional[float] = None
    numero_factura: Optional[str] = None
    fecha_factura: Optional[date] = None


class CedibleOut(BaseModel):
    id: int
    empresa_id: Optional[int] = None
    folio: Optional[str] = None
    cliente: Optional[str] = None
    estado: Optional[str] = None
    fecha: Optional[datetime] = None
    archivo_url: Optional[str] = None
    monto_total: Optional[float] = None
    numero_factura: Optional[str] = None
    fecha_factura: Optional[date] = None
    archivado: int
    pagado_at: Optional[datetime] = None
    pago_id: Optional[int] = None


class SyncPagoRequest(BaseModel):
    empresa_id: Optional[int] = None
    empresa: Optional[str] = None
    numero_factura: Optional[str] = None
    monto_total: Optional[float] = None
    fecha_emision: Optional[date] = None
    fecha_vencimiento: Optional[date] = None


# =========================================================
# CONTRACT REGISTRY
# =========================================================
class CediblesContractRegistry(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.storage_subdir = "cedibles"
        self.default_estado = "Pendiente"
        self.default_vencimiento_days = 30


# =========================================================
# HELPERS / FACTORIES
# =========================================================
class CediblesHelperFactory:
    @staticmethod
    def dictfetchall(cursor) -> List[Dict[str, Any]]:
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    @staticmethod
    def require(value, msg: str):
        if value is None or (isinstance(value, str) and value.strip() == ""):
            raise HTTPException(status_code=400, detail=msg)

    @staticmethod
    def as_date(value: Any) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            try:
                return date.fromisoformat(value[:10])
            except Exception:
                return None
        return None

    @staticmethod
    def validate_fecha_factura(fecha: Optional[date]):
        fecha = CediblesHelperFactory.as_date(fecha)
        if not fecha:
            return

        max_future_days = int(os.getenv("CEDIBLE_MAX_FUTURE_DAYS", "365"))
        max_past_years = int(os.getenv("CEDIBLE_MAX_PAST_YEARS", "10"))

        today = date.today()
        if fecha > (today + timedelta(days=max_future_days)):
            raise HTTPException(status_code=400, detail="fecha_factura fuera de rango (demasiado futura)")
        if fecha < (today - timedelta(days=365 * max_past_years)):
            raise HTTPException(status_code=400, detail="fecha_factura fuera de rango (demasiado antigua)")

    @staticmethod
    def compute_vencimiento(fecha_emision: date) -> date:
        fecha_emision = CediblesHelperFactory.as_date(fecha_emision)
        if not fecha_emision:
            raise HTTPException(status_code=400, detail="fecha_emision invalida")
        return date.fromordinal(
            fecha_emision.toordinal() + CediblesContractRegistry().default_vencimiento_days
        )

    @staticmethod
    def safe_filename(name: str) -> str:
        name = (name or "archivo").strip()
        name = name.replace("\\", "_").replace("/", "_")
        return name


class CediblesStorageFactory(metaclass=SingletonMeta):
    def project_base_dir(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def storage_dir(self) -> Path:
        d = self.project_base_dir() / "storage" / CediblesContractRegistry().storage_subdir
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_upload_to_storage(self, file: UploadFile) -> str:
        storage_dir = self.storage_dir()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{ts}_{CediblesHelperFactory.safe_filename(file.filename or 'archivo')}"
        dest_path = storage_dir / filename

        try:
            with dest_path.open("wb") as f:
                shutil.copyfileobj(file.file, f)
        finally:
            try:
                file.file.close()
            except Exception:
                pass

        return f"/storage/cedibles/{filename}"


class CediblesResponseFactory:
    @staticmethod
    def ok(**data) -> Dict[str, Any]:
        out = {"status": "ok"}
        out.update(data)
        return out


# =========================================================
# SERVICE
# =========================================================
class CediblesService(metaclass=SingletonMeta):
    def find_pago_id_by_cedible(self, cur, cedible_id: int) -> Optional[int]:
        cur.execute("SELECT id FROM pagos WHERE cedible_id = %s LIMIT 1", (cedible_id,))
        row = cur.fetchone()
        if not row:
            return None
        if isinstance(row, dict):
            return row.get("id")
        return row[0]

    def sync_pago_for_cedible_row(
        self,
        conn,
        *,
        cedible_id: int,
        empresa_id: int,
        numero_factura: str,
        monto_total: float,
        fecha_emision: date,
        archivo_url: Optional[str],
        empresa: Optional[str] = None,
    ) -> int:
        numero_factura = str(numero_factura)
        fecha_emision = CediblesHelperFactory.as_date(fecha_emision)
        CediblesHelperFactory.require(fecha_emision, "fecha_emision invalida")
        fecha_vencimiento = CediblesHelperFactory.compute_vencimiento(fecha_emision)

        cur = conn.cursor()

        pago_id = self.find_pago_id_by_cedible(cur, cedible_id)
        if pago_id:
            cur.execute(
                """
                UPDATE pagos
                SET numero_factura=%s,
                    empresa=%s,
                    monto_total=%s,
                    empresa_id=%s,
                    fecha_emision=%s,
                    fecha_vencimiento=%s,
                    archivo_url=%s,
                    estado_pago='pendiente',
                    archivado=0,
                    pagado_at=NULL
                WHERE id=%s
                """,
                (
                    numero_factura,
                    empresa,
                    monto_total,
                    empresa_id,
                    fecha_emision,
                    fecha_vencimiento,
                    archivo_url,
                    pago_id,
                ),
            )
            return int(pago_id)

        cur.execute(
            """
            INSERT INTO pagos
            (numero_factura, empresa, monto_total, empresa_id, fecha_emision, fecha_vencimiento, estado_pago, archivado, cedible_id, archivo_url)
            VALUES (%s, %s, %s, %s, %s, %s, 'pendiente', 0, %s, %s)
            """,
            (
                numero_factura,
                empresa,
                monto_total,
                empresa_id,
                fecha_emision,
                fecha_vencimiento,
                cedible_id,
                archivo_url,
            ),
        )
        return int(cur.lastrowid)

    def list_cedibles(
        self,
        *,
        archivado: Optional[int],
        estado: Optional[str],
        cliente: Optional[str],
        folio: Optional[str],
        empresa_id: Optional[int],
        numero_factura: Optional[str],
        limit: int,
    ) -> List[Dict[str, Any]]:
        conn = get_db_connection()
        try:
            cur = conn.cursor()

            where = []
            params: List[Any] = []

            if archivado is not None:
                where.append("c.archivado = %s")
                params.append(archivado)
            if estado:
                where.append("c.estado = %s")
                params.append(estado)
            if cliente:
                where.append("c.cliente LIKE %s")
                params.append(f"%{cliente}%")
            if folio:
                where.append("c.folio LIKE %s")
                params.append(f"%{folio}%")
            if empresa_id is not None:
                where.append("c.empresa_id = %s")
                params.append(empresa_id)
            if numero_factura:
                where.append("c.numero_factura = %s")
                params.append(str(numero_factura))

            where_sql = ("WHERE " + " AND ".join(where)) if where else ""

            sql = f"""
                SELECT
                    c.id, c.empresa_id, c.folio, c.cliente, c.estado, c.fecha, c.archivo_url,
                    c.monto_total, c.numero_factura, c.fecha_factura, c.archivado, c.pagado_at,
                    p.id AS pago_id
                FROM cedibles c
                LEFT JOIN pagos p ON p.cedible_id = c.id
                {where_sql}
                ORDER BY c.id DESC
                LIMIT %s
            """
            params.append(limit)

            cur.execute(sql, params)
            return CediblesHelperFactory.dictfetchall(cur)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def get_cedible(self, cedible_id: int) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            sql = """
                SELECT
                    c.id, c.empresa_id, c.folio, c.cliente, c.estado, c.fecha, c.archivo_url,
                    c.monto_total, c.numero_factura, c.fecha_factura, c.archivado, c.pagado_at,
                    p.id AS pago_id
                FROM cedibles c
                LEFT JOIN pagos p ON p.cedible_id = c.id
                WHERE c.id = %s
                LIMIT 1
            """
            cur.execute(sql, (cedible_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Cedible no encontrado")
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def create_cedible(self, payload: CedibleCreate) -> Dict[str, Any]:
        CediblesHelperFactory.validate_fecha_factura(payload.fecha_factura)

        conn = get_db_connection()
        try:
            cur = conn.cursor()

            cur.execute(
                """
                INSERT INTO cedibles
                (empresa_id, folio, cliente, estado, archivo_url, monto_total, numero_factura, fecha_factura)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    payload.empresa_id,
                    payload.folio,
                    payload.cliente,
                    payload.estado or CediblesContractRegistry().default_estado,
                    payload.archivo_url,
                    payload.monto_total,
                    payload.numero_factura,
                    payload.fecha_factura,
                ),
            )
            conn.commit()

            cedible_id = int(cur.lastrowid)
            pago_id: Optional[int] = None
            synced = False

            if (
                payload.empresa_id is not None
                and payload.numero_factura
                and payload.monto_total is not None
                and payload.fecha_factura is not None
            ):
                numero_factura = str(payload.numero_factura)
                fecha_emision = CediblesHelperFactory.as_date(payload.fecha_factura)
                CediblesHelperFactory.require(fecha_emision, "fecha_factura invalida")

                pago_id = self.sync_pago_for_cedible_row(
                    conn,
                    cedible_id=cedible_id,
                    empresa_id=payload.empresa_id,
                    numero_factura=numero_factura,
                    monto_total=float(payload.monto_total),
                    fecha_emision=fecha_emision,
                    archivo_url=payload.archivo_url,
                    empresa=None,
                )
                conn.commit()
                synced = True

            return CediblesResponseFactory.ok(
                cedible_id=cedible_id,
                synced_pago=synced,
                pago_id=pago_id,
            )

        except mysql.connector.Error as e:
            try:
                conn.rollback()
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def upload_cedible(
        self,
        *,
        file: UploadFile,
        empresa_id: Optional[int],
        folio: Optional[str],
        cliente: Optional[str],
        estado: Optional[str],
        monto_total: Optional[float],
        numero_factura: Optional[str],
        fecha_factura: Optional[str],
    ) -> Dict[str, Any]:
        fecha_factura_date: Optional[date] = None
        if fecha_factura:
            fecha_factura_date = CediblesHelperFactory.as_date(fecha_factura)
            if not fecha_factura_date:
                raise HTTPException(status_code=400, detail="fecha_factura invalida (usar YYYY-MM-DD)")
        else:
            # modo automático: si no viene fecha manual, usar la fecha actual de ingreso
            fecha_factura_date = date.today()

        CediblesHelperFactory.validate_fecha_factura(fecha_factura_date)

        archivo_url = CediblesStorageFactory().save_upload_to_storage(file)

        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO cedibles
                (empresa_id, folio, cliente, estado, archivo_url, monto_total, numero_factura, fecha_factura)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    empresa_id,
                    folio,
                    cliente,
                    estado or CediblesContractRegistry().default_estado,
                    archivo_url,
                    monto_total,
                    numero_factura,
                    fecha_factura_date,
                ),
            )
            conn.commit()

            cedible_id = int(cur.lastrowid)
            pago_id: Optional[int] = None
            synced = False

            if empresa_id is not None and numero_factura and monto_total is not None and fecha_factura_date is not None:
                pago_id = self.sync_pago_for_cedible_row(
                    conn,
                    cedible_id=cedible_id,
                    empresa_id=empresa_id,
                    numero_factura=str(numero_factura),
                    monto_total=float(monto_total),
                    fecha_emision=fecha_factura_date,
                    archivo_url=archivo_url,
                    empresa=None,
                )
                conn.commit()
                synced = True

            return CediblesResponseFactory.ok(
                cedible_id=cedible_id,
                archivo_url=archivo_url,
                synced_pago=synced,
                pago_id=pago_id,
            )

        except mysql.connector.Error as e:
            try:
                conn.rollback()
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def sync_pago_from_cedible(self, cedible_id: int, payload: SyncPagoRequest) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            cur = conn.cursor(dictionary=True)

            cur.execute("SELECT * FROM cedibles WHERE id = %s LIMIT 1", (cedible_id,))
            ced = cur.fetchone()
            if not ced:
                raise HTTPException(status_code=404, detail="Cedible no encontrado")

            CediblesHelperFactory.validate_fecha_factura(ced.get("fecha_factura"))

            numero_factura = payload.numero_factura or ced.get("numero_factura")
            monto_total = payload.monto_total if payload.monto_total is not None else ced.get("monto_total")
            empresa_id = payload.empresa_id if payload.empresa_id is not None else ced.get("empresa_id")
            fecha_emision = payload.fecha_emision or CediblesHelperFactory.as_date(ced.get("fecha_factura"))

            CediblesHelperFactory.require(numero_factura, "numero_factura requerido para crear pago")
            CediblesHelperFactory.require(monto_total, "monto_total requerido para crear pago")
            CediblesHelperFactory.require(empresa_id, "empresa_id requerido para crear pago")
            CediblesHelperFactory.require(fecha_emision, "fecha_emision/fecha_factura requerida para crear pago")

            numero_factura = str(numero_factura)
            fecha_emision = CediblesHelperFactory.as_date(fecha_emision)
            CediblesHelperFactory.require(fecha_emision, "fecha_emision invalida")

            if payload.fecha_vencimiento:
                fecha_vencimiento = CediblesHelperFactory.as_date(payload.fecha_vencimiento)
                CediblesHelperFactory.require(fecha_vencimiento, "fecha_vencimiento invalida")
            else:
                fecha_vencimiento = CediblesHelperFactory.compute_vencimiento(fecha_emision)

            cur.execute("SELECT id FROM pagos WHERE cedible_id = %s LIMIT 1", (cedible_id,))
            existing = cur.fetchone()

            if existing:
                pago_id = int(existing["id"])
                cur.execute(
                    """
                    UPDATE pagos
                    SET numero_factura=%s,
                        empresa=%s,
                        monto_total=%s,
                        empresa_id=%s,
                        fecha_emision=%s,
                        fecha_vencimiento=%s,
                        archivo_url=%s,
                        estado_pago='pendiente',
                        archivado=0,
                        pagado_at=NULL
                    WHERE id=%s
                    """,
                    (
                        numero_factura,
                        payload.empresa,
                        monto_total,
                        empresa_id,
                        fecha_emision,
                        fecha_vencimiento,
                        ced.get("archivo_url"),
                        pago_id,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO pagos
                    (numero_factura, empresa, monto_total, empresa_id, fecha_emision, fecha_vencimiento, estado_pago, archivado, cedible_id, archivo_url)
                    VALUES (%s, %s, %s, %s, %s, %s, 'pendiente', 0, %s, %s)
                    """,
                    (
                        numero_factura,
                        payload.empresa,
                        monto_total,
                        empresa_id,
                        fecha_emision,
                        fecha_vencimiento,
                        cedible_id,
                        ced.get("archivo_url"),
                    ),
                )
                pago_id = int(cur.lastrowid)

            conn.commit()
            return CediblesResponseFactory.ok(cedible_id=cedible_id, pago_id=pago_id)

        except mysql.connector.Error as e:
            try:
                conn.rollback()
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def sync_pagos_masivo(
        self,
        *,
        archivado: Optional[int],
        empresa_id: Optional[int],
        limit: int,
    ) -> Dict[str, Any]:
        conn = get_db_connection()
        created = 0
        updated = 0
        skipped = 0
        errors: List[Dict[str, Any]] = []

        try:
            cur = conn.cursor(dictionary=True)

            where = ["c.archivado = %s"]
            params: List[Any] = [archivado]

            if empresa_id is not None:
                where.append("c.empresa_id = %s")
                params.append(empresa_id)

            where_sql = " AND ".join(where)

            cur.execute(
                f"""
                SELECT c.*
                FROM cedibles c
                WHERE {where_sql}
                ORDER BY c.id ASC
                LIMIT %s
                """,
                params + [limit],
            )
            cedibles = cur.fetchall() or []

            for c in cedibles:
                try:
                    CediblesHelperFactory.validate_fecha_factura(c.get("fecha_factura"))

                    if (
                        not c.get("empresa_id")
                        or not c.get("numero_factura")
                        or c.get("monto_total") is None
                        or not c.get("fecha_factura")
                    ):
                        skipped += 1
                        continue

                    empresa_id_row = int(c["empresa_id"])
                    numero_factura = str(c["numero_factura"]).strip()
                    fecha_emision = CediblesHelperFactory.as_date(c["fecha_factura"])
                    CediblesHelperFactory.require(fecha_emision, "fecha_factura invalida")
                    fecha_vencimiento = CediblesHelperFactory.compute_vencimiento(fecha_emision)

                    # 1) buscar por cedible_id
                    cur.execute(
                        "SELECT id, estado_pago FROM pagos WHERE cedible_id = %s LIMIT 1",
                        (c["id"],),
                    )
                    existing = cur.fetchone()

                    # 2) fallback por unique key (empresa_id, numero_factura)
                    if not existing:
                        cur.execute(
                            """
                            SELECT id, estado_pago
                            FROM pagos
                            WHERE empresa_id = %s AND numero_factura = %s
                            LIMIT 1
                            """,
                            (empresa_id_row, numero_factura),
                        )
                        existing = cur.fetchone()

                    if existing:
                        cur.execute(
                            """
                            UPDATE pagos
                            SET empresa = COALESCE(empresa, %s),
                                monto_total = CASE
                                    WHEN monto_total IS NULL OR monto_total <= 0 THEN %s
                                    ELSE monto_total
                                END,
                                empresa_id = %s,
                                fecha_emision = %s,
                                fecha_vencimiento = %s,
                                archivo_url = COALESCE(%s, archivo_url),
                                cedible_id = COALESCE(cedible_id, %s)
                            WHERE id = %s
                            """,
                            (
                                c.get("cliente"),
                                c["monto_total"],
                                empresa_id_row,
                                fecha_emision,
                                fecha_vencimiento,
                                c.get("archivo_url"),
                                c["id"],
                                existing["id"],
                            ),
                        )
                        if int(getattr(cur, "rowcount", 0) or 0) > 0:
                            updated += 1
                        else:
                            skipped += 1
                        continue

                    cur.execute(
                        """
                        INSERT INTO pagos
                        (numero_factura, empresa, monto_total, empresa_id, fecha_emision, fecha_vencimiento, estado_pago, archivado, cedible_id, archivo_url)
                        VALUES (%s, %s, %s, %s, %s, %s, 'pendiente', 0, %s, %s)
                        """,
                        (
                            numero_factura,
                            c.get("cliente"),
                            c["monto_total"],
                            empresa_id_row,
                            fecha_emision,
                            fecha_vencimiento,
                            c["id"],
                            c.get("archivo_url"),
                        ),
                    )
                    created += 1

                except HTTPException as he:
                    skipped += 1
                    errors.append({"cedible_id": c.get("id"), "error": str(he.detail)})
                except Exception as ex:
                    skipped += 1
                    errors.append({"cedible_id": c.get("id"), "error": str(ex)})

            conn.commit()
            return CediblesResponseFactory.ok(
                created=created,
                updated=updated,
                skipped=skipped,
                errors=errors[:50],
            )

        except mysql.connector.Error as e:
            try:
                conn.rollback()
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def archivar_cedible(self, cedible_id: int) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE cedibles SET archivado = 1 WHERE id = %s", (cedible_id,))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Cedible no encontrado")
            conn.commit()
            return CediblesResponseFactory.ok(cedible_id=cedible_id, archivado=1)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def marcar_pagado_cedible(self, cedible_id: int) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            now = datetime.now()
            cur.execute(
                "UPDATE cedibles SET estado = 'Pagado', pagado_at = %s WHERE id = %s",
                (now, cedible_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Cedible no encontrado")

            cur.execute(
                "UPDATE pagos SET estado_pago='pagado', pagado_at=%s WHERE cedible_id = %s",
                (now, cedible_id),
            )
            conn.commit()
            return CediblesResponseFactory.ok(
                cedible_id=cedible_id,
                pagado_at=now.isoformat(),
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass


# =========================================================
# ENDPOINTS
# =========================================================
@router.get("", response_model=List[CedibleOut])
def list_cedibles(
    archivado: Optional[int] = Query(None, description="0 o 1"),
    estado: Optional[str] = Query(None),
    cliente: Optional[str] = Query(None),
    folio: Optional[str] = Query(None),
    empresa_id: Optional[int] = Query(None),
    numero_factura: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
):
    return CediblesService().list_cedibles(
        archivado=archivado,
        estado=estado,
        cliente=cliente,
        folio=folio,
        empresa_id=empresa_id,
        numero_factura=numero_factura,
        limit=limit,
    )


@router.get("/{cedible_id}", response_model=CedibleOut)
def get_cedible(cedible_id: int):
    return CediblesService().get_cedible(cedible_id)


@router.post("", response_model=dict)
def create_cedible(payload: CedibleCreate):
    return CediblesService().create_cedible(payload)


@router.post("/upload", response_model=dict)
def upload_cedible(
    file: UploadFile = File(...),
    empresa_id: Optional[int] = Form(None),
    folio: Optional[str] = Form(None),
    cliente: Optional[str] = Form(None),
    estado: Optional[str] = Form("Pendiente"),
    monto_total: Optional[float] = Form(None),
    numero_factura: Optional[str] = Form(None),
    fecha_factura: Optional[str] = Form(None),
):
    return CediblesService().upload_cedible(
        file=file,
        empresa_id=empresa_id,
        folio=folio,
        cliente=cliente,
        estado=estado,
        monto_total=monto_total,
        numero_factura=numero_factura,
        fecha_factura=fecha_factura,
    )


@router.post("/{cedible_id}/sync-pago", response_model=dict)
def sync_pago_from_cedible(cedible_id: int, payload: SyncPagoRequest):
    return CediblesService().sync_pago_from_cedible(cedible_id, payload)


@router.post("/sync-pagos", response_model=dict)
def sync_pagos_masivo(
    archivado: Optional[int] = Query(0, description="Solo cedibles archivado=0 por defecto"),
    empresa_id: Optional[int] = Query(None, description="Filtrar por empresa_id"),
    limit: int = Query(500, ge=1, le=5000),
):
    return CediblesService().sync_pagos_masivo(
        archivado=archivado,
        empresa_id=empresa_id,
        limit=limit,
    )


@router.post("/{cedible_id}/archivar", response_model=dict)
def archivar_cedible(cedible_id: int):
    return CediblesService().archivar_cedible(cedible_id)


@router.post("/{cedible_id}/marcar-pagado", response_model=dict)
def marcar_pagado_cedible(cedible_id: int):
    return CediblesService().marcar_pagado_cedible(cedible_id)