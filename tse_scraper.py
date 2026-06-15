"""
TSE Registro Civil scraper — v6

Estrategia:
  1. Warmup: Playwright abre el formulario del TSE en Chromium headless → resuelve
     Radware Bot Manager automáticamente. La página se mantiene abierta y lista.
  2. Consultas rápidas: rellena el formulario en la página ya caliente y hace click
     en el botón. Playwright navega como un usuario real → ~700ms por consulta.
  3. Después de cada consulta, navega de vuelta al formulario (en background) para
     la siguiente consulta.
  4. Caché LRU 24h, 1 000 cédulas máximo.

Latencia esperada post-warmup: 700–950ms por consulta (round-trip de red a TSE CR).
"""
import asyncio
import re
import time
from collections import OrderedDict
from typing import Optional

from playwright.async_api import Page, async_playwright

BASE = "https://servicioselectorales.tse.go.cr/chc"
URL_CEDULA  = f"{BASE}/consulta_cedula.aspx"
URL_NOMBRES = f"{BASE}/consulta_nombres.aspx"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
NAV_TIMEOUT      = 90_000
SELECTOR_TIMEOUT = 12_000   # tiempo máximo para detectar selector del form (fallback si Radware)
QUERY_TIMEOUT    = 20_000   # timeout para cada consulta
COOKIE_TTL       = 3_600    # 1 hora — renovar warmup Radware
CACHE_TTL        = 86_400   # 24 horas
MAX_CACHE        = 1_000

# Stealth JS: ocultar indicadores de Playwright/headless
_STEALTH_JS = """
(function() {
  // Ocultar webdriver
  Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
  // Simular plugins reales
  Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
  // Idiomas reales
  Object.defineProperty(navigator, 'languages', {get: () => ['es-CR','es','en-US','en']});
  // Plataforma
  Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
  // Chrome runtime
  window.chrome = {runtime: {}};
  // Permissions
  const orig = navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query = (p) =>
    p.name === 'notifications' ? Promise.resolve({state:'denied'}) : orig(p);
  // Ocultar HeadlessChrome en userAgentData (detectado por Radware)
  try {
    Object.defineProperty(navigator, 'userAgentData', {
      get: () => ({
        brands: [
          {brand: 'Chromium', version: '124'},
          {brand: 'Google Chrome', version: '124'},
          {brand: 'Not/A)Brand', version: '99'}
        ],
        mobile: false,
        platform: 'macOS',
        getHighEntropyValues: async function(hints) {
          const vals = {
            platform: 'macOS', platformVersion: '13.5.2',
            architecture: 'x86', bitness: '64', model: '',
            uaFullVersion: '124.0.6367.243',
            fullVersionList: [
              {brand: 'Chromium', version: '124.0.6367.243'},
              {brand: 'Google Chrome', version: '124.0.6367.243'},
              {brand: 'Not/A)Brand', version: '99.0.0.0'}
            ]
          };
          return Object.fromEntries((hints||[]).map(h => [h, vals[h]]));
        },
        toJSON: function() {
          return {brands: this.brands, mobile: this.mobile, platform: this.platform};
        }
      }),
      configurable: true
    });
  } catch(e) {}
  // Dimensiones de ventana reales
  try {
    Object.defineProperty(window, 'outerWidth',  {get: () => 1280});
    Object.defineProperty(window, 'outerHeight', {get: () => 800});
    Object.defineProperty(window, 'screenX',     {get: () => 0});
    Object.defineProperty(window, 'screenY',     {get: () => 0});
  } catch(e) {}
  // deviceMemory y concurrencia
  try {
    Object.defineProperty(navigator, 'deviceMemory',      {get: () => 8});
    Object.defineProperty(navigator, 'hardwareConcurrency',{get: () => 8});
  } catch(e) {}
})();
"""

# ─── helpers ────────────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _spans(html: str) -> dict:
    out = {}
    for m in re.finditer(r'<span[^>]*id="([^"]+)"[^>]*>(.*?)</span>', html, re.S | re.I):
        out[m.group(1)] = _clean(re.sub(r"<[^>]+>", " ", m.group(2)))
    return out


def _is_blocked(html: str) -> bool:
    t = html[:3000]
    return ("rbzid" in t and "Bot Manager" in t) or "challenge" in t.lower()[:500]

# ─── caché LRU ──────────────────────────────────────────────────────────────

