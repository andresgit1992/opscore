# -*- coding: utf-8 -*-
# backend/routes/cotizador.py
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import Table, TableStyle

from backend.config import get_db_connection

router = APIRouter(prefix="/api", tags=["Cotizador"])


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
# PATHS
# =============================================================================
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STORAGE_COT = os.path.join(ROOT_DIR, "storage", "cotizaciones")
os.makedirs(STORAGE_COT, exist_ok=True)


# =============================================================================
# REGLAS DE NEGOCIO
# =============================================================================
RBU_TERMINAL_DUMMY = "(No aplica)"
SHOW_RBU_TERMINALES = os.getenv("SHOW_RBU_TERMINALES", "0") == "1"
IVA_SIEMPRE_INCLUIDO_CODIGOS = {"METROPOL", "METBUS"}


# =============================================================================
# MODELOS
# =============================================================================
class ItemCotizacion(BaseModel):
    descripcion: str
    cantidad: int
    precio_base_db: int


class SolicitudCotizacion(BaseModel):
    empresa: str
    terminal: str
    items: List[ItemCotizacion]


# =============================================================================
# CONTRACT REGISTRY
# =============================================================================
class CotizadorContractRegistry(metaclass=SingletonMeta):
    def __init__(self) -> None:
        self.rbu_terminal_dummy = RBU_TERMINAL_DUMMY
        self.show_rbu_terminales = SHOW_RBU_TERMINALES
        self.iva_siempre_incluido_codigos = IVA_SIEMPRE_INCLUIDO_CODIGOS

    def normalizar_cliente_historial(self, empresa_raw: str) -> str:
        s = (empresa_raw or "").strip()
        up = s.upper()

        if "RBU" in up:
            return "RBU"
        if "METROPOL" in up:
            return "Metropol"
        if "METBUS" in up:
            return "Metbus"
        if "GRAN" in up and ("AMERICA" in up or "AMERICA" in up):
            return "Gran America"

        if up == "METROPOL":
            return "Metropol"
        if up in ("GRAN_AMERICA", "GRAN AMERICA", "GRANAMERICA"):
            return "Gran America"

        return s


# =============================================================================
# FACTORIES / HELPERS
# =============================================================================
class CotizadorHelperFactory:
    @staticmethod
    def money_clp(n: int) -> str:
        return f"${int(n):,}".replace(",", ".")

    @staticmethod
    def table_exists(cur, table_name: str) -> bool:
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
        if not row:
            return False
        if isinstance(row, dict):
            return int(list(row.values())[0]) > 0
        return int(row[0]) > 0

    @staticmethod
    def asegurar_fila_seq(cur) -> None:
        cur.execute("SELECT id FROM cotizacion_seq WHERE id = 1 LIMIT 1")
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO cotizacion_seq (id, next_num) VALUES (1, 0)")

    @staticmethod
    def resolve_empresa_id(
        cur_dict,
        empresa_id: Optional[int] = None,
        empresa_codigo: Optional[str] = None,
        empresa: Optional[str] = None,
    ) -> Optional[int]:
        if empresa_id is not None:
            try:
                return int(empresa_id)
            except Exception:
                return None

        code = (empresa_codigo or empresa or "").strip().upper()
        if not code:
            return None

        try:
            cur_dict.execute("SELECT id FROM empresas WHERE UPPER(codigo)=UPPER(%s) LIMIT 1", (code,))
            row = cur_dict.fetchone()
            if not row:
                return None
            return int(row["id"])
        except Exception:
            return None

    @staticmethod
    def normalize_optional_url(v: Any) -> Optional[str]:
        s = (str(v or "")).strip()
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


class CotizadorEmpresaFactory:
    @staticmethod
    def find_empresa_by_codigo_or_nombre(cur, empresa_raw: str) -> Optional[Dict[str, Any]]:
        s = (empresa_raw or "").strip()
        if not s:
            return None

        try:
            cur.execute("SELECT * FROM empresas WHERE UPPER(codigo) = UPPER(%s) LIMIT 1", (s,))
            row = cur.fetchone()
            if row:
                return row
        except Exception:
            pass

        cur.execute("SELECT * FROM empresas WHERE nombre_fantasia = %s LIMIT 1", (s,))
        row = cur.fetchone()
        if row:
            return row

        up = s.upper()
        try:
            if "RBU" in up:
                cur.execute("SELECT * FROM empresas WHERE UPPER(codigo)='RBU' LIMIT 1")
                return cur.fetchone()
            if "METROPOL" in up:
                cur.execute("SELECT * FROM empresas WHERE UPPER(codigo)='METROPOL' LIMIT 1")
                return cur.fetchone()
            if "METBUS" in up:
                cur.execute("SELECT * FROM empresas WHERE UPPER(codigo)='METBUS' LIMIT 1")
                return cur.fetchone()
            if "GRAN" in up:
                cur.execute("SELECT * FROM empresas WHERE UPPER(codigo) IN ('GRAN_AMERICA','GRANAMERICA') LIMIT 1")
                return cur.fetchone()
        except Exception:
            pass

        return None


class CotizadorTerminalFactory:
    @staticmethod
    def get_terminal_id(cur, empresa_id: int, terminal_nombre: str) -> Optional[int]:
        t = (terminal_nombre or "").strip()
        if not t or t == CotizadorContractRegistry().rbu_terminal_dummy:
            return None

        cur.execute(
            """
            SELECT id
            FROM terminales
            WHERE empresa_id = %s
              AND nombre = %s
            LIMIT 1
            """,
            (empresa_id, t),
        )
        row = cur.fetchone()
        if not row:
            return None
        return int(row["id"]) if isinstance(row, dict) else int(row[0])


class CotizadorResponseFactory:
    @staticmethod
    def empresas(empresas: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"empresas": empresas}

    @staticmethod
    def terminales(terminales: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"terminales": terminales}

    @staticmethod
    def categorias(empresa_id: Optional[int], categorias: List[str]) -> Dict[str, Any]:
        return {"empresa_id": empresa_id, "categorias": categorias}

    @staticmethod
    def items(empresa_id: Optional[int], categoria: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"empresa_id": empresa_id, "categoria": categoria, "items": items}

    @staticmethod
    def info_sistema(
        empresas: Dict[str, Any],
        catalogo: Dict[str, Dict[str, int]],
        catalogo_por_cliente: Optional[Dict[str, Dict[str, Dict[str, int]]]] = None,
    ) -> Dict[str, Any]:
        return {
            "empresas": empresas,
            "catalogo": catalogo,
            "catalogo_por_cliente": catalogo_por_cliente or {},
        }


