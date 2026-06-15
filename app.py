#!/usr/bin/env python3
"""
API REST del TSE Costa Rica — consulta en vivo del Registro Civil / Padrón.

Envuelve los sitios oficiales:
  - https://servicioselectorales.tse.go.cr/chc/consulta_cedula.aspx
  - https://servicioselectorales.tse.go.cr/chc/consulta_nombres.aspx

Estos sitios están detrás de un bot manager (Radware) que exige JS; por eso
la API conduce Chromium headless (Playwright) por dentro. Ver tse_scraper.py.

Ejecutar:
  uvicorn app:app --host 0.0.0.0 --port 8000
Endpoints:
  GET /health
  GET /cedula/{cedula}
  GET /nombres?nombre=&apellido1=&apellido2=&limite=
"""
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse

from tse_scraper import TSEScraper

scraper = TSEScraper(headless=True, max_concurrency=2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await scraper.start()
    yield
    await scraper.stop()


app = FastAPI(
    title="API TSE Costa Rica (en vivo)",
    version="2.0.0",
    description=(
        "Consulta de personas por cédula y por nombre contra los sitios oficiales "
        "del Tribunal Supremo de Elecciones de Costa Rica (Registro Civil). "
        "Datos en tiempo real: fecha de nacimiento, edad, nacionalidad, padre y madre, "
        "estado de fallecimiento, etc."
    ),
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/docs")


@app.get("/health", summary="Estado del servicio y del bot wall")
async def health():
    try:
        return await scraper.health()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Scraper no disponible: {e}")


@app.get("/cedula/{cedula}", summary="Consulta una persona por número de cédula")
async def consulta_cedula(cedula: str):
    """Equivale a consulta_cedula.aspx → resultado_persona.aspx."""
    try:
        data = await scraper.consulta_cedula(cedula)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error consultando TSE: {e}")
    if not data.get("encontrado"):
        raise HTTPException(
            status_code=404,
            detail=data.get("mensaje") or f"Cédula {cedula} no encontrada en el TSE",
        )
    return data


@app.get("/nombres", summary="Busca personas por nombre y apellidos")
async def consulta_nombres(
    nombre: str = Query(..., description="Nombre (obligatorio para el TSE)"),
    apellido1: Optional[str] = Query("", description="Primer apellido"),
    apellido2: Optional[str] = Query("", description="Segundo apellido"),
    limite: int = Query(50, ge=1, le=100, description="Cantidad de resultados a mostrar"),
):
    """Equivale a consulta_nombres.aspx → muestra_nombres.aspx.

    El TSE exige al menos el campo `nombre`. Devuelve una lista de coincidencias
    con cédula, nombre completo y estado de fallecimiento. Para los datos
    completos de cada persona, consulte luego /cedula/{cedula}.
    """
    try:
        return await scraper.consulta_nombres(nombre, apellido1 or "", apellido2 or "", limite)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error consultando TSE: {e}")
