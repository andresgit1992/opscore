
from __future__ import annotations

import os
import threading
from pathlib import Path
from dataclasses import dataclass
import mysql.connector
from mysql.connector import pooling, Error as MySQLError

"carga .env"
BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=str(ENV_PATH), override=False)
except Exception:
    pass


class SingletonMeta(type):
    _instances = {}
    _lock = threading.Lock()

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with cls._lock:
                if cls not in cls._instances:
                    cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


"configuracio base de datos maria"
@dataclass(frozen=True)
class DBConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    pool_size: int = 10


class DBConfigFactory:

    @staticmethod
    def from_env() -> DBConfig:

        host = os.getenv("DB_HOST") or os.getenv("HOST")
        user = os.getenv("DB_USER") or os.getenv("USER")
        password = os.getenv("DB_PASSWORD") or os.getenv("PASSWORD")
        database = os.getenv("DB_NAME") or os.getenv("NAME")
        port = os.getenv("DB_PORT") or os.getenv("PORT") or "3306"

        missing = []

        if not host:
            missing.append("DB_HOST")

        if not user:
            missing.append("DB_USER")

        if password is None:
            missing.append("DB_PASSWORD")

        if not database:
            missing.append("DB_NAME")

        if missing:
            raise RuntimeError(
                f"Variables DB faltantes en .env: {', '.join(missing)}"
            )

        return DBConfig(
            host=host,
            port=int(port),
            user=user,
            password=password,
            database=database,
            pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
        )


"control de conexiones a la base de datos usando pool de conexiones"
class DBConnectionManager(metaclass=SingletonMeta):

    def __init__(self):

        self.config = DBConfigFactory.from_env()

        try:
            self.pool = pooling.MySQLConnectionPool(
                pool_name="bymetal_pool",
                pool_size=self.config.pool_size,
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
            )

        except MySQLError as e:
            raise RuntimeError(
                f"Error creando pool DB: {e} | host={self.config.host} db={self.config.database}"
            )

    def get_connection(self):

        try:
            return self.pool.get_connection()

        except MySQLError as e:
            raise RuntimeError(f"Error obteniendo conexion DB: {e}")


"api publica para obtener conexiones a la base de datos"
def get_db_connection():
  
    return DBConnectionManager().get_connection()