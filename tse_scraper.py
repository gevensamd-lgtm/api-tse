"""
TSE Registro Civil scraper — v4 FAST

Estrategia:
  1. Warmup (una vez): Playwright abre el formulario y resuelve el desafío
     Radware Bot Manager. El contexto del browser queda con las cookies.
  2. Consultas rápidas: usa `browser_context.request` de Playwright — el mismo
     contexto que el browser, con el mismo fingerprint TLS y las mismas cookies.
     Sin rendering, sin DOM, solo HTTP. Latencia ~150-400 ms por consulta.

Por qué no httpx: Radware valida el fingerprint TLS (JA3/JA4). httpx tiene
fingerprint diferente al Chromium, así que aunque enviemos las cookies, Radware
las rechaza. `browser_context.request` comparte contexto con el browser real.

Caché LRU en memoria: resultados 24h, máx 1 000 cédulas.
"""
import asyncio
import re
import time
from collections import OrderedDict
from typing import Optional

from playwright.async_api import async_playwright

BASE = "https://servicioselectorales.tse.go.cr/chc"
URL_CEDULA = f"{BASE}/consulta_cedula.aspx"
URL_NOMBRES = f"{BASE}/consulta_nombres.aspx"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
COOKIE_TTL   = 3600    # 1 hora — renovar warmup Radware
CACHE_TTL    = 86400   # 24 horas — cachear resultados
MAX_CACHE    = 1_000

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

# ─── helpers ────────────────────────────────────────────────────────────────

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _spans(html: str) -> dict:
    out = {}
    for m in re.finditer(r'<span[^>]*id="([^"]+)"[^>]*>(.*?)</span>', html, re.S | re.I):
        out[m.group(1)] = _clean(re.sub(r"<[^>]+>", " ", m.group(2)))
    return out


def _extract_viewstate(html: str) -> dict:
    def _val(name: str) -> str:
        m = re.search(rf'id="{name}"\s+value="([^"]*)"', html, re.I)
        return m.group(1) if m else ""
    return {
        "vs":  _val("__VIEWSTATE"),
        "vsg": _val("__VIEWSTATEGENERATOR"),
        "ev":  _val("__EVENTVALIDATION"),
    }


