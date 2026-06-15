"""
TSE Registro Civil scraper — v3 FAST

Estrategia dual:
  1. Playwright (warmup único): abre la página una vez para resolver el desafío
     JavaScript de Radware Bot Manager y extraer las cookies de sesión.
     Se renueva automáticamente cada hora si las cookies expiran.
  2. httpx (todas las consultas): reutiliza las cookies Radware para hacer
     GET+POST directos al formulario ASP.NET. Latencia típica: 150-400ms.

Por qué funciona: Radware emite una cookie de bypass (rbzid + rbzsessionid)
que vale para toda la sesión HTTP, no solo para el primer request. Una vez
obtenida con Playwright, httpx puede usarla sin limitaciones de tiempo
mientras el TTL no expire (~1-2 horas en el bot manager del TSE).

Caché en memoria: resultados se cachean 24h (max 1 000 cédulas).
"""
import asyncio
import re
import time
from collections import OrderedDict
from typing import Optional

import httpx
from playwright.async_api import async_playwright

BASE = "https://servicioselectorales.tse.go.cr/chc"
URL_CEDULA = f"{BASE}/consulta_cedula.aspx"
URL_NOMBRES = f"{BASE}/consulta_nombres.aspx"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
COOKIE_TTL = 3600        # 1 hora — renovar cookies Radware
CACHE_TTL  = 86400       # 24 horas — cachear resultados de cédula
MAX_CACHE  = 1_000       # máx entradas en caché

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


def _is_blocked(resp: httpx.Response) -> bool:
    """Detecta si Radware nos bloqueó (pide JS challenge o devuelve 403)."""
    if resp.status_code == 403:
        return True
    text = resp.text[:2000]
    return "rbzid" in text or "Bot Manager" in text or "challenge" in text.lower()