# =============================================================================
# PDF SERVICE
# =============================================================================
class CotizadorPDFService(metaclass=SingletonMeta):
    def next_cotizacion_numero(self) -> int:
        conn = get_db_connection()
        try:
            cur = conn.cursor(dictionary=True)
            conn.start_transaction()

            CotizadorHelperFactory.asegurar_fila_seq(cur)

            cur.execute("SELECT next_num FROM cotizacion_seq WHERE id = 1 FOR UPDATE")
            row = cur.fetchone()

            if not row or row.get("next_num") is None:
                raise RuntimeError("cotizacion_seq sin next_num valido (fila id=1).")

            current = int(row["next_num"])
            cur.execute("UPDATE cotizacion_seq SET next_num = %s WHERE id = 1", (current + 1,))
            conn.commit()
            return current

        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def insertar_historial_cotizacion(
        self,
        *,
        cliente: str,
        terminal: str,
        numero_cotizacion: str,
        monto_total: int,
        archivo_url: str,
        descripcion: str = "",
        empresa_id: Optional[int] = None,
        terminal_id: Optional[int] = None,
    ) -> None:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO cotizacion_seg
                      (cliente, empresa_id, terminal, terminal_id, descripcion, monto, archivo_url, estado, numero_cotizacion)
                    VALUES
                      (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        cliente,
                        empresa_id,
                        terminal,
                        terminal_id,
                        descripcion,
                        int(monto_total),
                        archivo_url,
                        "Emitida",
                        numero_cotizacion,
                    ),
                )
            except Exception:
                cur.execute(
                    """
                    INSERT INTO cotizacion_seg (cliente, terminal, descripcion, monto, archivo_url, estado, numero_cotizacion)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (cliente, terminal, descripcion, int(monto_total), archivo_url, "Emitida", numero_cotizacion),
                )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def generar_pdf(self, data: SolicitudCotizacion) -> FileResponse:
        conn = None
        try:
            if not data.items:
                raise HTTPException(status_code=400, detail="Debe enviar al menos 1 item.")

            conn = get_db_connection()
            cur = conn.cursor(dictionary=True)

            empresa = CotizadorEmpresaFactory.find_empresa_by_codigo_or_nombre(cur, data.empresa)
            if not empresa:
                raise HTTPException(status_code=404, detail=f"Empresa no encontrada: {data.empresa}")

            empresa_id = int(empresa.get("id"))
            empresa_nombre = str(empresa.get("nombre_fantasia") or data.empresa)
            empresa_codigo = str(empresa.get("codigo") or "").strip().upper()

            modo_iva = (empresa.get("modo_iva") or "").strip().upper()
            iva_incluido = modo_iva == "IVA_INCLUIDO"

            if empresa_codigo in CotizadorContractRegistry().iva_siempre_incluido_codigos:
                iva_incluido = True

            terminal_in = (data.terminal or "").strip()
            if empresa_codigo == "RBU":
                if not terminal_in:
                    terminal_in = CotizadorContractRegistry().rbu_terminal_dummy
            else:
                if not terminal_in:
                    raise HTTPException(status_code=400, detail="Debe seleccionar una terminal.")

            nro = self.next_cotizacion_numero()
            nro_txt = f"{nro:04d}"

            tabla: List[List[Any]] = []
            if iva_incluido:
                tabla.append(["CANT", "DETALLE", "PRECIO UNITARIO (IVA incl.)", "TOTAL (IVA incl.)"])
            else:
                tabla.append(["CANT", "DETALLE", "PRECIO NETO", "TOTAL NETO"])

            total_general = 0
            for item in data.items:
                cant = int(item.cantidad)
                base = int(item.precio_base_db)

                if cant <= 0:
                    raise HTTPException(status_code=400, detail="Cantidad invalida (<=0).")

                precio_unit = int(round(base * 1.19)) if iva_incluido else base
                total_fila = precio_unit * cant
                total_general += total_fila

                tabla.append([
                    cant,
                    item.descripcion,
                    CotizadorHelperFactory.money_clp(precio_unit),
                    CotizadorHelperFactory.money_clp(total_fila),
                ])

            if iva_incluido:
                total_final = int(total_general)
            else:
                neto = int(total_general)
                iva = int(round(neto * 0.19))
                total_final = int(neto + iva)

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_emp_for_file = empresa_codigo or empresa_nombre
            safe_emp = (
                "".join(ch for ch in base_emp_for_file if ch.isalnum() or ch in (" ", "_", "-"))
                .strip()
                .replace(" ", "_")
            )
            nombre_archivo = f"COT_{safe_emp}_{nro_txt}_{stamp}.pdf"
            ruta_storage = os.path.join(STORAGE_COT, nombre_archivo)
            archivo_url = f"/storage/cotizaciones/{nombre_archivo}"

            c = canvas.Canvas(ruta_storage, pagesize=LETTER)
            w, h = LETTER
            color_corp = colors.HexColor("#003366")

            c.setFillColor(color_corp)
            c.setFont("Helvetica-Bold", 20)
            c.drawString(2 * cm, h - 2 * cm, "BY METAL SPA")

            c.setStrokeColor(color_corp)
            c.setLineWidth(2)
            c.line(2 * cm, h - 2.5 * cm, w - 2 * cm, h - 2.5 * cm)

            c.setFillColor(colors.black)
            c.setFont("Helvetica", 10)
            c.drawString(2 * cm, h - 3.3 * cm, "GIRO: MANTENCION Y REPARACION DE VEHICULOS")
            c.drawString(2 * cm, h - 3.8 * cm, "EMAIL: contacto@bymetal.cl")

            c.setFont("Helvetica-Bold", 14)
            c.drawRightString(w - 2 * cm, h - 2 * cm, f"COTIZACION Ndeg {nro_txt}")

            c.rect(2 * cm, h - 6.7 * cm, w - 4 * cm, 2.2 * cm)
            c.setFont("Helvetica-Bold", 11)
            c.drawString(2.5 * cm, h - 5.5 * cm, f"CLIENTE: {empresa_nombre}")
            c.drawString(2.5 * cm, h - 6.2 * cm, f"TERMINAL: {terminal_in}")
            c.drawRightString(w - 2.5 * cm, h - 5.5 * cm, f"FECHA: {datetime.now().strftime('%d/%m/%Y')}")

            tabla_pdf = Table(tabla, colWidths=[2 * cm, 10 * cm, 3.5 * cm, 3.5 * cm])
            tabla_pdf.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), color_corp),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("ALIGN", (0, 0), (0, -1), "CENTER"),
                        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ]
                )
            )

            tw, th = tabla_pdf.wrap(w - 4 * cm, h)
            tabla_pdf.drawOn(c, 2 * cm, h - 8 * cm - th)

            y_total = h - 8 * cm - th - 1 * cm

            if iva_incluido:
                c.setFont("Helvetica-Bold", 13)
                c.drawRightString(w - 2 * cm, y_total, f"TOTAL: {CotizadorHelperFactory.money_clp(total_general)}")
                c.setFont("Helvetica", 9)
                c.drawRightString(w - 2 * cm, y_total - 0.5 * cm, "(IVA INCLUIDO EN PRECIOS)")
            else:
                neto = total_general
                iva = int(round(neto * 0.19))
                total = neto + iva

                c.setFont("Helvetica", 11)
                c.drawRightString(w - 2 * cm, y_total, f"NETO: {CotizadorHelperFactory.money_clp(neto)}")
                c.drawRightString(w - 2 * cm, y_total - 0.6 * cm, f"IVA (19%): {CotizadorHelperFactory.money_clp(iva)}")
                c.setFont("Helvetica-Bold", 13)
                c.drawRightString(w - 2 * cm, y_total - 1.4 * cm, f"TOTAL: {CotizadorHelperFactory.money_clp(total)}")

            c.save()

            cliente_hist = CotizadorContractRegistry().normalizar_cliente_historial(empresa_codigo or empresa_nombre)
            resumen = f"COT {nro_txt} - {cliente_hist} - {terminal_in}"

            terminal_id = None
            try:
                terminal_id = CotizadorTerminalFactory.get_terminal_id(cur, empresa_id=empresa_id, terminal_nombre=terminal_in)
            except Exception:
                terminal_id = None

            self.insertar_historial_cotizacion(
                cliente=cliente_hist,
                terminal=terminal_in,
                numero_cotizacion=nro_txt,
                monto_total=total_final,
                archivo_url=archivo_url,
                descripcion=resumen,
                empresa_id=empresa_id,
                terminal_id=terminal_id,
            )

            return FileResponse(ruta_storage, media_type="application/pdf", filename=nombre_archivo)

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error al generar PDF: {str(e)}")
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass


# =============================================================================
# SERVICE
# =============================================================================
class CotizadorRouteService(metaclass=SingletonMeta):
    def listar_empresas(self) -> Dict[str, Any]:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(dictionary=True)
            cur.execute(
                """
                SELECT id, nombre_fantasia, rut, codigo, pendiente_config, modo_iva
                FROM empresas
                ORDER BY id ASC
                """
            )
            rows = cur.fetchall() or []

            empresas = []
            for r in rows:
                code = (r.get("codigo") or "").strip().upper()
                requiere_terminal = 0 if code == "RBU" else 1
                iva_forzado = 1 if code in CotizadorContractRegistry().iva_siempre_incluido_codigos else 0

                empresas.append(
                    {
                        "id": r.get("id"),
                        "codigo": r.get("codigo"),
                        "nombre_fantasia": r.get("nombre_fantasia"),
                        "rut": r.get("rut"),
                        "pendiente_config": int(r.get("pendiente_config") or 0),
                        "requiere_terminal": int(requiere_terminal),
                        "modo_iva": r.get("modo_iva"),
                        "iva_siempre_incluido": iva_forzado,
                    }
                )

            return CotizadorResponseFactory.empresas(empresas)

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error al listar empresas: {str(e)}")
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def listar_terminales(
        self,
        empresa_codigo: Optional[str],
        empresa: Optional[str],
    ) -> Dict[str, Any]:
        conn = None
        try:
            code = (empresa_codigo or empresa or "").strip().upper()
            if not code:
                return CotizadorResponseFactory.terminales([])

            if code == "RBU" and not CotizadorContractRegistry().show_rbu_terminales:
                return CotizadorResponseFactory.terminales([{"id": None, "nombre": CotizadorContractRegistry().rbu_terminal_dummy}])

            conn = get_db_connection()
            cur = conn.cursor(dictionary=True)

            cur.execute("SELECT id FROM empresas WHERE UPPER(codigo)=UPPER(%s) LIMIT 1", (code,))
            emp = cur.fetchone()
            if not emp:
                return CotizadorResponseFactory.terminales([])

            empresa_id = int(emp["id"])

            try:
                cur.execute(
                    """
                    SELECT id, nombre
                    FROM terminales
                    WHERE empresa_id = %s
                      AND (activo = 1 OR activo IS NULL)
                    ORDER BY nombre ASC
                    """,
                    (empresa_id,),
                )
                rows = cur.fetchall() or []
                terminales = [{"id": r.get("id"), "nombre": r.get("nombre")} for r in rows]
            except Exception:
                terminales = []

            if code == "RBU" and not terminales:
                terminales = [{"id": None, "nombre": CotizadorContractRegistry().rbu_terminal_dummy}]

            return CotizadorResponseFactory.terminales(terminales)

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error al listar terminales: {str(e)}")
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def cotizador_categorias(
        self,
        empresa_id: Optional[int],
        empresa_codigo: Optional[str],
        empresa: Optional[str],
    ) -> Dict[str, Any]:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(dictionary=True)

            eid = CotizadorHelperFactory.resolve_empresa_id(cur, empresa_id=empresa_id, empresa_codigo=empresa_codigo, empresa=empresa)
            if not eid:
                return CotizadorResponseFactory.categorias(None, [])

            categorias: List[str] = []

            if CotizadorHelperFactory.table_exists(cur, "cotizador_categorias"):
                cur.execute(
                    """
                    SELECT nombre
                    FROM cotizador_categorias
                    WHERE empresa_id=%s AND activo=1
                    ORDER BY orden ASC, nombre ASC
                    """,
                    (eid,),
                )
                rows = cur.fetchall() or []
                categorias = [str(r.get("nombre") or "").strip() for r in rows if (r.get("nombre") or "").strip()]

            if not categorias:
                cur.execute(
                    """
                    SELECT categoria AS nombre
                    FROM catalogo
                    WHERE empresa_id=%s
                      AND categoria IS NOT NULL AND categoria <> ''
                    GROUP BY categoria
                    ORDER BY categoria ASC
                    """,
                    (eid,),
                )
                rows = cur.fetchall() or []
                categorias = [str(r.get("nombre") or "").strip() for r in rows if (r.get("nombre") or "").strip()]

            return CotizadorResponseFactory.categorias(int(eid), categorias)

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error al listar categorias: {str(e)}")
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def cotizador_items(
        self,
        empresa_id: Optional[int],
        empresa_codigo: Optional[str],
        empresa: Optional[str],
        categoria: str,
    ) -> Dict[str, Any]:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(dictionary=True)

            eid = CotizadorHelperFactory.resolve_empresa_id(cur, empresa_id=empresa_id, empresa_codigo=empresa_codigo, empresa=empresa)
            if not eid:
                return CotizadorResponseFactory.items(None, categoria, [])

            cat = (categoria or "").strip()
            if not cat:
                return CotizadorResponseFactory.items(int(eid), "", [])

            cur.execute(
                """
                SELECT
                  id,
                  empresa_id,
                  categoria,
                  descripcion,
                  descripcion_doc,
                  precio,
                  archivo_url,
                  imagen_url,
                  pdf_url
                FROM catalogo
                WHERE empresa_id=%s AND categoria=%s
                ORDER BY descripcion ASC, id ASC
                """,
                (eid, cat),
            )
            items = cur.fetchall() or []

            out_items: List[Dict[str, Any]] = []
            for r in items:
                out_items.append(
                    {
                        "id": r.get("id"),
                        "empresa_id": r.get("empresa_id"),
                        "categoria": r.get("categoria"),
                        "descripcion": r.get("descripcion"),
                        "descripcion_doc": r.get("descripcion_doc"),
                        "precio": int(r.get("precio") or 0),
                        "archivo_url": CotizadorHelperFactory.normalize_optional_url(r.get("archivo_url")),
                        "imagen_url": CotizadorHelperFactory.normalize_optional_url(r.get("imagen_url")),
                        "pdf_url": CotizadorHelperFactory.normalize_optional_url(r.get("pdf_url")),
                    }
                )

            return CotizadorResponseFactory.items(int(eid), cat, out_items)

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error al listar items: {str(e)}")
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def obtener_info_sistema(self) -> Dict[str, Any]:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(dictionary=True)

            cur.execute("SELECT * FROM empresas")
            empresas_raw = cur.fetchall() or []

            cur.execute("SELECT * FROM catalogo")
            catalogo_raw = cur.fetchall() or []

            terminales_map: Dict[int, List[str]] = {}
            try:
                cur.execute("SELECT id, empresa_id, nombre, activo FROM terminales")
                terms = cur.fetchall() or []
                for t in terms:
                    if t.get("activo") is not None and int(t.get("activo") or 0) != 1:
                        continue
                    eid = int(t.get("empresa_id"))
                    terminales_map.setdefault(eid, []).append((t.get("nombre") or "").strip())
                for eid in terminales_map:
                    terminales_map[eid] = sorted([x for x in terminales_map[eid] if x])
            except Exception:
                terminales_map = {}

            empresas: Dict[str, Any] = {}
            empresa_id_to_nombre: Dict[int, str] = {}
            empresa_id_to_codigo: Dict[int, str] = {}

            for emp in empresas_raw:
                emp_id = int(emp.get("id"))
                codigo = (emp.get("codigo") or "").strip().upper()
                nombre_fantasia = (emp.get("nombre_fantasia") or "").strip()

                empresa_id_to_nombre[emp_id] = nombre_fantasia
                empresa_id_to_codigo[emp_id] = codigo

                if terminales_map:
                    terms = terminales_map.get(emp_id, [])
                else:
                    terms = [t.strip() for t in (emp.get("terminales") or "").split(",") if t.strip()]

                if codigo == "RBU" and not CotizadorContractRegistry().show_rbu_terminales:
                    terms = [CotizadorContractRegistry().rbu_terminal_dummy]

                empresas[nombre_fantasia] = {
                    "rut": emp.get("rut"),
                    "direccion": emp.get("direccion"),
                    "modo_iva": emp.get("modo_iva"),
                    "terminales": terms,
                    "codigo": emp.get("codigo"),
                }

            catalogo: Dict[str, Dict[str, int]] = {}
            catalogo_por_cliente: Dict[str, Dict[str, Dict[str, int]]] = {}

            def _add_catalog_item(bucket_map, empresa_key, categoria, descripcion, precio):
                if not empresa_key:
                    return
                bucket_map.setdefault(empresa_key, {})
                bucket_map[empresa_key].setdefault(categoria, {})
                bucket_map[empresa_key][categoria][descripcion] = precio

            for item in catalogo_raw:
                categoria = (item.get("categoria") or "Sin categoria")
                descripcion = (item.get("descripcion") or "")
                precio = int(item.get("precio") or 0)

                if categoria not in catalogo:
                    catalogo[categoria] = {}
                if descripcion:
                    catalogo[categoria][descripcion] = precio

                # Intento de separación por empresa sin romper compatibilidad
                empresa_keys = set()

                empresa_id = item.get("empresa_id")
                if empresa_id is not None:
                    try:
                        eid = int(empresa_id)
                        if eid in empresa_id_to_nombre:
                            empresa_keys.add(empresa_id_to_nombre[eid])
                        if eid in empresa_id_to_codigo:
                            empresa_keys.add(empresa_id_to_codigo[eid])
                    except Exception:
                        pass

                for key_name in ("empresa_nombre", "nombre_fantasia", "empresa", "cliente", "empresa_codigo", "codigo"):
                    val = (item.get(key_name) or "").strip() if isinstance(item.get(key_name), str) else item.get(key_name)
                    if isinstance(val, str) and val.strip():
                        empresa_keys.add(val.strip())

                for empresa_key in empresa_keys:
                    _add_catalog_item(catalogo_por_cliente, empresa_key, categoria, descripcion, precio)


            return CotizadorResponseFactory.info_sistema(empresas, catalogo, catalogo_por_cliente)

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error al obtener info del sistema: {str(e)}")
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass


# =============================================================================
# ENDPOINTS BD (para cotizador.js)
# =============================================================================
@router.get("/empresas")
def listar_empresas():
    return CotizadorRouteService().listar_empresas()


@router.get("/terminales")
def listar_terminales(
    empresa_codigo: Optional[str] = Query(None, description="Codigo empresa"),
    empresa: Optional[str] = Query(None, description="Alias legacy"),
):
    return CotizadorRouteService().listar_terminales(empresa_codigo=empresa_codigo, empresa=empresa)


@router.get("/cotizador/terminales")
def listar_terminales_alias(
    empresa_codigo: Optional[str] = Query(None),
    empresa: Optional[str] = Query(None),
):
    return CotizadorRouteService().listar_terminales(empresa_codigo=empresa_codigo, empresa=empresa)


@router.get("/cotizador/categorias")
def cotizador_categorias(
    empresa_id: Optional[int] = Query(None, description="ID empresa"),
    empresa_codigo: Optional[str] = Query(None, description="Codigo empresa"),
    empresa: Optional[str] = Query(None, description="Alias compat"),
):
    return CotizadorRouteService().cotizador_categorias(
        empresa_id=empresa_id,
        empresa_codigo=empresa_codigo,
        empresa=empresa,
    )


@router.get("/cotizador/items")
def cotizador_items(
    empresa_id: Optional[int] = Query(None, description="ID empresa"),
    empresa_codigo: Optional[str] = Query(None, description="Codigo empresa"),
    empresa: Optional[str] = Query(None, description="Alias compat"),
    categoria: str = Query(..., min_length=1, description="Nombre categoria"),
):
    return CotizadorRouteService().cotizador_items(
        empresa_id=empresa_id,
        empresa_codigo=empresa_codigo,
        empresa=empresa,
        categoria=categoria,
    )


@router.get("/info-sistema")
def obtener_info_sistema():
    return CotizadorRouteService().obtener_info_sistema()


# =============================================================================
# GENERAR PDF COTIZACION + GUARDAR HISTORIAL
# =============================================================================
@router.post("/generar-pdf")
def generar_pdf_cotizacion(data: SolicitudCotizacion):
    return CotizadorPDFService().generar_pdf(data)


# === BYMETAL_PDF_RBU_FAMILY_PATCH ===
try:
    _ByMetalOriginalCotizadorPDFService = CotizadorPDFService
except NameError:
    _ByMetalOriginalCotizadorPDFService = None


if _ByMetalOriginalCotizadorPDFService is not None:
    class CotizadorPDFService(_ByMetalOriginalCotizadorPDFService):
        _RBU_FAMILY = {"RBU", "METBUS", "GRAN_AMERICA", "GRAN AMERICA", "CONECTA", "SUBUS"}

        _CONTACTO_EMPRESA = {
            "razon_social": "BY METAL SPA",
            "giro": "MANTENCION TRANSPORTE PUBLICO Y REPARACION DE VEHICULOS",
            "rut": "77.618.985-5",
            "direccion": "AV LO OVALLE 311 - SAN JOAQUIN",
            "email": "BYMETALSPA@GMAIL.COM",
            "telefono": "+56982883945",
            "tipo_venta": "DEL GIRO",
            "firma_nombre": "Eduardo Onetto.",
        }

        def _norm_empresa_patch(self, value):
            return str(value or "").strip().upper().replace("Á", "A").replace("_", " ")

        def _cliente_desde_empresa_patch(self, value):
            v = self._norm_empresa_patch(value)
            if v == "RBU":
                return "TRANSPORTES RBU"
            if v == "METBUS":
                return "METBUS"
            if v == "GRAN AMERICA":
                return "GRAN AMERICA"
            if v == "CONECTA":
                return "CONECTA"
            if v == "SUBUS":
                return "SUBUS"
            return str(value or "").strip()

        def _is_rbu_family_patch(self, value):
            return self._norm_empresa_patch(value) in self._RBU_FAMILY


        def _slug_filename_part(self, value):
            import re
            s = str(value or "").strip()
            s = (
                s.replace("á", "a").replace("é", "e").replace("í", "i")
                 .replace("ó", "o").replace("ú", "u").replace("Á", "A")
                 .replace("É", "E").replace("Í", "I").replace("Ó", "O")
                 .replace("Ú", "U").replace("ñ", "n").replace("Ñ", "N")
            )
            s = re.sub(r"\s+", " ", s).strip()
            s = re.sub(r'[\\/:*?"<>|]', "", s)
            return s

        def _money_patch(self, n):
            try:
                return "${:,.0f}".format(float(n)).replace(",", ".")
            except Exception:
                return "$0"

        def _safe_num_patch(self, v):
            try:
                if v is None:
                    return 0
                if isinstance(v, str):
                    vv = v.replace(".", "").replace(",", "").strip()
                    return int(vv or "0")
                return int(v)
            except Exception:
                return 0

        def _build_rbu_family_pdf_patch(self, data):
            import os
            import tempfile
            from datetime import datetime
            from pathlib import Path
            from fastapi.responses import FileResponse
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas

            now = datetime.now()
            numero = None
            try:
                numero = getattr(data, "numero", None) or getattr(data, "correlativo", None)
            except Exception:
                numero = None
            if not numero:
                numero = now.strftime("%H%M")

            empresa = str(getattr(data, "empresa", "") or "").strip()
            terminal = str(getattr(data, "terminal", "") or "").strip()
            cliente = self._cliente_desde_empresa_patch(empresa)
            items = list(getattr(data, "items", []) or [])

            cinfo = self._CONTACTO_EMPRESA

            out_dir = Path(tempfile.gettempdir()) / "bymetal_pdf_cotizaciones"
            out_dir.mkdir(parents=True, exist_ok=True)

            terminal_fname = self._slug_filename_part(terminal)
            empresa_fname = self._slug_filename_part(empresa)

            if terminal_fname:
                fname = f"COTIZACION {numero} TERMINAL {terminal_fname}.pdf"
            else:
                fname = f"COTIZACION {numero} {empresa_fname}.pdf"
            out_path = out_dir / fname

            c = canvas.Canvas(str(out_path), pagesize=A4)
            width, height = A4

            navy = colors.HexColor("#0b3f78")
            black = colors.black
            gray = colors.HexColor("#666666")

            y = height - 55

            # Encabezado principal
            c.setFillColor(navy)
            c.setFont("Helvetica-Bold", 22)
            c.drawString(55, y, cinfo["razon_social"])

            c.setFillColor(black)
            c.setFont("Helvetica-Bold", 16)
            c.drawRightString(width - 55, y, f"COTIZACIÓN N° {numero}")

            c.setStrokeColor(navy)
            c.setLineWidth(2)
            c.line(55, y - 18, width - 55, y - 18)

            y -= 55
            c.setFillColor(black)
            c.setFont("Helvetica", 10.5)
            c.drawString(55, y, f"GIRO: {cinfo['giro']}")
            y -= 16
            c.drawString(55, y, f"RUT: {cinfo['rut']}")
            y -= 16
            c.drawString(55, y, f"DIRECCION: {cinfo['direccion']}")
            y -= 16
            c.drawString(55, y, f"EMAIL: {cinfo['email']}")
            y -= 16
            c.drawString(55, y, f"TELEFONO: {cinfo['telefono']}")
            y -= 16
            c.drawString(55, y, f"TIPO DE VENTA: {cinfo['tipo_venta']}")

            # Caja cliente / terminal / fecha
            box_top = y - 24
            box_h = 78
            c.setStrokeColor(navy)
            c.setLineWidth(2)
            c.rect(55, box_top - box_h, width - 110, box_h, stroke=1, fill=0)

            c.setFillColor(black)
            c.setFont("Helvetica-Bold", 11)
            c.drawString(72, box_top - 22, f"CLIENTE: {cliente}")
            c.drawString(72, box_top - 46, f"TERMINAL: {terminal or empresa}")
            c.drawRightString(width - 72, box_top - 22, f"FECHA: {now.strftime('%d/%m/%Y')}")

            y = box_top - box_h - 42

            # Tabla
            table_x = 55
            table_w = width - 110
            row_h = 24
            col_w = [55, 255, 85, 90]

            headers = ["CANT", "DETALLE", "PRECIO NETO", "TOTAL NETO"]

            c.setFillColor(navy)
            c.rect(table_x, y - row_h, table_w, row_h, stroke=0, fill=1)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 10)
            x = table_x
            for idx, htxt in enumerate(headers):
                c.drawString(x + 8, y - 16, htxt)
                x += col_w[idx]

            y -= row_h

            neto = 0
            c.setStrokeColor(colors.HexColor("#999999"))
            c.setLineWidth(0.7)
            c.setFont("Helvetica", 10)
            c.setFillColor(black)

            if not items:
                items = [{"descripcion": "SIN ITEMS", "cantidad": 1, "precio_base_db": 0}]

            def _split_text_lines(pdf_canvas, text, max_width, font_name="Helvetica", font_size=10):
                text = str(text or "").strip()
                if not text:
                    return [""]

                words = text.split()
                if not words:
                    return [""]

                lines = []
                current = words[0]

                for word in words[1:]:
                    trial = current + " " + word
                    if pdf_canvas.stringWidth(trial, font_name, font_size) <= max_width:
                        current = trial
                    else:
                        lines.append(current)
                        current = word

                if current:
                    lines.append(current)

                return lines or [""]

            for raw in items:
                try:
                    desc = str(getattr(raw, "descripcion", None) if not isinstance(raw, dict) else raw.get("descripcion", "")).strip()
                    cant = self._safe_num_patch(getattr(raw, "cantidad", None) if not isinstance(raw, dict) else raw.get("cantidad", 0))
                    precio = self._safe_num_patch(getattr(raw, "precio_base_db", None) if not isinstance(raw, dict) else raw.get("precio_base_db", 0))
                except Exception:
                    desc, cant, precio = "", 0, 0

                if cant <= 0:
                    cant = 1

                total_linea = cant * precio
                neto += total_linea

                detail_lines = _split_text_lines(
                    c,
                    desc,
                    col_w[1] - 16,
                    font_name="Helvetica",
                    font_size=10
                )

                line_height = 12
                padding_y = 8
                dynamic_row_h = max(row_h, padding_y + (len(detail_lines) * line_height) + padding_y)

                if y - dynamic_row_h < 220:
                    c.showPage()
                    y = height - 70

                    c.setFillColor(navy)
                    c.rect(table_x, y - row_h, table_w, row_h, stroke=0, fill=1)
                    c.setFillColor(colors.white)
                    c.setFont("Helvetica-Bold", 10)
                    x = table_x
                    for idx, htxt in enumerate(headers):
                        c.drawString(x + 8, y - 16, htxt)
                        x += col_w[idx]
                    y -= row_h

                    c.setStrokeColor(colors.HexColor("#999999"))
                    c.setLineWidth(0.7)
                    c.setFont("Helvetica", 10)
                    c.setFillColor(black)

                c.rect(table_x, y - dynamic_row_h, table_w, dynamic_row_h, stroke=1, fill=0)

                xx = table_x
                for w in col_w[:-1]:
                    xx += w
                    c.line(xx, y, xx, y - dynamic_row_h)

                # cantidad centrada verticalmente
                mid_y = y - (dynamic_row_h / 2) - 3
                c.drawCentredString(table_x + col_w[0] / 2, mid_y, str(cant))

                # detalle multilínea
                text_y = y - 15
                for line in detail_lines:
                    c.drawString(table_x + col_w[0] + 8, text_y, line)
                    text_y -= line_height

                # precio y total alineados arriba
                c.drawRightString(table_x + col_w[0] + col_w[1] + col_w[2] - 8, y - 15, self._money_patch(precio))
                c.drawRightString(table_x + table_w - 8, y - 15, self._money_patch(total_linea))

                y -= dynamic_row_h

            iva = int(round(neto * 0.19))
            total = neto + iva

            # Totales en cuadro
            y -= 22

            total_box_w = 210
            total_label_w = 110
            total_value_w = total_box_w - total_label_w
            total_row_h = 24
            total_x = width - 55 - total_box_w
            total_y = y

            c.setStrokeColor(colors.HexColor("#999999"))
            c.setLineWidth(0.8)

            totals_rows = [
                ("NETO", self._money_patch(neto), False),
                ("IVA (19%)", self._money_patch(iva), False),
                ("TOTAL", self._money_patch(total), True),
            ]

            for label, value, is_total in totals_rows:
                c.rect(total_x, total_y - total_row_h, total_box_w, total_row_h, stroke=1, fill=0)
                c.line(total_x + total_label_w, total_y, total_x + total_label_w, total_y - total_row_h)

                c.setFillColor(black)
                c.setFont("Helvetica-Bold" if is_total else "Helvetica", 11 if is_total else 10.5)
                c.drawString(total_x + 8, total_y - 16, label)

                c.setFont("Helvetica-Bold" if is_total else "Helvetica", 12 if is_total else 10.5)
                c.drawRightString(total_x + total_box_w - 8, total_y - 16, value)

                total_y -= total_row_h

            y = total_y

            # Condiciones comerciales
            y -= 70
            c.setFont("Helvetica-Bold", 11)
            c.drawString(110, y, "Condiciones Comerciales")
            y -= 28

            c.setFont("Helvetica", 10.5)
            c.drawString(110, y, "Plazo de entrega: 5 Días, previo recepción OC.")
            y -= 26
            empresa_norm = self._norm_empresa_patch(empresa)
            if empresa_norm == "RBU":
                empresa_lugar = "RED BUS"
            elif empresa_norm == "METBUS":
                empresa_lugar = "METBUS"
            elif empresa_norm == "GRAN AMERICA":
                empresa_lugar = "GRAN AMERICA"
            elif empresa_norm == "CONECTA":
                empresa_lugar = "CONECTA"
            elif empresa_norm == "SUBUS":
                empresa_lugar = "SUBUS"
            else:
                empresa_lugar = empresa_norm or empresa

            c.drawString(110, y, f"Lugar de entrega: En instalaciones de empresa {empresa_lugar} {terminal or ''}".rstrip() + ".")
            y -= 26
            c.drawString(110, y, "Esperamos que la presente Oferta, cumpla con sus expectativas y quedamos atentos a sus")
            y -= 15
            c.drawString(110, y, "comentarios.")
            y -= 55
            c.drawString(110, y, "Cordialmente,")
            y -= 28
            c.setFont("Helvetica-Bold", 10.5)
            c.drawString(110, y, cinfo["firma_nombre"])

            c.save()

            return FileResponse(
                path=str(out_path),
                media_type="application/pdf",
                filename=fname,
            )


        def _is_metropol_patch(self, value):
            return self._norm_empresa_patch(value) == "METROPOL"


        def _metropol_email_by_terminal(self, terminal):
            t = str(terminal or "").strip().lower()
            t_norm = (
                t.replace("á", "a").replace("é", "e").replace("í", "i")
                 .replace("ó", "o").replace("ú", "u").replace("ñ", "n")
            )

            mapping = {
                "aguirre luco": "bodega.aguirreluco@grupometropol.cl",
                "las torres": "bodega.aguirreluco@grupometropol.cl",
                "condell": "bodega.condell@grupometropol.cl",
                "juanita": "bodega.juanita@grupometropol.cl",
                "pie andino": "bodega.pieandino@grupometropol.cl",
                "el retiro": "bodega.elretiro@grupometropol.cl",
                "santa marta": "bodega.santamarta@grupometropol.cl",
            }

            return mapping.get(t_norm, "")


        def _safe_text_patch(self, value):
            return str(value or "").strip()

        def _split_lines_metropol(self, pdf_canvas, text, max_width, font_name="Helvetica", font_size=8.5):
            text = str(text or "").strip()
            if not text:
                return [""]

            words = text.split()
            if not words:
                return [""]

            lines = []
            current = words[0]

            for word in words[1:]:
                trial = current + " " + word
                if pdf_canvas.stringWidth(trial, font_name, font_size) <= max_width:
                    current = trial
                else:
                    lines.append(current)
                    current = word

            if current:
                lines.append(current)

            return lines or [""]

        def _draw_cell_text(self, c, x, y, w, h, text, align="left", font="Helvetica", size=8.5, bold=False, pad=5):
            c.setFont("Helvetica-Bold" if bold else font, size)
            txt = str(text or "").strip()
            yy = y - (h / 2) - 3
            if align == "center":
                c.drawCentredString(x + (w / 2), yy, txt)
            elif align == "right":
                c.drawRightString(x + w - pad, yy, txt)
            else:
                c.drawString(x + pad, yy, txt)

        def _draw_pair_table(self, c, x, y, total_w, row_h, rows, header_text, header_fill):
            from reportlab.lib import colors
            grid = colors.HexColor("#6b6b6b")
            c.setStrokeColor(grid)
            c.setLineWidth(1)

            header_h = 22
            c.setFillColor(header_fill)
            c.rect(x, y - header_h, total_w, header_h, stroke=1, fill=1)
            c.setFillColor(colors.black)
            c.setFont("Helvetica-Bold", 10)
            c.drawCentredString(x + total_w / 2, y - 15, header_text)

            y -= header_h

            # 4 columnas: label1, value1, label2, value2
            col_fracs = [0.16, 0.34, 0.16, 0.34]
            col_ws = [total_w * f for f in col_fracs]

            for row in rows:
                c.rect(x, y - row_h, total_w, row_h, stroke=1, fill=0)

                xx = x
                for w in col_ws[:-1]:
                    xx += w
                    c.line(xx, y, xx, y - row_h)

                cells = [row[0], row[1], row[2], row[3]]
                xx = x
                for i, txt in enumerate(cells):
                    bold = (i in (0, 2))
                    self._draw_cell_text(
                        c, xx, y, col_ws[i], row_h, txt,
                        align="left", size=8.5, bold=bold, pad=5
                    )
                    xx += col_ws[i]

                y -= row_h

            return y

        def _build_metropol_pdf_patch(self, data):
            import tempfile
            from datetime import datetime
            from pathlib import Path
            from fastapi.responses import FileResponse
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas

            now = datetime.now()
            numero = None
            try:
                numero = getattr(data, "numero", None) or getattr(data, "correlativo", None)
            except Exception:
                numero = None
            if not numero:
                numero = now.strftime("%H%M")

            empresa = self._safe_text_patch(getattr(data, "empresa", "") or "")
            terminal = self._safe_text_patch(getattr(data, "terminal", "") or "")
            items = list(getattr(data, "items", []) or [])

            out_dir = Path(tempfile.gettempdir()) / "bymetal_pdf_cotizaciones"
            out_dir.mkdir(parents=True, exist_ok=True)

            terminal_fname = self._slug_filename_part(terminal)
            empresa_fname = self._slug_filename_part(empresa)
            if terminal_fname:
                fname = f"COTIZACION {numero} TERMINAL {terminal_fname}.pdf"
            else:
                fname = f"COTIZACION {numero} {empresa_fname}.pdf"

            out_path = out_dir / fname

            c = canvas.Canvas(str(out_path), pagesize=A4)
            width, height = A4

            black = colors.black
            green = colors.HexColor("#C6E0B4")
            grid = colors.HexColor("#6b6b6b")

            left = 42
            right = width - 42
            usable_w = right - left
            y = height - 35

            # Encabezado superior
            c.setFillColor(black)
            c.setFont("Helvetica", 9.5)
            c.drawCentredString(width / 2, y, "MANTENCION TRANSPORTE PUBLICO Y REP")
            y -= 16
            c.setFont("Helvetica-Bold", 11.5)
            c.drawCentredString(width / 2, y, "BY METAL SPA")
            y -= 20

            # Datos del cliente
            metropol_email = self._metropol_email_by_terminal(terminal)

            client_rows = [
                ("Rut", "77.532.117-2", "Contacto", terminal or ""),
                ("Nombre", "METROPOL", "Email", metropol_email),
                ("Giro", "", "Teléfono", ""),
                ("Dirección", terminal or "", "Celular", ""),
            ]
            y = self._draw_pair_table(c, left, y, usable_w, 22, client_rows, "DATOS DEL CLIENTE", green)
            y -= 16

            # Título cotización
            c.setStrokeColor(grid)
            c.setLineWidth(1)
            c.setFillColor(green)
            c.rect(left, y - 22, usable_w, 22, stroke=1, fill=1)
            c.setFillColor(black)
            c.setFont("Helvetica-Bold", 10)
            c.drawCentredString(width / 2, y - 15, f"COTIZACIÓN BY METAL N° {numero} - {now.strftime('%d/%m/%Y')}")
            y -= 34

            # Tabla de ítems
            table_x = left
            table_w = usable_w
            # Item, VD, Descripción, Cant, V.Unit, V.Total
            cols = [44, 44, 238, 42, 96, 96]
            diff = table_w - sum(cols)
            cols[-1] += diff

            headers = ["Item", "V.D.", "Descripción", "Cant.", "Valor Unitario $", "Valor Total $"]
            header_h = 22

            c.setFillColor(green)
            c.rect(table_x, y - header_h, table_w, header_h, stroke=1, fill=1)
            c.setFillColor(black)
            c.setFont("Helvetica-Bold", 8.5)

            xx = table_x
            for i, htxt in enumerate(headers):
                self._draw_cell_text(c, xx, y, cols[i], header_h, htxt, align="center", size=8.3, bold=True, pad=3)
                xx += cols[i]

            y -= header_h

            c.setStrokeColor(grid)
            c.setLineWidth(1)

            if not items:
                items = [{"descripcion": "SIN ITEMS", "cantidad": 1, "precio_base_db": 0}]

            neto = 0
            idx = 1

            for raw in items:
                try:
                    desc = str(getattr(raw, "descripcion", None) if not isinstance(raw, dict) else raw.get("descripcion", "")).strip()
                    cant = self._safe_num_patch(getattr(raw, "cantidad", None) if not isinstance(raw, dict) else raw.get("cantidad", 0))
                    precio = self._safe_num_patch(getattr(raw, "precio_base_db", None) if not isinstance(raw, dict) else raw.get("precio_base_db", 0))
                except Exception:
                    desc, cant, precio = "", 0, 0

                if cant <= 0:
                    cant = 1

                total_linea = cant * precio
                neto += total_linea

                desc_lines = self._split_lines_metropol(c, desc, cols[2] - 10, font_size=8.5)
                row_h = max(24, 8 + (len(desc_lines) * 11) + 6)

                if y - row_h < 230:
                    c.showPage()
                    y = height - 40

                    c.setFillColor(green)
                    c.rect(table_x, y - header_h, table_w, header_h, stroke=1, fill=1)
                    c.setFillColor(black)
                    c.setFont("Helvetica-Bold", 8.5)
                    xx = table_x
                    for i, htxt in enumerate(headers):
                        self._draw_cell_text(c, xx, y, cols[i], header_h, htxt, align="center", size=8.3, bold=True, pad=3)
                        xx += cols[i]
                    y -= header_h
                    c.setStrokeColor(grid)
                    c.setLineWidth(1)

                c.rect(table_x, y - row_h, table_w, row_h, stroke=1, fill=0)
                xx = table_x
                for w in cols[:-1]:
                    xx += w
                    c.line(xx, y, xx, y - row_h)

                self._draw_cell_text(c, table_x, y, cols[0], row_h, idx, align="center", size=8.5)
                self._draw_cell_text(c, table_x + cols[0], y, cols[1], row_h, "", align="center", size=8.5)

                text_y = y - 13
                c.setFont("Helvetica", 8.5)
                for line in desc_lines:
                    c.drawString(table_x + cols[0] + cols[1] + 5, text_y, line)
                    text_y -= 11

                self._draw_cell_text(c, table_x + cols[0] + cols[1] + cols[2], y, cols[3], row_h, cant, align="center", size=8.5)
                self._draw_cell_text(c, table_x + cols[0] + cols[1] + cols[2] + cols[3], y, cols[4], row_h, self._money_patch(precio), align="right", size=8.5)
                self._draw_cell_text(c, table_x + cols[0] + cols[1] + cols[2] + cols[3] + cols[4], y, cols[5], row_h, self._money_patch(total_linea), align="right", size=8.5)

                y -= row_h
                idx += 1

            # Totales
            y -= 16
            iva = int(round(neto * 0.19))
            bruto = neto + iva

            total_x = right - 245
            total_w = 245
            row_h_t = 22
            totals = [
                ("Total Neto $", self._money_patch(neto)),
                ("IVA 19%", self._money_patch(iva)),
                ("Total Bruto $", self._money_patch(bruto)),
            ]

            for label, value in totals:
                c.setFillColor(green)
                c.rect(total_x, y - row_h_t, total_w * 0.52, row_h_t, stroke=1, fill=1)
                c.setFillColor(black)
                c.rect(total_x + total_w * 0.52, y - row_h_t, total_w * 0.48, row_h_t, stroke=1, fill=0)
                c.setFont("Helvetica-Bold", 8.8)
                c.drawString(total_x + 6, y - 15, label)
                c.setFont("Helvetica", 8.8)
                c.drawRightString(total_x + total_w - 6, y - 15, value)
                y -= row_h_t

            # Condiciones comerciales
            y -= 24
            c.setFont("Helvetica-Bold", 9.5)
            c.drawString(left, y, "CONDICIONES COMERCIALES")
            y -= 16
            c.setFont("Helvetica", 8.5)
            condiciones = [
                "Forma de Entrega: 24 a 48 horas.",
                "Despacho: Se incluye despacho.",
                "Forma de Pago: Transferencia bancaria crédito 30 días.",
                "Validez de la cotización: 15 días.",
                "Precios: Valores válidos para las cantidades indicadas.",
                "Garantía: 12 meses excepto accesorios.",
            ]
            for line in condiciones:
                c.drawString(left, y, line)
                y -= 13

            y -= 10

            # Datos proveedor según DOCX
            proveedor_rows = [
                ("Razón Social", "BY METAL SPA", "Rut", "77.618.985-5"),
                ("Banco", "BANCO BCI", "Cuenta Corriente", "63801043"),
                ("Correo", "BYMETALSPA@GMAIL.COM", "Cel", "+569 82883945"),
            ]
            y = self._draw_pair_table(c, left, y, usable_w, 22, proveedor_rows, "DATOS PROVEEDOR", green)

            c.save()

            return FileResponse(
                path=str(out_path),
                media_type="application/pdf",
                filename=fname,
            )

        def generar_pdf(self, data):
            empresa = str(getattr(data, "empresa", "") or "").strip()
            if self._is_metropol_patch(empresa):
                return self._build_metropol_pdf_patch(data)
            if self._is_rbu_family_patch(empresa):
                return self._build_rbu_family_pdf_patch(data)
            return super().generar_pdf(data)

# === /BYMETAL_PDF_RBU_FAMILY_PATCH ===