def _is_blocked(html: str, status: int) -> bool:
    if status == 403:
        return True
    return "rbzid" in html[:2000] or "Bot Manager" in html[:2000]

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
    def __init__(self, headless: bool = True, max_concurrency: int = 8):
        self._headless = headless
        self._pw = None
        self._browser = None
        self._ctx = None                  # BrowserContext con cookies Radware
        self._cookies_at: float = 0.0
        self._warm_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_concurrency)
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
        # Warm en background — no bloquea el arranque del servidor
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
        """Abre la página con Playwright para resolver el desafío Radware.
        Guarda el BrowserContext con las cookies de bypass."""
        async with self._warm_lock:
            if time.time() - self._cookies_at < COOKIE_TTL:
                return   # ya frescos

            # Crear nuevo contexto
            ctx = await self._browser.new_context(
                user_agent=UA,
                locale="es-CR",
                timezone_id="America/Costa_Rica",
                viewport={"width": 1280, "height": 800},
                color_scheme="light",
                java_script_enabled=True,
            )
            ctx.set_default_navigation_timeout(90_000)
            await ctx.add_init_script(_STEALTH_JS)

            page = await ctx.new_page()
            try:
                await page.goto(URL_CEDULA, wait_until="domcontentloaded")
                await page.wait_for_selector("#txtcedula", timeout=60_000)
                # Contexto con cookies válidas, guardarlo para requests rápidos
                if self._ctx and self._ctx != ctx:
                    try:
                        await self._ctx.close()
                    except Exception:
                        pass
                self._ctx = ctx
                self._cookies_at = time.time()
            except Exception:
                try:
                    await ctx.close()
                except Exception:
                    pass
                raise
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    async def _ensure_warm(self):
        if time.time() - self._cookies_at >= COOKIE_TTL or self._ctx is None:
            await self._warm()

    # ── consultas rápidas vía browser_context.request ────────────────────────
    # Usar ctx.request en vez de httpx porque Radware valida fingerprint TLS.
    # browser_context.request comparte el mismo contexto + cookies + TLS.

    async def _ctx_post_cedula(self, ced: str) -> str:
        """GET formulario → extraer ViewState → POST cédula.
        Retorna el HTML de resultado_persona.aspx."""
        req = self._ctx.request   # APIRequestContext compartido con el browser

        # 1. GET formulario
        r_get = await req.get(URL_CEDULA)
        html_form = await r_get.text()
        if _is_blocked(html_form, r_get.status):
            raise RuntimeError("Radware bloqueó GET formulario")

        vs = _extract_viewstate(html_form)

        # 2. POST cédula (sigue redirects automáticamente)
        r_post = await req.post(URL_CEDULA, form={
            "__VIEWSTATE":          vs["vs"],
            "__VIEWSTATEGENERATOR": vs["vsg"],
            "__EVENTVALIDATION":    vs["ev"],
            "txtcedula":            ced,
            "btnConsultaCedula":    "Consultar",
        })
        html = await r_post.text()
        if _is_blocked(html, r_post.status):
            raise RuntimeError("Radware bloqueó POST cédula")
        return html

    async def _ctx_post_nombres(self, nombre: str, ap1: str, ap2: str) -> str:
        """GET formulario nombres → extraer ViewState → POST."""
        req = self._ctx.request

        r_get = await req.get(URL_NOMBRES)
        html_form = await r_get.text()
        if _is_blocked(html_form, r_get.status):
            raise RuntimeError("Radware bloqueó GET nombres")

        vs = _extract_viewstate(html_form)

        r_post = await req.post(URL_NOMBRES, form={
            "__VIEWSTATE":          vs["vs"],
            "__VIEWSTATEGENERATOR": vs["vsg"],
            "__EVENTVALIDATION":    vs["ev"],
            "txtnombre":            nombre,
            "txtapellido1":         ap1 or "",
            "txtapellido2":         ap2 or "",
            "btnConsultarNombre":   "Consultar",
        })
        html = await r_post.text()
        if _is_blocked(html, r_post.status):
            raise RuntimeError("Radware bloqueó POST nombres")
        return html

    # ── API pública ──────────────────────────────────────────────────────────

    async def consulta_cedula(self, cedula: str) -> dict:
        ced = re.sub(r"\D", "", cedula)
        if not ced:
            raise ValueError("Cédula inválida")

        # Cache hit → respuesta instantánea
        cached = self._cache.get(ced)
        if cached is not None:
            return cached

        async with self._sem:
            return await self._fetch_cedula(ced)

    async def _fetch_cedula(self, ced: str, retry: bool = True) -> dict:
        await self._ensure_warm()
        try:
            html = await self._ctx_post_cedula(ced)
        except RuntimeError:
            if retry:
                self._cookies_at = 0
                await self._warm()
                return await self._fetch_cedula(ced, retry=False)
            raise

        result = _parse_persona(html, ced)
        if result.get("encontrado"):
            self._cache.set(ced, result)
        return result

    async def consulta_nombres(
        self, nombre: str, apellido1: str = "", apellido2: str = "", limite: int = 50
    ) -> dict:
        if not nombre or not nombre.strip():
            raise ValueError("El campo 'nombre' es obligatorio para el TSE")

        async with self._sem:
            return await self._fetch_nombres(nombre, apellido1, apellido2)

    async def _fetch_nombres(
        self, nombre: str, ap1: str, ap2: str, retry: bool = True
    ) -> dict:
        await self._ensure_warm()
        try:
            html = await self._ctx_post_nombres(nombre, ap1, ap2)
        except RuntimeError:
            if retry:
                self._cookies_at = 0
                await self._warm()
                return await self._fetch_nombres(nombre, ap1, ap2, retry=False)
            raise

        return _parse_lista_nombres(html)

    async def health(self) -> dict:
        warm = time.time() - self._cookies_at < COOKIE_TTL and self._ctx is not None
        return {
            "status":        "ok" if warm else "warming",
            "bot_wall":      "superado" if warm else "pendiente",
            "cookies_age_s": int(time.time() - self._cookies_at) if self._cookies_at else None,
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