# ─── caché LRU simple ────────────────────────────────────────────────────────

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
        self._cookies: dict[str, str] = {}
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
        # Warm inicial en background — no bloquea el arranque
        asyncio.create_task(self._warm_cookies())

    async def stop(self):
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    # ── warmup Radware con Playwright ────────────────────────────────────────

    async def _warm_cookies(self):
        """Abre el formulario con Playwright para resolver el desafío Radware
        y extraer las cookies de bypass. El lock garantiza un único warmup a la vez."""
        async with self._warm_lock:
            if time.time() - self._cookies_at < COOKIE_TTL:
                return   # ya están frescas

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
                raw = await ctx.cookies()
                self._cookies = {c["name"]: c["value"] for c in raw}
                self._cookies_at = time.time()
            finally:
                try:
                    await page.close()
                    await ctx.close()
                except Exception:
                    pass

    async def _ensure_cookies(self):
        """Garantiza cookies válidas; renueva si expiró el TTL."""
        if time.time() - self._cookies_at >= COOKIE_TTL:
            await self._warm_cookies()

    # ── cliente httpx ────────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-CR,es;q=0.9,en-US;q=0.5,en;q=0.3",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            },
            cookies=self._cookies,
            follow_redirects=True,
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    # ── consulta por cédula ──────────────────────────────────────────────────

    async def consulta_cedula(self, cedula: str) -> dict:
        ced = re.sub(r"\D", "", cedula)
        if not ced:
            raise ValueError("Cédula inválida")

        # Caché hit → respuesta instantánea
        cached = self._cache.get(ced)
        if cached is not None:
            return cached

        async with self._sem:
            return await self._fetch_cedula(ced)

    async def _fetch_cedula(self, ced: str, retry: bool = True) -> dict:
        await self._ensure_cookies()

        try:
            async with self._client() as cli:
                # 1. GET formulario → extraer ViewState ASP.NET
                r_get = await cli.get(URL_CEDULA)
                if _is_blocked(r_get):
                    raise RuntimeError("Radware bloqueó GET formulario")

                vs = _extract_viewstate(r_get.text)

                # 2. POST cédula → redirige a resultado_persona.aspx
                r_post = await cli.post(URL_CEDULA, data={
                    "__VIEWSTATE":          vs["vs"],
                    "__VIEWSTATEGENERATOR": vs["vsg"],
                    "__EVENTVALIDATION":    vs["ev"],
                    "txtcedula":            ced,
                    "btnConsultaCedula":    "Consultar",
                })

                if _is_blocked(r_post):
                    raise RuntimeError("Radware bloqueó POST")

                result = _parse_persona(r_post.text, ced)
        except RuntimeError:
            # Cookies vencidas: renovar y reintentar una vez
            if retry:
                self._cookies_at = 0
                await self._warm_cookies()
                return await self._fetch_cedula(ced, retry=False)
            raise

        if result.get("encontrado"):
            self._cache.set(ced, result)
        return result

    # ── consulta por nombre ──────────────────────────────────────────────────

    async def consulta_nombres(
        self,
        nombre: str,
        apellido1: str = "",
        apellido2: str = "",
        limite: int = 50,
    ) -> dict:
        if not nombre or not nombre.strip():
            raise ValueError("El campo 'nombre' es obligatorio para el TSE")

        async with self._sem:
            return await self._fetch_nombres(nombre, apellido1, apellido2, limite)

    async def _fetch_nombres(
        self,
        nombre: str,
        apellido1: str,
        apellido2: str,
        limite: int,
        retry: bool = True,
    ) -> dict:
        await self._ensure_cookies()

        try:
            async with self._client() as cli:
                r_get = await cli.get(URL_NOMBRES)
                if _is_blocked(r_get):
                    raise RuntimeError("Radware bloqueó GET nombres")

                vs = _extract_viewstate(r_get.text)

                r_post = await cli.post(URL_NOMBRES, data={
                    "__VIEWSTATE":          vs["vs"],
                    "__VIEWSTATEGENERATOR": vs["vsg"],
                    "__EVENTVALIDATION":    vs["ev"],
                    "txtnombre":            nombre,
                    "txtapellido1":         apellido1 or "",
                    "txtapellido2":         apellido2 or "",
                    "btnConsultarNombre":   "Consultar",
                })

                if _is_blocked(r_post):
                    raise RuntimeError("Radware bloqueó POST nombres")

                return _parse_lista_nombres(r_post.text)

        except RuntimeError:
            if retry:
                self._cookies_at = 0
                await self._warm_cookies()
                return await self._fetch_nombres(nombre, apellido1, apellido2, limite, retry=False)
            raise

    # ── health ───────────────────────────────────────────────────────────────

    async def health(self) -> dict:
        cookies_ok = time.time() - self._cookies_at < COOKIE_TTL
        return {
            "status":       "ok" if cookies_ok else "warming",
            "bot_wall":     "superado" if cookies_ok else "pendiente",
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
        "encontrado":     bool(g("lblcedula")),
        "cedula":         g("lblcedula") or ced,
        "nombre_completo": g("lblnombrecompleto"),
        "conocido_como":  g("lblconocidocomo"),
        "fecha_nacimiento": g("lblfechaNacimiento"),
        "edad":           g("lbledad"),
        "nacionalidad":   g("lblnacionalidad"),
        "marginal":       g("lblLeyendaMarginal"),
        "padre": {
            "nombre":         g("lblnombrepadre"),
            "identificacion": g("lblid_padre"),
        },
        "madre": {
            "nombre":         g("lblnombremadre"),
            "identificacion": g("lblid_madre"),
        },
        "fallecido":  bool(defuncion),
        "defuncion":  defuncion or None,
        "fuente":     "resultado_persona.aspx (Registro Civil TSE)",
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
                "cedula":         m.group(1),
                "nombre_completo": _clean(m.group(2)),
                "fallecido":      bool(m.group(3)),
            })
    pag = re.search(r"P[áa]gina\s*#?\s*(\d+)\s*de un total de\s*(\d+)", body, re.I)
    return {
        "total":          len(resultados),
        "pagina":         int(pag.group(1)) if pag else 1,
        "paginas_totales": int(pag.group(2)) if pag else 1,
        "resultados":     resultados,
        "fuente":         "muestra_nombres.aspx (Registro Civil TSE)",
    }
