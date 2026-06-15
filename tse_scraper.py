"""
Scraper del Registro Civil / Padrón del TSE de Costa Rica vía navegador real.

Los sitios https://servicioselectorales.tse.go.cr/chc/consulta_cedula.aspx y
consulta_nombres.aspx están protegidos por un bot manager (Radware) que exige
ejecución de JavaScript (challenge stormcaster.js / perfdrive). Un cliente HTTP
plano recibe 302 al challenge; por eso se conduce Chromium headless (Playwright),
que resuelve el challenge de forma transparente.

Flujo real descubierto:
  consulta_cedula.aspx   --submit cédula-->  resultado_persona.aspx  (datos ricos)
  consulta_nombres.aspx  --submit nombre-->  muestra_nombres.aspx    (lista cédulas)
"""
import asyncio
import re
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright

BASE = "https://servicioselectorales.tse.go.cr/chc"
URL_CEDULA = f"{BASE}/consulta_cedula.aspx"
URL_NOMBRES = f"{BASE}/consulta_nombres.aspx"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
NAV_TIMEOUT = 60000


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _spans(html: str) -> dict:
    out = {}
    for m in re.finditer(r'<span[^>]*id="([^"]+)"[^>]*>(.*?)</span>', html, re.S | re.I):
        out[m.group(1)] = _clean(re.sub(r"<[^>]+>", " ", m.group(2)))
    return out


class TSEScraper:
    """Mantiene un Chromium + contexto vivo y reutiliza la cookie del bot manager."""

    def __init__(self, headless: bool = True, max_concurrency: int = 2):
        self._pw = None
        self._browser = None
        self._ctx = None
        self._headless = headless
        self._sem = asyncio.Semaphore(max_concurrency)
        self._lock = asyncio.Lock()

    async def start(self):
        self._pw = await async_playwright().start()
        await self._ensure_browser()

    async def _ensure_browser(self):
        if self._browser and self._browser.is_connected():
            return
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        self._browser = await self._pw.chromium.launch(
            headless=self._headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        self._ctx = await self._browser.new_context(user_agent=UA, locale="es-CR")
        self._ctx.set_default_navigation_timeout(NAV_TIMEOUT)
        self._ctx.set_default_timeout(NAV_TIMEOUT)

    async def stop(self):
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    @asynccontextmanager
    async def _page(self):
        async with self._sem:
            async with self._lock:
                await self._ensure_browser()
            page = await self._ctx.new_page()
            try:
                yield page
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    async def health(self) -> dict:
        async with self._page() as page:
            await page.goto(URL_CEDULA, wait_until="domcontentloaded")
            ok = await page.query_selector("#txtcedula") is not None
        return {"status": "ok" if ok else "degradado", "bot_wall": "superado" if ok else "?"}

    # ---------------- consulta por cédula ----------------
    async def consulta_cedula(self, cedula: str) -> dict:
        ced = re.sub(r"\D", "", cedula)
        if not ced:
            raise ValueError("Cédula inválida")
        async with self._page() as page:
            await page.goto(URL_CEDULA, wait_until="networkidle")
            await page.fill("#txtcedula", ced)
            try:
                async with page.expect_navigation(
                    url="**/resultado_persona.aspx", timeout=30000
                ):
                    await page.click("#btnConsultaCedula")
            except Exception:
                # quedó en la misma página: leer mensaje de error/validación
                msg = ""
                el = await page.query_selector("#lblmensajes")
                if el:
                    msg = _clean(await el.inner_text())
                body = await page.inner_text("body")
                if "no" in body.lower() and "exist" in body.lower():
                    msg = msg or "Cédula no encontrada"
                return {"encontrado": False, "cedula": ced, "mensaje": msg or "Sin resultados"}

            await page.wait_for_load_state("networkidle")
            html = await page.content()
            return _parse_persona(html, ced)

    # ---------------- consulta por nombre ----------------
    async def consulta_nombres(
        self, nombre: str, apellido1: str = "", apellido2: str = "", limite: int = 50
    ) -> dict:
        if not nombre or not nombre.strip():
            raise ValueError("El campo 'nombre' es obligatorio para el TSE")
        async with self._page() as page:
            await page.goto(URL_NOMBRES, wait_until="networkidle")
            await page.fill("#txtnombre", nombre)
            await page.fill("#txtapellido1", apellido1 or "")
            await page.fill("#txtapellido2", apellido2 or "")
            # selector de cantidad de resultados, si existe
            try:
                await page.select_option("select[name='ddlcantidad'], select", str(_clamp(limite)))
            except Exception:
                pass
            try:
                async with page.expect_navigation(
                    url="**/muestra_nombres.aspx", timeout=30000
                ):
                    await page.click("#btnConsultarNombre")
            except Exception:
                body = await page.inner_text("body")
                m = re.search(r"Debe digitar[^\n]*", body)
                return {
                    "total": 0,
                    "resultados": [],
                    "mensaje": _clean(m.group(0)) if m else "Sin resultados",
                }
            await page.wait_for_load_state("networkidle")
            body = await page.inner_text("body")
            page_html = await page.content()
            return _parse_lista_nombres(body, page_html)


def _clamp(n: int) -> int:
    for opt in (10, 25, 50, 100):
        if n <= opt:
            return opt
    return 100


_FECHA = re.compile(r"^\d{2}/\d{2}/\d{4}$")


def _parse_persona(html: str, ced: str) -> dict:
    s = _spans(html)

    def g(k):
        return s.get(k, "").strip()

    defuncion = " ".join(
        x for x in [g("lbldefuncion2"), g("lbldefunciontemporal")] if x
    ).strip()
    fallecido = bool(defuncion)  # el TSE muestra la fecha de defunción si falleció

    return {
        "encontrado": bool(g("lblcedula")),
        "cedula": g("lblcedula") or ced,
        "nombre_completo": g("lblnombrecompleto"),
        "conocido_como": g("lblconocidocomo"),
        "fecha_nacimiento": g("lblfechaNacimiento"),
        "edad": g("lbledad"),
        "nacionalidad": g("lblnacionalidad"),
        "marginal": g("lblLeyendaMarginal"),
        "padre": {"nombre": g("lblnombrepadre"), "identificacion": g("lblid_padre")},
        "madre": {"nombre": g("lblnombremadre"), "identificacion": g("lblid_madre")},
        "fallecido": fallecido,
        "defuncion": defuncion or None,
        "fuente": "resultado_persona.aspx (Registro Civil TSE)",
    }


_ROW = re.compile(
    r"^\s*\d+\s*-\s*(\d{9})\s+(.+?)\s*(\*\*\*\s*Fallecido\s*\*\*\*)?\s*$",
    re.I,
)


def _parse_lista_nombres(body: str, html: str) -> dict:
    resultados = []
    for line in body.splitlines():
        m = _ROW.match(line)
        if m:
            resultados.append(
                {
                    "cedula": m.group(1),
                    "nombre_completo": _clean(m.group(2)),
                    "fallecido": bool(m.group(3)),
                }
            )
    pag = re.search(r"P[áa]gina\s*#?\s*(\d+)\s*de un total de\s*(\d+)", body, re.I)
    return {
        "total": len(resultados),
        "pagina": int(pag.group(1)) if pag else 1,
        "paginas_totales": int(pag.group(2)) if pag else 1,
        "resultados": resultados,
        "fuente": "muestra_nombres.aspx (Registro Civil TSE)",
    }
