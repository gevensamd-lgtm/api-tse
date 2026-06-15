"""
TSE Registro Civil scraper — v5 FAST

Estrategia:
  1. Warmup: Playwright abre las páginas del formulario una vez → resuelve
     Radware Bot Manager (JS challenge). Los páginas se mantienen abiertas.
  2. Consultas rápidas: usa page.evaluate() con window.fetch() ejecutado DENTRO
     del contexto del browser. El fetch() corre con:
       - Fingerprint TLS de Chromium real (JA3/JA4 idéntico al browser)
       - Cookies de sesión Radware ya en el contexto
       - Mismo origen que el formulario TSE → sin CORS
     No hay rendering de DOM; solo recibimos el HTML de la respuesta HTTP.
  3. Caché LRU 24h, 1 000 cédulas máximo.

Latencia esperada: 200-600ms por consulta (solo round-trip de red a TSE).
"""
import asyncio
import re
import time
from collections import OrderedDict
from typing import Optional

from playwright.async_api import Page, async_playwright

BASE = "https://servicioselectorales.tse.go.cr/chc"
URL_CEDULA = f"{BASE}/consulta_cedula.aspx"
URL_NOMBRES = f"{BASE}/consulta_nombres.aspx"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
NAV_TIMEOUT     = 90_000
SELECTOR_TIMEOUT = 60_000
COOKIE_TTL      = 3_600    # 1 hora — renovar warmup Radware
CACHE_TTL       = 86_400   # 24 horas
MAX_CACHE       = 1_000

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

# JS ejecutado dentro del browser Chromium para hacer fetch sin rendering
_JS_FETCH_CEDULA = """
async ([urlForm, ced]) => {
    // GET formulario — obtener ViewState actualizado
    const fg = await fetch(urlForm, {credentials: 'include', redirect: 'follow'});
    const fh = await fg.text();
    const dp = new DOMParser();
    const fd = dp.parseFromString(fh, 'text/html');
    const vs  = fd.getElementById('__VIEWSTATE')?.value ?? '';
    const vsg = fd.getElementById('__VIEWSTATEGENERATOR')?.value ?? '';
    const ev  = fd.getElementById('__EVENTVALIDATION')?.value ?? '';

    // POST cédula
    const body = new URLSearchParams({
        '__VIEWSTATE': vs, '__VIEWSTATEGENERATOR': vsg, '__EVENTVALIDATION': ev,
        'txtcedula': ced, 'btnConsultaCedula': 'Consultar',
    });
    const rp = await fetch(urlForm, {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: body.toString(),
        credentials: 'include',
        redirect: 'follow',
    });
    return await rp.text();
}
"""