class _LRUCache:
    def __init__(self, maxsize: int, ttl: int):
        self._d: OrderedDict[str, tuple] = OrderedDict()
        self._maxsize = maxsize
        self._ttl = ttl

    def get(self, key: str) -> Optional[dict]:
        if key not in self._d:
            return None
        val, ts = self._d[key]
        if time.time() - ts > self._ttl:
            del self._d[key]
            return None
        self._d.move_to_end(key)
        return val

    def set(self, key: str, val: dict):
        if key in self._d:
            self._d.move_to_end(key)
        self._d[key] = (val, time.time())
        if len(self._d) > self._maxsize:
            self._d.popitem(last=False)

# ─── scraper ─────────────────────────────────────────────────────────────────

class TSEScraper:
    def __init__(self, headless: bool = True, max_concurrency: int = 4):
        self._headless = headless
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page_cedula: Optional[Page] = None
        self._page_nombres: Optional[Page] = None
        self._warmed_at: float = 0.0
        self._warm_lock = asyncio.Lock()
        self._lock_cedula  = asyncio.Lock()
        self._lock_nombres = asyncio.Lock()
        self._cache = _LRUCache(MAX_CACHE, CACHE_TTL)

    # ── ciclo de vida ────────────────────────────────────────────────────────

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
                "--lang=es-CR",
            ],
        )
        asyncio.create_task(self._warm())

    async def stop(self):
        try:
            if self._ctx:
                await self._ctx.close()
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    # ── warmup Radware ───────────────────────────────────────────────────────

    async def _warm(self):
        async with self._warm_lock:
            if time.time() - self._warmed_at < COOKIE_TTL:
                return

            ctx = await self._browser.new_context(
                user_agent=UA,
                locale="es-CR",
                timezone_id="America/Costa_Rica",
                viewport={"width": 1280, "height": 800},
                color_scheme="light",
                java_script_enabled=True,
            )
            ctx.set_default_navigation_timeout(NAV_TIMEOUT)
            ctx.set_default_timeout(NAV_TIMEOUT)
            await ctx.add_init_script(_STEALTH_JS)

            p_ced = await self._open_warm(ctx, URL_CEDULA,  "#txtcedula")
            p_nom = await self._open_warm(ctx, URL_NOMBRES, "#txtnombre")

            if self._ctx and self._ctx is not ctx:
                try:
                    await self._ctx.close()
                except Exception:
                    pass

            self._ctx         = ctx
            self._page_cedula = p_ced
            self._page_nombres = p_nom
            self._warmed_at   = time.time()

    async def _open_warm(self, ctx, url: str, selector: str) -> Page:
        """Abre la página del formulario. Reintenta si Radware desafía."""
        for attempt in range(6):
            if attempt:
                await asyncio.sleep(min(2 ** attempt, 10))
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded")
                # Si Radware desafió, la URL cambia a validate.perfdrive.com
                if "validate.perfdrive.com" in page.url or "tse.go.cr" not in page.url:
                    # Las cookies del desafío se acumulan — cerrar y reintentar
                    await page.close()
                    continue
                # Esperar el formulario
                await page.wait_for_selector(selector, timeout=SELECTOR_TIMEOUT)
                # Verificar que la página no se redirigió mientras esperábamos
                if "tse.go.cr" in page.url:
                    return page
                await page.close()
            except Exception as e:
                try:
                    await page.close()
                except Exception:
                    pass
                if attempt == 5:
                    raise RuntimeError(f"Warm falló para {url}: {e}")
        raise RuntimeError(f"Warm falló para {url}: demasiados reintentos")

    async def _ensure_warm(self):
        page_ok = (
            self._page_cedula is not None
            and not self._page_cedula.is_closed()
            and "tse.go.cr" in self._page_cedula.url
        )
        if time.time() - self._warmed_at >= COOKIE_TTL or not page_ok:
            await self._warm()

    # ── navegación directa en el browser ────────────────────────────────────

    async def _browser_fetch_cedula(self, ced: str) -> tuple[str, str | None]:
        """Rellena y envía el form. Devuelve (html_persona, html_detalle_nacimiento|None)."""
        page = self._page_cedula
        await page.fill("#txtcedula", ced)
        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=QUERY_TIMEOUT):
                await page.click("#btnConsultaCedula")
        except Exception as e:
            raise RuntimeError(f"Timeout navegando TSE para cédula: {e}")
        html_persona = await page.content()

        # Seguir "Ver Más Detalles" → detalle_nacimiento.aspx (lugar de nacimiento)
        # El botón es un ASP.NET LinkButton con __doPostBack — ID=#LinkButton11
        html_detalle: str | None = None
        try:
            link = page.locator("#LinkButton11, a[title*='detalles del nacimiento']")
            if await link.count() > 0:
                async with page.expect_navigation(wait_until="domcontentloaded", timeout=QUERY_TIMEOUT):
                    await link.first.click()
                html_detalle = await page.content()
        except Exception:
            pass  # fail-soft

        return html_persona, html_detalle

    async def _browser_fetch_nombres(self, nombre: str, ap1: str, ap2: str) -> str:
        """Rellena y envía el form de nombres en el browser real."""
        page = self._page_nombres
        await page.fill("#txtnombre",    nombre)
        await page.fill("#txtapellido1", ap1 or "")
        await page.fill("#txtapellido2", ap2 or "")
        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=QUERY_TIMEOUT):
                await page.click("#btnConsultarNombre")
        except Exception as e:
            raise RuntimeError(f"Timeout navegando TSE para nombres: {e}")
        return await page.content()

    async def _reset_to_form(self, page: Page, url: str, selector: str):
        """Vuelve al formulario después de una consulta (para la siguiente)."""
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_selector(selector, timeout=SELECTOR_TIMEOUT)
        except Exception:
            # Si falla la vuelta al form, el siguiente ciclo hará re-warm
            self._warmed_at = 0

    # ── API pública ──────────────────────────────────────────────────────────

    async def consulta_cedula(self, cedula: str) -> dict:
        ced = re.sub(r"\D", "", cedula)
        if not ced:
            raise ValueError("Cédula inválida")

        cached = self._cache.get(ced)
        if cached is not None:
            return cached

        async with self._lock_cedula:
            cached = self._cache.get(ced)
            if cached is not None:
                return cached
            return await self._fetch_cedula(ced)

    async def _fetch_cedula(self, ced: str, retry: bool = True) -> dict:
        await self._ensure_warm()
        try:
            html, html_detalle = await self._browser_fetch_cedula(ced)
        except RuntimeError:
            if retry:
                self._warmed_at = 0
                await self._warm()
                return await self._fetch_cedula(ced, retry=False)
            raise

        # Cédula no encontrada → error_trans.aspx
        if "error_trans.aspx" in self._page_cedula.url:
            asyncio.create_task(
                self._reset_to_form(self._page_cedula, URL_CEDULA, "#txtcedula")
            )
            return {"encontrado": False, "cedula": ced, "mensaje": "Cédula no encontrada en el Registro Civil"}

        if _is_blocked(html):
            if retry:
                self._warmed_at = 0
                await self._warm()
                return await self._fetch_cedula(ced, retry=False)
            raise RuntimeError("Radware bloqueó la consulta de cédula")

        result = _parse_persona(html, ced)
        if html_detalle:
            detalle = _parse_detalle_nacimiento(html_detalle)
            result.update({k: v for k, v in detalle.items() if v})

        if result.get("encontrado"):
            self._cache.set(ced, result)

        # Navegar de vuelta al formulario en background (para próxima consulta)
        asyncio.create_task(
            self._reset_to_form(self._page_cedula, URL_CEDULA, "#txtcedula")
        )
        return result

    async def consulta_nombres(
        self, nombre: str, apellido1: str = "", apellido2: str = "", limite: int = 50
    ) -> dict:
        if not nombre or not nombre.strip():
            raise ValueError("El campo 'nombre' es obligatorio para el TSE")

        async with self._lock_nombres:
            return await self._fetch_nombres(nombre, apellido1, apellido2)

    async def _fetch_nombres(
        self, nombre: str, ap1: str, ap2: str, retry: bool = True
    ) -> dict:
        await self._ensure_warm()
        try:
            html = await self._browser_fetch_nombres(nombre, ap1, ap2)
        except RuntimeError:
            if retry:
                self._warmed_at = 0
                await self._warm()
                return await self._fetch_nombres(nombre, ap1, ap2, retry=False)
            raise

        if "error_trans.aspx" in self._page_nombres.url:
            asyncio.create_task(
                self._reset_to_form(self._page_nombres, URL_NOMBRES, "#txtnombre")
            )
            return {"total": 0, "pagina": 1, "paginas_totales": 1, "resultados": [], "fuente": "TSE"}

        if _is_blocked(html):
            if retry:
                self._warmed_at = 0
                await self._warm()
                return await self._fetch_nombres(nombre, ap1, ap2, retry=False)
            raise RuntimeError("Radware bloqueó la consulta de nombres")

        result = _parse_lista_nombres(html)
        asyncio.create_task(
            self._reset_to_form(self._page_nombres, URL_NOMBRES, "#txtnombre")
        )
        return result

    async def health(self) -> dict:
        ced_ok = (
            self._page_cedula is not None
            and not self._page_cedula.is_closed()
            and "tse.go.cr" in (self._page_cedula.url if self._page_cedula else "")
        )
        warm = time.time() - self._warmed_at < COOKIE_TTL and ced_ok
        return {
            "status":        "ok" if warm else "warming",
            "bot_wall":      "superado" if warm else "pendiente",
            "cookies_age_s": int(time.time() - self._warmed_at) if self._warmed_at else None,
            "cache_entries": len(self._cache._d),
        }


