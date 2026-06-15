# API REST TSE Costa Rica

API REST **100% funcional** que envuelve las consultas públicas del Tribunal
Supremo de Elecciones (TSE) de Costa Rica:

- `https://servicioselectorales.tse.go.cr/chc/consulta_cedula.aspx`
- `https://servicioselectorales.tse.go.cr/chc/consulta_nombres.aspx`

## El problema y la solución

Esos sitios están detrás de un **bot manager (Radware)**: cualquier cliente HTTP
plano (curl/requests) recibe `302` hacia un challenge JavaScript
(`stormcaster.js` / `validate.perfdrive.com`) y **nunca** obtiene datos.

Solución: la API conduce **Chromium headless con Playwright** por dentro, que
resuelve el challenge de forma transparente, completa el formulario ASP.NET
(`__VIEWSTATE`/`__EVENTVALIDATION`), sigue el postback y parsea el resultado.

Flujo real descubierto:

| Sitio TSE | Tras enviar | Devuelve |
|-----------|-------------|----------|
| `consulta_cedula.aspx`  | cédula | `resultado_persona.aspx` (datos ricos del Registro Civil) |
| `consulta_nombres.aspx` | nombre | `muestra_nombres.aspx` (lista de cédulas que coinciden) |

## Instalación

```bash
cd tse-padron-api
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

## Ejecutar

```bash
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

Documentación interactiva: `http://localhost:8000/docs`

## Endpoints

### `GET /cedula/{cedula}`
Datos completos de una persona (no disponibles en el padrón descargable):

```bash
curl http://localhost:8000/cedula/109160216
```
```json
{
  "encontrado": true,
  "cedula": "109160216",
  "nombre_completo": "JOSE MANUEL PORRAS AGUERO",
  "fecha_nacimiento": "30/07/1975",
  "edad": "50 AÑOS",
  "nacionalidad": "COSTARRICENSE",
  "marginal": "NO",
  "padre": {"nombre": "JOSE RAMON PORRAS ALVAREZ", "identificacion": "0"},
  "madre": {"nombre": "LIDIETTE AGUERO DELGADO", "identificacion": "0"},
  "fallecido": false,
  "defuncion": null
}
```
- Persona fallecida → `"fallecido": true` con `"defuncion": "DD/MM/AAAA"`.
- Cédula no inscrita → `HTTP 404`.

### `GET /nombres?nombre=&apellido1=&apellido2=&limite=`
El TSE **exige** el campo `nombre`. Devuelve coincidencias con cédula y estado:

```bash
curl "http://localhost:8000/nombres?nombre=JOSE&apellido1=PORRAS&apellido2=AGUERO"
```
```json
{
  "total": 7,
  "pagina": 1,
  "paginas_totales": 1,
  "resultados": [
    {"cedula": "111400817", "nombre_completo": "ESTEBAN JOSE PORRAS AGUERO", "fallecido": false},
    {"cedula": "102670281", "nombre_completo": "JOSE JOAQUIN PORRAS AGUERO", "fallecido": true}
  ]
}
```

### `GET /health`
Verifica que el bot wall se está superando: `{"status":"ok","bot_wall":"superado"}`.

## Notas

- Cada consulta abre una pestaña en el Chromium compartido; concurrencia limitada
  (`max_concurrency=2`) para no gatillar rate-limit del bot manager.
- Latencia típica 3–8 s por consulta (incluye challenge + postback).
- Uso responsable: datos públicos del Registro Civil costarricense.

## Extra offline (opcional): `build_db.py`

El TSE publica el **padrón completo** descargable (~3.7M electores). `build_db.py`
lo baja y arma un SQLite local para lookups masivos offline (cédula→nombre y
nombre→cédula, sin navegador). Da menos campos que la API en vivo (no trae
fecha de nacimiento, padres ni defunción), pero es útil para volumen:

```bash
.venv/bin/python build_db.py   # descarga y construye padron.db
```
