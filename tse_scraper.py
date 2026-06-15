"""
Scraper del Registro Civil / Padrón del TSE de Costa Rica vía navegador real.

Usa Playwright Chromium con stealth patches para superar el bot manager Radware
que protege servicioselectorales.tse.go.cr.

Estrategia:
- Chromium headless con anti-detección: navigator.webdriver=false, plugins, idioma, etc.
- Página reutilizable por tipo de consulta (cédula / nombres), warm lazy.
- Semáforo y lock por página para serializar acceso.
"""
import asyncio
import re

from playwright.async_api import Page, async_playwright

BASE = "https://servicioselectorales.tse.go.cr/chc"
URL_CEDULA = f"{BASE}/consulta_cedula.aspx"
URL_NOMBRES = f"{BASE}/consulta_nombres.aspx"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
NAV_TIMEOUT = 90_000
SELECTOR_TIMEOUT = 60_000

# Script que inyecta en cada página para ocultar marcadores de automatización
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['es-CR','es','en-US','en']});
Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
window.chrome = {runtime: {}};
const orig = navigator.permissions.query.bind(navigator.permissions);
navigator.permissions.query = (p) =>
    p.name === 'notifications' ? Promise.resolve({state: 'denied'}) : orig(p);
"""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _spans(html: str) -> dict:
    out = {}
    for m in re.finditer(r'<span[^>]*id="([^"]+)"[^>]*>(.*?)</span>', html, re.S | re.I):
        out[m.group(1)] = _clean(re.sub(r"<[^>]+>", " ", m.group(2)))
    return out


class TSEScraper:
    def __init__(self, headless: bool = True, max_concurrency: int = 2):
        self._pw = None
        self._browser = None
        self._ctx = None
        self._headless = headless
        self._lock_cedula = asyncio.Lock()
        self._lock_nombres = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_concurrency)
        self._cedula_page: Page | None = None
        self._nombres_page: Page | None = None

    async def start(self):
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self._headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--window-size=1280,800",
                "--start-maximized",
                "--lang=es-CR",
            ],
        )
        self._ctx = await self._browser.new_context(
            user_agent=UA,
            locale="es-CR",
            timezone_id="America/Costa_Rica",
            viewport={"width": 1280, "height": 800},
            color_scheme="light",
            java_script_enabled=True,
        )
        self._ctx.set_default_navigation_timeout(NAV_TIMEOUT)
        self._ctx.set_default_timeout(NAV_TIMEOUT)
        await self._ctx.add_init_script(_STEALTH_JS)

    async def stop(self):
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def _open_page(self) -> Page:
        page = await self._ctx.new_page()
        return page

    async def _get_cedula_page(self) -> Page:
        """Reutiliza o crea página warm para cédula (con retry ante errores de red)."""
        if self._cedula_page and not self._cedula_page.is_closed():
            return self._cedula_page
        last_err: Exception | None = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(2 ** attempt)
            page = await self._open_page()
            try:
                await page.goto(URL_CEDULA, wait_until="domcontentloaded")
                await page.wait_for_selector("#txtcedula", timeout=SELECTOR_TIMEOUT)
                self._cedula_page = page
                return page
            except Exception as e:
                last_err = e
                try:
                    await page.close()
                except Exception:
                    pass
        raise RuntimeError(f"No se pudo abrir formulario cédula: {last_err}")

    async def _get_nombres_page(self) -> Page:
        """Reutiliza o crea página warm para nombres (con retry ante errores de red)."""
        if self._nombres_page and not self._nombres_page.is_closed():
            return self._nombres_page
        last_err: Exception | None = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(2 ** attempt)
            page = await self._open_page()
            try:
                await page.goto(URL_NOMBRES, wait_until="domcontentloaded")
                await page.wait_for_selector("#txtnombre", timeout=SELECTOR_TIMEOUT)
                self._nombres_page = page
                return page
            except Exception as e:
                last_err = e
                try:
                    await page.close()
                except Exception:
                    pass
        raise RuntimeError(f"No se pudo abrir formulario nombres: {last_err}")

    async def _reset_cedula(self):
        if self._cedula_page and not self._cedula_page.is_closed():
            try:
                await self._cedula_page.close()
            except Exception:
                pass
        self._cedula_page = None

    async def _reset_nombres(self):
        if self._nombres_page and not self._nombres_page.is_closed():
            try:
                await self._nombres_page.close()
            except Exception:
                pass
        self._nombres_page = None

    async def health(self) -> dict:
        async with self._lock_cedula:
            try:
                page = await self._get_cedula_page()
                ok = not page.is_closed()
            except Exception:
                ok = False
        return {
            "status": "ok" if ok else "degradado",
            "bot_wall": "superado" if ok else "bloqueado",
        }

    # ---------------- consulta por cédula ----------------
    async def consulta_cedula(self, cedula: str) -> dict:
        ced = re.sub(r"\D", "", cedula)
        if not ced:
            raise ValueError("Cédula inválida")

        async with self._sem:
            async with self._lock_cedula:
                page = await self._get_cedula_page()

                # Navegar al formulario (si ya estamos ahí, sólo recarga el form)
                if not page.url.startswith(URL_CEDULA.split("?")[0]):
                    await page.goto(URL_CEDULA, wait_until="domcontentloaded")
                    await page.wait_for_selector("#txtcedula", timeout=SELECTOR_TIMEOUT)

                await page.fill("#txtcedula", ced)
                try:
                    async with page.expect_navigation(
                        url="**/resultado_persona.aspx", timeout=30_000
                    ):
                        await page.click("#btnConsultaCedula")
                except Exception:
                    msg = ""
                    el = await page.query_selector("#lblmensajes")
                    if el:
                        msg = _clean(await el.inner_text())
                    body = await page.inner_text("body")
                    if "no" in body.lower() and "exist" in body.lower():
                        msg = msg or "Cédula no encontrada"
                    await self._reset_cedula()
                    return {"encontrado": False, "cedula": ced, "mensaje": msg or "Sin resultados"}

                await page.wait_for_load_state("domcontentloaded")
                html = await page.content()
                # Resetear para que próxima llamada vuelva al formulario limpio
                await self._reset_cedula()
                return _parse_persona(html, ced)

    # ---------------- consulta por nombre ----------------
    async def consulta_nombres(
        self, nombre: str, apellido1: str = "", apellido2: str = "", limite: int = 50
    ) -> dict:
        if not nombre or not nombre.strip():
            raise ValueError("El campo 'nombre' es obligatorio para el TSE")

        async with self._sem:
            async with self._lock_nombres:
                page = await self._get_nombres_page()

                if not page.url.startswith(URL_NOMBRES.split("?")[0]):
                    await page.goto(URL_NOMBRES, wait_until="domcontentloaded")
                    await page.wait_for_selector("#txtnombre", timeout=SELECTOR_TIMEOUT)

                await page.evaluate(
                    "([n, a1, a2]) => { "
                    "document.getElementById('txtnombre').value = n; "
                    "document.getElementById('txtapellido1').value = a1; "
                    "document.getElementById('txtapellido2').value = a2; "
                    "}",
                    [nombre, apellido1 or "", apellido2 or ""],
                )
                try:
                    async with page.expect_navigation(
                        url="**/muestra_nombres.aspx", timeout=30_000
                    ):
                        await page.click("#btnConsultarNombre")
                except Exception:
                    body = await page.inner_text("body")
                    m = re.search(r"Debe digitar[^\n]*", body)
                    await self._reset_nombres()
                    return {
                        "total": 0,
                        "resultados": [],
                        "mensaje": _clean(m.group(0)) if m else "Sin resultados",
                    }

                await page.wait_for_load_state("domcontentloaded")
                body = await page.inner_text("body")
                page_html = await page.content()
                await self._reset_nombres()
                return _parse_lista_nombres(body, page_html)


def _clamp(n: int) -> int:
    for opt in (10, 25, 50, 100):
        if n <= opt:
            return opt
    return 100


def _parse_persona(html: str, ced: str) -> dict:
    s = _spans(html)

    def g(k):
        return s.get(k, "").strip()

    defuncion = " ".join(
        x for x in [g("lbldefuncion2"), g("lbldefunciontemporal")] if x
    ).strip()
    fallecido = bool(defuncion)

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