# ─── parsers ─────────────────────────────────────────────────────────────────

def _parse_persona(html: str, ced: str) -> dict:
    s = _spans(html)

    def g(k: str) -> str:
        return s.get(k, "").strip()

    defuncion = " ".join(x for x in [g("lbldefuncion2"), g("lbldefunciontemporal")] if x).strip()

    return {
        "encontrado":       bool(g("lblcedula")),
        "cedula":           g("lblcedula") or ced,
        "nombre_completo":  g("lblnombrecompleto"),
        "conocido_como":    g("lblconocidocomo"),
        "fecha_nacimiento": g("lblfechaNacimiento"),
        "edad":             g("lbledad"),
        "nacionalidad":     g("lblnacionalidad"),
        "marginal":         g("lblLeyendaMarginal"),
        "padre": {
            "nombre":         g("lblnombrepadre"),
            "identificacion": g("lblid_padre"),
        },
        "madre": {
            "nombre":         g("lblnombremadre"),
            "identificacion": g("lblid_madre"),
        },
        "fallecido": bool(defuncion),
        "defuncion": defuncion or None,
        "fuente":    "resultado_persona.aspx (Registro Civil TSE)",
    }


def _parse_detalle_nacimiento(html: str) -> dict:
    """
    Extrae datos adicionales de detalle_nacimiento.aspx.
    IDs confirmados del TSE (2025):
      lbllugar_nacimiento  → "DISTRITO CANTON PROVINCIA" (todo en un string)
      lblfecha_nacimiento  → "DD/MM/YYYY"
      lblnombre_padre      → nombre completo del padre
      lblid_padre          → cédula del padre
      lblnombre_madre      → nombre completo de la madre
      lblid_madre          → cédula de la madre
      lblempadronado       → "SI" / "NO"
      lblfallecido         → "SI" / "NO"
    """
    s = _spans(html)

    def g(k: str) -> str:
        return s.get(k, "").strip()

    result: dict = {}

    lugar = g("lbllugar_nacimiento")
    if lugar:
        result["lugar_nacimiento"] = lugar  # "CENTRO GOLFITO PUNTARENAS"

    empadronado = g("lblempadronado")
    if empadronado:
        result["empadronado"] = empadronado == "SI"

    # padre/madre del detalle (más completos para menores sin cédula propia)
    nombre_padre = g("lblnombre_padre")
    id_padre     = g("lblid_padre")
    nombre_madre = g("lblnombre_madre")
    id_madre     = g("lblid_madre")
    if nombre_padre or nombre_madre:
        result["padre"] = {"nombre": nombre_padre or None, "identificacion": id_padre or None}
        result["madre"] = {"nombre": nombre_madre or None, "identificacion": id_madre or None}

    return result


_ROW = re.compile(
    r"^\s*\d+\s*-\s*(\d{9})\s+(.+?)\s*(\*\*\*\s*Fallecido\s*\*\*\*)?\s*$",
    re.I,
)


def _parse_lista_nombres(html: str) -> dict:
    body = re.sub(r"<[^>]+>", " ", html)
    body = re.sub(r"\s+", " ", body)
    resultados = []
    for line in body.splitlines():
        m = _ROW.match(line)
        if m:
            resultados.append({
                "cedula":          m.group(1),
                "nombre_completo": _clean(m.group(2)),
                "fallecido":       bool(m.group(3)),
            })
    pag = re.search(r"P[áa]gina\s*#?\s*(\d+)\s*de un total de\s*(\d+)", body, re.I)
    return {
        "total":           len(resultados),
        "pagina":          int(pag.group(1)) if pag else 1,
        "paginas_totales": int(pag.group(2)) if pag else 1,
        "resultados":      resultados,
        "fuente":          "muestra_nombres.aspx (Registro Civil TSE)",
    }