_JS_FETCH_NOMBRES = """
async ([urlForm, nombre, ap1, ap2]) => {
    const fg = await fetch(urlForm, {credentials: 'include', redirect: 'follow'});
    const fh = await fg.text();
    const dp = new DOMParser();
    const fd = dp.parseFromString(fh, 'text/html');
    const vs  = fd.getElementById('__VIEWSTATE')?.value ?? '';
    const vsg = fd.getElementById('__VIEWSTATEGENERATOR')?.value ?? '';
    const ev  = fd.getElementById('__EVENTVALIDATION')?.value ?? '';

    const body = new URLSearchParams({
        '__VIEWSTATE': vs, '__VIEWSTATEGENERATOR': vsg, '__EVENTVALIDATION': ev,
        'txtnombre': nombre, 'txtapellido1': ap1, 'txtapellido2': ap2,
        'btnConsultarNombre': 'Consultar',
    });
    const rp = await fetch(urlForm, {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: body.toString(),
        credentials: 'include',
        redirect: 'follow',
    });
    return await rp.text();
}
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
        self._lock_cedula = asyncio.Lock()   # serializa fetches sobre la misma página
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

            # Abrir ambas páginas con retry
            p_ced = await self._open_warm(ctx, URL_CEDULA, "#txtcedula")
            p_nom = await self._open_warm(ctx, URL_NOMBRES, "#txtnombre")

            # Cerrar contexto anterior si existe
            if self._ctx and self._ctx is not ctx:
                try:
                    await self._ctx.close()
                except Exception:
                    pass

            self._ctx = ctx
            self._page_cedula = p_ced
            self._page_nombres = p_nom
            self._warmed_at = time.time()

    async def _open_warm(self, ctx, url: str, selector: str) -> Page:
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(2 ** attempt)
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_selector(selector, timeout=SELECTOR_TIMEOUT)
                return page
            except Exception as e:
                try:
                    await page.close()
                except Exception:
                    pass
                if attempt == 2:
                    raise RuntimeError(f"Warm falló para {url}: {e}")

    async def _ensure_warm(self):
        if time.time() - self._warmed_at >= COOKIE_TTL or self._page_cedula is None:
            await self._warm()

    # ── fetches via page.evaluate() ──────────────────────────────────────────

    async def _js_fetch_cedula(self, ced: str) -> str:
        """Ejecuta fetch() dentro del browser Chromium — fingerprint TLS real."""
        try:
            html = await self._page_cedula.evaluate(_JS_FETCH_CEDULA, [URL_CEDULA, ced])
            return html or ""
        except Exception as e:
            raise RuntimeError(f"page.evaluate falló para cédula: {e}")

    async def _js_fetch_nombres(self, nombre: str, ap1: str, ap2: str) -> str:
        try:
            html = await self._page_nombres.evaluate(
                _JS_FETCH_NOMBRES, [URL_NOMBRES, nombre, ap1 or "", ap2 or ""]
            )
            return html or ""
        except Exception as e:
            raise RuntimeError(f"page.evaluate falló para nombres: {e}")

    # ── API pública ──────────────────────────────────────────────────────────

    async def consulta_cedula(self, cedula: str) -> dict:
        ced = re.sub(r"\D", "", cedula)
        if not ced:
            raise ValueError("Cédula inválida")

        cached = self._cache.get(ced)
        if cached is not None:
            return cached

        async with self._lock_cedula:
            # Double-check caché tras obtener lock
            cached = self._cache.get(ced)
            if cached is not None:
                return cached
            return await self._fetch_cedula(ced)

    async def _fetch_cedula(self, ced: str, retry: bool = True) -> dict:
        await self._ensure_warm()
        try:
            html = await self._js_fetch_cedula(ced)
        except RuntimeError:
            if retry:
                self._warmed_at = 0
                await self._warm()
                return await self._fetch_cedula(ced, retry=False)
            raise

        if _is_blocked(html):
            if retry:
                self._warmed_at = 0
                await self._warm()
                return await self._fetch_cedula(ced, retry=False)
            raise RuntimeError("Radware bloqueó la consulta de cédula")

        result = _parse_persona(html, ced)
        if result.get("encontrado"):
            self._cache.set(ced, result)
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
            html = await self._js_fetch_nombres(nombre, ap1, ap2)
        except RuntimeError:
            if retry:
                self._warmed_at = 0
                await self._warm()
                return await self._fetch_nombres(nombre, ap1, ap2, retry=False)
            raise

        if _is_blocked(html):
            if retry:
                self._warmed_at = 0
                await self._warm()
                return await self._fetch_nombres(nombre, ap1, ap2, retry=False)
            raise RuntimeError("Radware bloqueó la consulta de nombres")

        return _parse_lista_nombres(html)

    async def health(self) -> dict:
        warm = (
            time.time() - self._warmed_at < COOKIE_TTL
            and self._page_cedula is not None
            and not self._page_cedula.is_closed()
        )
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
        "encontrado":      bool(g("lblcedula")),
        "cedula":          g("lblcedula") or ced,
        "nombre_completo": g("lblnombrecompleto"),
        "conocido_como":   g("lblconocidocomo"),
        "fecha_nacimiento": g("lblfechaNacimiento"),
        "edad":            g("lbledad"),
        "nacionalidad":    g("lblnacionalidad"),
        "marginal":        g("lblLeyendaMarginal"),
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
