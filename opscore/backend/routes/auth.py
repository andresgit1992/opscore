from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Set

import mysql.connector
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------
# Proyecto / .env
# ---------------------------------------------------------------------
try:
    PROJECT_ROOT = Path(__file__).resolve().parents[2]
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(env_path)
except Exception:
    pass

router = APIRouter(prefix="/api", tags=["auth"])


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
# REQUEST / RESPONSE MODELS
# =========================================================

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=200)


class LoginResponse(BaseModel):
    success: bool
    mensaje: str
    rol: str
    nombre: str
    username: str
    archivo_url: Optional[str] = None
    must_change_password: bool = False


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=10, max_length=300)
    new_password: str = Field(..., min_length=6, max_length=200)


class ResetPasswordResponse(BaseModel):
    success: bool
    mensaje: str


class ChangePasswordRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    current_password: str = Field(..., min_length=1, max_length=200)
    new_password: str = Field(..., min_length=6, max_length=200)


class ChangePasswordResponse(BaseModel):
    success: bool
    mensaje: str


# =========================================================
# DB CONFIG / CONNECTION
# =========================================================

@dataclass(frozen=True)
class DBConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


class AuthDBConfigFactory:
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


class AuthDBConnectionManager(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.config = AuthDBConfigFactory.from_env()

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
    return AuthDBConnectionManager().connect()


# =========================================================
# CONTRACT REGISTRY
# =========================================================

class AuthContractRegistry(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.default_role = "Trabajador"
        self.legacy_admin_roles = {"admin", "administrador"}
        self.roles_map = {
            "programador": "Programador",
            "trabajador": "Trabajador",
            "gerente": "Gerente",
        }


# =========================================================
# HELPERS / FACTORIES
# =========================================================

_PBKDF2_RE = re.compile(r"^pbkdf2_sha256\$(\d+)\$([0-9a-f]+)\$([0-9a-f]+)$")


class PasswordFactory:
    @staticmethod
    def is_pbkdf2_hash(value: str) -> bool:
        return bool(value and _PBKDF2_RE.match(value.strip()))

    @staticmethod
    def hash_password_pbkdf2(password: str, iterations: int = 210_000) -> str:
        salt = secrets.token_bytes(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"

    @staticmethod
    def verify_password_pbkdf2(password: str, stored: str) -> bool:
        m = _PBKDF2_RE.match(stored.strip())
        if not m:
            return False
        iterations = int(m.group(1))
        salt = bytes.fromhex(m.group(2))
        expected = bytes.fromhex(m.group(3))
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(dk, expected)

    @staticmethod
    def sha256_hex(s: str) -> str:
        return hashlib.sha256(s.encode("utf-8")).hexdigest()


class AuthHelperFactory:
    @staticmethod
    def normalize_role(raw: Optional[str]) -> str:
        registry = AuthContractRegistry()
        if not raw:
            return registry.default_role

        r = raw.strip()
        low = r.lower()

        if low in registry.legacy_admin_roles:
            return "Gerente"

        if low in registry.roles_map:
            return registry.roles_map[low]

        return r

    @staticmethod
    def table_missing_error(e: mysql.connector.Error) -> bool:
        return getattr(e, "errno", None) == 1146

    @staticmethod
    def column_missing_error(e: mysql.connector.Error) -> bool:
        return getattr(e, "errno", None) == 1054

    @staticmethod
    def safe_int(v: Any, default: int = 0) -> int:
        try:
            return int(v)
        except Exception:
            return default

    @staticmethod
    def get_table_columns(conn, table: str) -> Set[str]:
        cur = conn.cursor()
        try:
            cur.execute(f"DESCRIBE {table}")
            return {r[0] for r in cur.fetchall()}
        finally:
            try:
                cur.close()
            except Exception:
                pass


class AuthResponseFactory:
    @staticmethod
    def login_success(
        rol: str,
        nombre: str,
        username: str,
        archivo_url: Optional[str] = None,
        must_change_password: bool = False,
    ) -> Dict[str, Any]:
        return {
            "success": True,
            "mensaje": "Bienvenido",
            "rol": rol,
            "nombre": nombre,
            "username": username,
            "archivo_url": archivo_url,
            "must_change_password": must_change_password,
        }

    @staticmethod
    def simple_success(mensaje: str) -> Dict[str, Any]:
        return {
            "success": True,
            "mensaje": mensaje,
        }


# =========================================================
# PASSWORD HISTORY HELPERS
# =========================================================

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
        last_hashes = [r["password_hash"] if isinstance(r, dict) else r[0] for r in rows]
        if new_hash in last_hashes:
            raise HTTPException(
                status_code=400,
                detail="No puedes reutilizar una contrasena usada recientemente (ultimas 3).",
            )
    except mysql.connector.Error as e:
        if AuthHelperFactory.table_missing_error(e):
            return
        raise


def _push_password_history(cursor, usuario_id: int, new_hash: str) -> None:
    try:
        cursor.execute(
            """
            INSERT INTO usuarios_password_history (usuario_id, password_hash)
            VALUES (%s, %s)
            """,
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
        if AuthHelperFactory.table_missing_error(e):
            return
        raise


# =========================================================
# AUTH SERVICE
# =========================================================

class AuthService(metaclass=SingletonMeta):
    def login(self, payload: LoginRequest) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            cols = AuthHelperFactory.get_table_columns(conn, "usuarios")
            cursor = conn.cursor(dictionary=True)

            select_fields = [
                "id",
                "username",
                "password",
                "rol",
                "nombre_completo",
                "archivo_url",
            ]

            select_fields.append("activo" if "activo" in cols else "1 AS activo")
            select_fields.append("must_change_password" if "must_change_password" in cols else "0 AS must_change_password")

            sql = f"""
                SELECT {", ".join(select_fields)}
                FROM usuarios
                WHERE username = %s
                LIMIT 1
            """

            cursor.execute(sql, (payload.username,))
            user = cursor.fetchone()

            if not user:
                raise HTTPException(status_code=401, detail="Credenciales incorrectas")

            if AuthHelperFactory.safe_int(user.get("activo"), 1) != 1:
                raise HTTPException(status_code=403, detail="Usuario bloqueado. Contacta al gerente.")

            stored_pw = (user.get("password") or "").strip()
            ok = False

            if PasswordFactory.is_pbkdf2_hash(stored_pw):
                ok = PasswordFactory.verify_password_pbkdf2(payload.password, stored_pw)
            else:
                ok = hmac.compare_digest(stored_pw, payload.password)

            if not ok:
                raise HTTPException(status_code=401, detail="Credenciales incorrectas")

            # migracion legacy -> PBKDF2
            if not PasswordFactory.is_pbkdf2_hash(stored_pw):
                new_hash = PasswordFactory.hash_password_pbkdf2(payload.password)
                _enforce_password_history_last3(cursor, user["id"], new_hash)

                if "must_change_password" in cols:
                    cursor.execute(
                        """
                        UPDATE usuarios
                        SET password = %s,
                            must_change_password = 0
                        WHERE id = %s
                        """,
                        (new_hash, user["id"]),
                    )
                else:
                    cursor.execute(
                        "UPDATE usuarios SET password = %s WHERE id = %s",
                        (new_hash, user["id"]),
                    )

                _push_password_history(cursor, user["id"], new_hash)

            if "last_login_at" in cols:
                cursor.execute(
                    """
                    UPDATE usuarios
                    SET last_login_at = NOW()
                    WHERE id = %s
                    """,
                    (user["id"],),
                )

            conn.commit()

            rol = AuthHelperFactory.normalize_role(user.get("rol"))
            nombre = user.get("nombre_completo") or user.get("username") or "Usuario"
            must_change = bool(AuthHelperFactory.safe_int(user.get("must_change_password"), 0) == 1)

            return AuthResponseFactory.login_success(
                rol=rol,
                nombre=nombre,
                username=user.get("username") or payload.username,
                archivo_url=user.get("archivo_url"),
                must_change_password=must_change,
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
            conn.close()

    def reset_password(self, payload: ResetPasswordRequest) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            cols = AuthHelperFactory.get_table_columns(conn, "usuarios")
            cursor = conn.cursor(dictionary=True)
            token_hash = PasswordFactory.sha256_hex(payload.token)

            try:
                cursor.execute(
                    """
                    SELECT id, usuario_id, expira_en, usado
                    FROM usuarios_password_reset
                    WHERE token_hash = %s
                    LIMIT 1
                    """,
                    (token_hash,),
                )
            except mysql.connector.Error as e:
                if AuthHelperFactory.table_missing_error(e):
                    raise HTTPException(
                        status_code=400,
                        detail="Recuperacion no habilitada: falta tabla usuarios_password_reset (migracion).",
                    )
                raise

            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=400, detail="Token invalido.")

            if AuthHelperFactory.safe_int(row.get("usado"), 0) == 1:
                raise HTTPException(status_code=400, detail="Token ya fue utilizado.")

            expira_en = row.get("expira_en")
            if not expira_en:
                raise HTTPException(status_code=400, detail="Token invalido (sin expiracion).")

            if isinstance(expira_en, datetime):
                if expira_en <= datetime.now():
                    raise HTTPException(status_code=400, detail="Token expirado.")
            else:
                cursor.execute(
                    """
                    SELECT (expira_en > NOW()) AS vigente
                    FROM usuarios_password_reset
                    WHERE id = %s
                    """,
                    (row["id"],),
                )
                v = cursor.fetchone()
                if not v or AuthHelperFactory.safe_int(v.get("vigente"), 0) != 1:
                    raise HTTPException(status_code=400, detail="Token expirado.")

            usuario_id = int(row["usuario_id"])
            new_hash = PasswordFactory.hash_password_pbkdf2(payload.new_password)

            _enforce_password_history_last3(cursor, usuario_id, new_hash)

            if "must_change_password" in cols:
                cursor.execute(
                    """
                    UPDATE usuarios
                    SET password = %s,
                        must_change_password = 0
                    WHERE id = %s
                    """,
                    (new_hash, usuario_id),
                )
            else:
                cursor.execute(
                    "UPDATE usuarios SET password = %s WHERE id = %s",
                    (new_hash, usuario_id),
                )

            _push_password_history(cursor, usuario_id, new_hash)

            cursor.execute(
                """
                UPDATE usuarios_password_reset
                SET usado = 1
                WHERE id = %s
                """,
                (row["id"],),
            )

            conn.commit()
            return AuthResponseFactory.simple_success("Contrasena actualizada correctamente.")

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

    def change_password(self, payload: ChangePasswordRequest) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            cols = AuthHelperFactory.get_table_columns(conn, "usuarios")
            cursor = conn.cursor(dictionary=True)

            cursor.execute(
                """
                SELECT id, username, password,
                       {activo_expr},
                       {mcp_expr}
                FROM usuarios
                WHERE username = %s
                LIMIT 1
                """.format(
                    activo_expr="activo" if "activo" in cols else "1 AS activo",
                    mcp_expr="must_change_password" if "must_change_password" in cols else "0 AS must_change_password",
                ),
                (payload.username,),
            )
            user = cursor.fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="Usuario no encontrado")

            if AuthHelperFactory.safe_int(user.get("activo"), 1) != 1:
                raise HTTPException(status_code=403, detail="Usuario bloqueado. Contacta al gerente.")

            stored_pw = (user.get("password") or "").strip()
            ok = False

            if PasswordFactory.is_pbkdf2_hash(stored_pw):
                ok = PasswordFactory.verify_password_pbkdf2(payload.current_password, stored_pw)
            else:
                ok = hmac.compare_digest(stored_pw, payload.current_password)

            if not ok:
                raise HTTPException(status_code=401, detail="Contrasena actual incorrecta")

            new_hash = PasswordFactory.hash_password_pbkdf2(payload.new_password)
            _enforce_password_history_last3(cursor, int(user["id"]), new_hash)

            if "must_change_password" in cols:
                cursor.execute(
                    """
                    UPDATE usuarios
                    SET password = %s,
                        must_change_password = 0
                    WHERE id = %s
                    """,
                    (new_hash, user["id"]),
                )
            else:
                cursor.execute(
                    "UPDATE usuarios SET password = %s WHERE id = %s",
                    (new_hash, user["id"]),
                )

            _push_password_history(cursor, int(user["id"]), new_hash)

            conn.commit()
            return AuthResponseFactory.simple_success("Contrasena cambiada correctamente.")

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

    def me(self, username: str) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            cols = AuthHelperFactory.get_table_columns(conn, "usuarios")
            cursor = conn.cursor(dictionary=True)

            select_fields = [
                "id",
                "username",
                "rol",
                "nombre_completo",
                "archivo_url",
                ("activo" if "activo" in cols else "1 AS activo"),
                ("must_change_password" if "must_change_password" in cols else "0 AS must_change_password"),
                ("last_login_at" if "last_login_at" in cols else "NULL AS last_login_at"),
            ]

            cursor.execute(
                f"""
                SELECT {", ".join(select_fields)}
                FROM usuarios
                WHERE username = %s
                LIMIT 1
                """,
                (username,),
            )
            user = cursor.fetchone()
            if not user:
                raise HTTPException(status_code=404, detail="Usuario no encontrado")

            user["rol"] = AuthHelperFactory.normalize_role(user.get("rol"))
            return user

        except HTTPException:
            raise
        except mysql.connector.Error as e:
            raise HTTPException(status_code=500, detail=f"Error DB: {str(e)}")
        finally:
            conn.close()


# =========================================================
# ROUTES
# =========================================================

@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest):
    return AuthService().login(payload)


@router.post("/reset-password", response_model=ResetPasswordResponse)
def reset_password(payload: ResetPasswordRequest):
    return AuthService().reset_password(payload)


@router.post("/change-password", response_model=ChangePasswordResponse)
def change_password(payload: ChangePasswordRequest):
    return AuthService().change_password(payload)


@router.get("/me")
def me(username: str):
    return AuthService().me(username)


@router.get("/health")
def health():
    return {"status": "ok", "router": "auth"}