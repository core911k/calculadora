from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg2 import IntegrityError
from psycopg2.extras import RealDictCursor
import psycopg2
from pydantic import BaseModel, Field

load_dotenv()

BASE_DIR = Path(__file__).parent
DATABASE_URL = os.getenv("DATABASE_URL")

app = FastAPI(title="CEC Calculator MVP API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=str(BASE_DIR)), name="static")

BANDS = {"3": 52.0, "4": 68.0, "5": 86.0, "6": 112.0, "7": 145.0, "8": 180.0, "9": 225.0, "10": 280.0}
DEFAULT_USERS = [
    (
        os.getenv("APP_USERNAME"),
        os.getenv("APP_PASSWORD"),
        os.getenv("APP_USER_NAME", "Usuario"),
        os.getenv("APP_USER_ROLE", "pricing"),
    )
]

class LoginRequest(BaseModel):
    username: str
    password: str


class ResourceInput(BaseModel):
    actividad: str = ""
    perfil: str = ""
    banda: str = "3"
    meses: List[float] = Field(default_factory=lambda: [0.0] * 24, min_length=24, max_length=24)


class OpportunityInput(BaseModel):
    codigo: str
    nombre: str
    cliente: str
    unidad: str = ""
    cierre: str = ""
    estado: str = "Draft"
    aprobado: str = "NO"
    es_inversion: bool = False
    gp_pct: float = 28.0
    contingencia_pct: float = 0.0
    poliza_pct: float = 1.0
    otros_costos_labor: float = 0.0
    otros_costos_software: float = 0.0
    recursos: List[ResourceInput] = Field(default_factory=list)
    revenue_manual: Optional[List[float]] = None


def db():
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL no está definido en el archivo .env")
    try:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error conectando a PostgreSQL/Neon: {exc}")


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
      id SERIAL PRIMARY KEY,
      username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      nombre TEXT NOT NULL,
      rol TEXT NOT NULL
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tarifas (
      banda TEXT PRIMARY KEY,
      costo_hora DOUBLE PRECISION NOT NULL
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS oportunidades (
      id SERIAL PRIMARY KEY,
      codigo TEXT UNIQUE NOT NULL,
      nombre TEXT NOT NULL,
      cliente TEXT NOT NULL,
      unidad TEXT,
      cierre TEXT,
      estado TEXT,
      aprobado TEXT,
      es_inversion BOOLEAN DEFAULT FALSE,
      gp_pct DOUBLE PRECISION,
      pti_pct DOUBLE PRECISION,
      contingencia_pct DOUBLE PRECISION,
      poliza_pct DOUBLE PRECISION,
      otros_costos_labor DOUBLE PRECISION,
      otros_costos_software DOUBLE PRECISION,
      costo_total_equipo DOUBLE PRECISION,
      horas_totales DOUBLE PRECISION,
      precio DOUBLE PRECISION,
      revenue_json JSONB,
      creado_en TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
      actualizado_en TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS recursos (
      id SERIAL PRIMARY KEY,
      oportunidad_id INTEGER NOT NULL REFERENCES oportunidades(id) ON DELETE CASCADE,
      actividad TEXT,
      perfil TEXT,
      banda TEXT REFERENCES tarifas(banda),
      meses_json JSONB NOT NULL,
      horas_totales DOUBLE PRECISION,
      costo_recurso DOUBLE PRECISION,
      costos_json JSONB NOT NULL
    );
    """)

    for username, password_hash, nombre, rol in DEFAULT_USERS:
        cur.execute(
            """
            INSERT INTO usuarios(username, password_hash, nombre, rol)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (username) DO NOTHING;
            """,
            (username, password_hash, nombre, rol),
        )

    for band, rate in BANDS.items():
        cur.execute(
            """
            INSERT INTO tarifas(banda, costo_hora)
            VALUES (%s, %s)
            ON CONFLICT (banda) DO UPDATE SET costo_hora = EXCLUDED.costo_hora;
            """,
            (band, rate),
        )

    conn.commit()
    cur.close()
    conn.close()


def calculate(payload: OpportunityInput):
    recursos_out = []
    costo_total = 0.0
    horas_totales = 0.0
    cost_by_month = [0.0] * 24

    for r in payload.recursos:
        rate = BANDS.get(r.banda)
        if rate is None:
            raise HTTPException(400, f"Banda inválida: {r.banda}")
        hours = [float(x or 0) * 160 for x in r.meses]
        costs = [h * rate for h in hours]
        recurso_horas = sum(hours)
        recurso_costo = sum(costs)
        horas_totales += recurso_horas
        costo_total += recurso_costo
        cost_by_month = [a + b for a, b in zip(cost_by_month, costs)]
        recursos_out.append({**r.model_dump(), "costo_hora": rate, "horas_totales": recurso_horas, "costo_recurso": recurso_costo, "costos_mes": costs})

    gp_factor = 1 - (payload.gp_pct / 100)
    if gp_factor <= 0:
        raise HTTPException(400, "El GP debe ser menor a 100%")

    uplift = (1 + payload.contingencia_pct / 100 + payload.poliza_pct / 100) / gp_factor
    revenue = [v * uplift for v in cost_by_month]
    if payload.revenue_manual and len(payload.revenue_manual) == 24:
        revenue = [float(x or 0) for x in payload.revenue_manual]

    price = sum(revenue) + float(payload.otros_costos_labor or 0) + float(payload.otros_costos_software or 0)
    return {
        "pti_pct": payload.gp_pct - 21.4,
        "horas_totales": horas_totales,
        "costo_total_equipo": costo_total,
        "costos_mes": cost_by_month,
        "revenue_mes": revenue,
        "precio": price,
        "recursos": recursos_out,
    }


def parse_json_value(value, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    return json.loads(value)


def serialize_row(row):
    data = dict(row)
    for key in ("creado_en", "actualizado_en"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    return data


@app.on_event("startup")
def startup():
    init_db()


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "index.html")


@app.post("/api/auth/login")
def login(data: LoginRequest):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, nombre, rol, password_hash FROM usuarios WHERE username = %s", (data.username,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if not user or user["password_hash"] != data.password:
        raise HTTPException(401, "Usuario o contraseña incorrectos")
    return {"usuario": {k: user[k] for k in ["id", "username", "nombre", "rol"]}}


@app.get("/api/tarifas")
def tarifas():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT banda, costo_hora FROM tarifas ORDER BY banda::integer")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/calcular")
def calcular(data: OpportunityInput):
    return calculate(data)


@app.get("/api/oportunidades")
def listar_oportunidades():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM oportunidades ORDER BY actualizado_en DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [serialize_row(r) for r in rows]


@app.get("/api/oportunidades/{opp_id}")
def obtener_oportunidad(opp_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM oportunidades WHERE id = %s", (opp_id,))
    opp = cur.fetchone()
    if not opp:
        cur.close()
        conn.close()
        raise HTTPException(404, "Oportunidad no encontrada")

    cur.execute("SELECT * FROM recursos WHERE oportunidad_id = %s ORDER BY id", (opp_id,))
    recursos = cur.fetchall()
    cur.close()
    conn.close()

    result = serialize_row(opp)
    result["revenue_mes"] = parse_json_value(result.pop("revenue_json"), [])
    result["recursos"] = [
        {
            **dict(r),
            "meses": parse_json_value(r["meses_json"], []),
            "costos_mes": parse_json_value(r["costos_json"], []),
        }
        for r in recursos
    ]
    return result


@app.post("/api/oportunidades")
def crear_oportunidad(data: OpportunityInput):
    calc = calculate(data)
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO oportunidades(
              codigo, nombre, cliente, unidad, cierre, estado, aprobado, es_inversion,
              gp_pct, pti_pct, contingencia_pct, poliza_pct, otros_costos_labor,
              otros_costos_software, costo_total_equipo, horas_totales, precio, revenue_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                data.codigo,
                data.nombre,
                data.cliente,
                data.unidad,
                data.cierre,
                data.estado,
                data.aprobado,
                data.es_inversion,
                data.gp_pct,
                calc["pti_pct"],
                data.contingencia_pct,
                data.poliza_pct,
                data.otros_costos_labor,
                data.otros_costos_software,
                calc["costo_total_equipo"],
                calc["horas_totales"],
                calc["precio"],
                json.dumps(calc["revenue_mes"]),
            ),
        )
        opp_id = cur.fetchone()["id"]
        for r in calc["recursos"]:
            cur.execute(
                """
                INSERT INTO recursos(
                  oportunidad_id, actividad, perfil, banda, meses_json,
                  horas_totales, costo_recurso, costos_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    opp_id,
                    r["actividad"],
                    r["perfil"],
                    r["banda"],
                    json.dumps(r["meses"]),
                    r["horas_totales"],
                    r["costo_recurso"],
                    json.dumps(r["costos_mes"]),
                ),
            )
        conn.commit()
        return {"id": opp_id, **calc}
    except IntegrityError:
        conn.rollback()
        raise HTTPException(409, "Ya existe una oportunidad con ese código")
    except Exception as exc:
        conn.rollback()
        raise HTTPException(500, f"Error creando oportunidad: {exc}")
    finally:
        cur.close()
        conn.close()


@app.put("/api/oportunidades/{opp_id}")
def actualizar_oportunidad(opp_id: int, data: OpportunityInput):
    calc = calculate(data)
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM oportunidades WHERE id = %s", (opp_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Oportunidad no encontrada")

        cur.execute(
            """
            UPDATE oportunidades SET
              codigo = %s,
              nombre = %s,
              cliente = %s,
              unidad = %s,
              cierre = %s,
              estado = %s,
              aprobado = %s,
              es_inversion = %s,
              gp_pct = %s,
              pti_pct = %s,
              contingencia_pct = %s,
              poliza_pct = %s,
              otros_costos_labor = %s,
              otros_costos_software = %s,
              costo_total_equipo = %s,
              horas_totales = %s,
              precio = %s,
              revenue_json = %s,
              actualizado_en = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (
                data.codigo,
                data.nombre,
                data.cliente,
                data.unidad,
                data.cierre,
                data.estado,
                data.aprobado,
                data.es_inversion,
                data.gp_pct,
                calc["pti_pct"],
                data.contingencia_pct,
                data.poliza_pct,
                data.otros_costos_labor,
                data.otros_costos_software,
                calc["costo_total_equipo"],
                calc["horas_totales"],
                calc["precio"],
                json.dumps(calc["revenue_mes"]),
                opp_id,
            ),
        )
        cur.execute("DELETE FROM recursos WHERE oportunidad_id = %s", (opp_id,))
        for r in calc["recursos"]:
            cur.execute(
                """
                INSERT INTO recursos(
                  oportunidad_id, actividad, perfil, banda, meses_json,
                  horas_totales, costo_recurso, costos_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    opp_id,
                    r["actividad"],
                    r["perfil"],
                    r["banda"],
                    json.dumps(r["meses"]),
                    r["horas_totales"],
                    r["costo_recurso"],
                    json.dumps(r["costos_mes"]),
                ),
            )
        conn.commit()
        return {"id": opp_id, **calc}
    except IntegrityError:
        conn.rollback()
        raise HTTPException(409, "Ya existe una oportunidad con ese código")
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(500, f"Error actualizando oportunidad: {exc}")
    finally:
        cur.close()
        conn.close()
