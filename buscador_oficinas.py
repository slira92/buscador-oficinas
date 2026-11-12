"""
Buscador de oficinas - Portal Inmobiliario.

Para cada zona y operacion de config.json recorre la categoria "oficina" con un rango de superficie
ampliado, entra a cada ficha, se queda con lo que cae en la zona (texto o coordenadas), clasifica
habilitacion, calce de m2, quien publica, y calcula UF/m2, costo total con gastos comunes,
comparacion con el mercado de la zona y un puntaje 0-100.
Guarda data/avisos.json, genera dashboard.html y avisa de lo nuevo que calza.

Uso:
    py buscador_oficinas.py                  ejecucion normal
    py buscador_oficinas.py --solo-dashboard recalcula y regenera dashboard.html sin scrapear
"""
import argparse
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
LOGS = BASE / "logs"
AVISOS_JSON = DATA / "avisos.json"
LOCK = DATA / ".lock"
DASHBOARD = BASE / "dashboard.html"
PLANTILLA = BASE / "plantilla_dashboard.html"

URL_BUSQUEDA = "https://www.portalinmobiliario.com/{op}/{tipo}/{slug}/_{campo}*AREA_{a}m²-{b}m²"
URL_FOTO = "https://http2.mlstatic.com/D_NQ_NP_{id}-O.webp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9",
}


# ---------------------------------------------------------------- utilidades (mismas del buscador de locales)

def log(msg):
    LOGS.mkdir(exist_ok=True)
    linea = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(linea, flush=True)
    with open(LOGS / f"oficinas_{datetime.now():%Y-%m}.log", "a", encoding="utf-8") as f:
        f.write(linea + "\n")


def ahora():
    return datetime.now().isoformat(timespec="seconds")


class Bloqueado(Exception):
    pass


class Cliente:
    def __init__(self, pausa):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.pausa = pausa
        self.ultimo = 0.0

    def get(self, url, intentos=3):
        espera = random.uniform(*self.pausa) - (time.time() - self.ultimo)
        if espera > 0:
            time.sleep(espera)
        for i in range(intentos):
            try:
                r = self.s.get(url, timeout=30)
                self.ultimo = time.time()
                if r.status_code == 200:
                    return r.text
                if r.status_code == 404:
                    return None  # el portal responde 404 cuando un filtro no tiene resultados
                if r.status_code in (403, 429):
                    raise Bloqueado(f"HTTP {r.status_code} en {url}")
                log(f"  HTTP {r.status_code} en {url} (intento {i + 1})")
            except requests.RequestException as e:
                log(f"  error de red {e.__class__.__name__} (intento {i + 1})")
            time.sleep(5 * (i + 1))
        return None


def estado_nordic(html):
    m = re.search(r'id="__NORDIC_RENDERING_CTX__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise Bloqueado("la pagina no trae __NORDIC_RENDERING_CTX__ (posible captcha)")
    body = m.group(1)
    d, _ = json.JSONDecoder().raw_decode(body[body.find("{"):])
    pp = (d.get("appProps") or {}).get("pageProps") or {}
    if "initialState" not in pp:
        raise Bloqueado("pagina inesperada (verificacion de cuenta)")
    return pp["initialState"]


def recorrer(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from recorrer(v)
    elif isinstance(o, list):
        for v in o:
            yield from recorrer(v)


def numeros(texto):
    return [float(x.replace(".", "").replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)*", texto or "")]


def monto(texto):
    n = numeros(texto)
    if not n:
        return None, None
    t = (texto or "").upper()
    return n[0], "CLF" if ("UF" in t or "CLF" in t) else "USD" if "USD" in t else "CLP"


MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]


def indicadores(cfg, previo):
    uf = previo.get("uf") or cfg["uf_respaldo"]
    dolar = previo.get("dolar") or cfg["dolar_respaldo"]
    try:
        uf = requests.get("https://mindicador.cl/api/uf", timeout=15).json()["serie"][0]["valor"]
        dolar = requests.get("https://mindicador.cl/api/dolar", timeout=15).json()["serie"][0]["valor"]
    except Exception:
        try:
            hoy = datetime.now()
            html = requests.get(f"https://www.sii.cl/valores_y_fechas/uf/uf{hoy.year}.htm", timeout=20).text
            mes = html.split(f"id='mes_{MESES[hoy.month - 1]}'", 1)[1].split("id='mes_", 1)[0]
            dias = dict(re.findall(r"<strong>(\d+)</strong></th>\s*<td[^>]*>([\d.,]+)</td>", mes))
            uf = numeros(dias[str(hoy.day)])[0]
        except Exception as e:
            log(f"No pude leer UF ({e.__class__.__name__}); uso {uf}")
    return uf, dolar


def a_uf(valor, moneda, uf, dolar):
    if valor is None:
        return None
    if moneda == "CLF":
        return float(valor)
    if moneda == "USD":
        return valor * dolar / uf
    return valor / uf


def escribir_seguro(destino, texto):
    """OneDrive bloquea a ratos el archivo mientras sincroniza: reintenta ~20 s."""
    tmp = destino.with_suffix(".tmp")
    tmp.write_text(texto, encoding="utf-8")
    for _ in range(20):
        try:
            tmp.replace(destino)
            return
        except PermissionError:
            time.sleep(1)
    destino.write_text(texto, encoding="utf-8")
    tmp.unlink(missing_ok=True)


CORREDOR_RE = re.compile(
    r"propiedad|corredor|inmobiliari|gesti[oó]n|asesor|broker|realty|real estate|propert|asociad|&| spa\b|ltda|"
    r"limitada|s\.a\.|group|grupo|arriendo|inversion|capital|homes?\b|house|remax|re/max|century|engel|sotheby|"
    r"keller|toctoc|negocios|consultor|administra|servicios|partners|holding", re.I)


def clasificar_vendedor(v):
    if not v or not (v.get("nombre") or v.get("tipo_ml")):
        return None, None
    if v.get("tipo_ml") == "real_estate_agency" or re.search(r"corredora|tienda", (v.get("encabezado") or "").lower()):
        return "Corredor", None
    if CORREDOR_RE.search(v.get("nombre") or ""):
        return "Corredor", "publica como particular, pero el nombre es de corredora"
    return "Dueño", None


def proceso_vivo(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    codigo = ctypes.c_ulong()
    k.GetExitCodeProcess(h, ctypes.byref(codigo))
    k.CloseHandle(h)
    return codigo.value == 259


def tomar_lock():
    DATA.mkdir(exist_ok=True)
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 90 * 60:
        try:
            pid = int(LOCK.read_text().strip())
        except (ValueError, OSError):
            pid = None
        if pid and proceso_vivo(pid):
            return False
    LOCK.write_text(str(os.getpid()))
    return True


def esperar_red(max_seg=120):
    t0 = time.time()
    while time.time() - t0 < max_seg:
        try:
            requests.head("https://www.portalinmobiliario.com", headers=HEADERS, timeout=10)
            return True
        except requests.RequestException:
            time.sleep(10)
    log(f"Sin internet despues de {max_seg} s")
    return False


def toast(titulo, texto):
    if os.name != "nt":
        return

    def x(s):
        return escape(s, quote=True).replace("'", "&apos;")
    xml = (f"<toast activationType='protocol' launch='{x(DASHBOARD.as_uri())}'><visual><binding template='ToastGeneric'>"
           f"<text>{x(titulo)}</text><text>{x(texto)}</text></binding></visual></toast>")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
        "$d = New-Object Windows.Data.Xml.Dom.XmlDocument; $d.LoadXml(@'\n" + xml + "\n'@);"
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($d))"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                       timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        log(f"No pude mostrar notificacion: {e}")


def publicar_web(cfg):
    pub = cfg.get("publicar_github") or {}
    if not pub.get("activo") or not DASHBOARD.exists():
        return
    tmp = Path(tempfile.mkdtemp(prefix="oficinas_web_"))
    try:
        shutil.copy(DASHBOARD, tmp / "index.html")
        (tmp / ".nojekyll").write_text("")
        git = ["git", "-C", str(tmp), "-c", "user.name=buscador", "-c", "user.email=buscador@users.noreply.github.com"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for args in (["init", "-q", "-b", "gh-pages"], ["add", "-A"], ["commit", "-qm", f"dashboard {ahora()}"],
                     ["push", "-qf", f"https://github.com/{pub['repo']}.git", "gh-pages"]):
            r = subprocess.run(git + args, capture_output=True, text=True, timeout=180, creationflags=flags)
            if r.returncode:
                log(f"No pude publicar en GitHub ({args[0]}): {r.stderr.strip()[:200]}")
                return
        log(f"Dashboard publicado en {pub.get('url', pub['repo'])}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- scraping

def parsear_tarjeta(pc):
    comp = {c.get("id"): c for c in pc.get("components", [])}
    precio = comp.get("price", {}).get("price", {}).get("current_price", {})
    fotos = [p["id"] for p in pc.get("pictures", {}).get("pictures", [])]
    md = pc["metadata"]
    return {
        "id": md["id"],
        "url": "https://" + md["url"].split("#")[0],
        "titulo": comp.get("title", {}).get("title", {}).get("text", ""),
        "direccion": comp.get("location", {}).get("location", {}).get("text", ""),
        "precio": precio.get("value"),
        "moneda": precio.get("currency", "CLP"),
        "atributos": comp.get("attributes_list", {}).get("attributes_list", {}).get("texts", []),
        "foto": URL_FOTO.format(id=fotos[0]) if fotos else None,
    }


def buscar(cli, cfg, b, op):
    """Une los resultados filtrando por superficie util y por superficie total (cada aviso trae una u otra)."""
    tol = cfg["tolerancia_busqueda"]
    a, z = round(b["m2_min"] * (1 - tol)), round(b["m2_max"] * (1 + tol))
    tarjetas, completa = {}, True
    for campo in ("COVERED", "TOTAL"):
        url = URL_BUSQUEDA.format(op=op, tipo=cfg["tipo_portal"], slug=b["slug"], campo=campo, a=a, b=z)
        paginas = 0
        while url and paginas < 10:
            paginas += 1
            html = cli.get(url)
            if html is None:
                break
            st = estado_nordic(html)
            for r in st.get("results", []):
                if "polycard" in r:
                    t = parsear_tarjeta(r["polycard"])
                    tarjetas[t["id"]] = t
            nxt = (st.get("pagination") or {}).get("next_page") or {}
            url = nxt.get("url") if nxt.get("show") else None
        if url:
            completa = False
    log(f"  {b['zona']} {op}: {len(tarjetas)} avisos entre {a} y {z} m²")
    return list(tarjetas.values()), completa


def parsear_detalle(html):
    st = estado_nordic(html)
    specs, lat, lon = {}, None, None
    for o in recorrer(st):
        if lat is None and "latitude" in o and "longitude" in o:
            try:
                lat, lon = float(o["latitude"]), float(o["longitude"])
            except (TypeError, ValueError):
                pass
        i, t = o.get("id"), o.get("text")
        if isinstance(i, str) and isinstance(t, str) and i[:1].isupper() and len(t) < 80:
            specs.setdefault(i, t)
    comp = st.get("components", {})
    sp_vend = comp.get("seller_profile") or {}
    tipo_ml = next((o["seller_type"] for o in recorrer(st) if isinstance(o.get("seller_type"), str)), None)
    fotos = list(dict.fromkeys(re.findall(r'"(\d{5,7}-MLC\d+_\d+)"', json.dumps(comp.get("gallery_mosaic", {})))))
    return {
        "lat": lat, "lon": lon, "specs": specs,
        "descripcion": ((comp.get("description") or {}).get("content", "") or "")[:2500],
        "fotos": [URL_FOTO.format(id=f) for f in fotos[:8]],
        "vendedor": {
            "nombre": ((sp_vend.get("seller_name") or {}).get("title") or {}).get("text", ""),
            "encabezado": (sp_vend.get("header") or {}).get("text", ""),
            "tipo_ml": tipo_ml,
        },
    }


# ---------------------------------------------------------------- evaluacion

HABILITACIONES = [  # orden de prioridad: el primero que calce
    ("Semi habilitada", r"semi[ -]?habilitad|parcialmente habilitad"),
    ("Habilitada", r"(?<!para )(?<!sin )(?<!por )habilitad[ao]s?\b|implementad[ao]|amoblad[ao]|con mobiliario|"
                   r"full equipad|lista para (usar|operar|ocupar)"),
    ("Obra gruesa", r"obra gruesa|en gris|\bcasco\b"),
    ("Planta libre", r"planta libre|para habilitar|sin habilitar|por habilitar|open space"),
]
PUNTOS_HAB = {"Habilitada": 100, "Semi habilitada": 70, "Planta libre": 50, "Obra gruesa": 30, "Sin info": 50}
LEED_RE = re.compile(r"\bleed\b|certificaci[oó]n (verde|sustentable|energ)|edge certif", re.I)
PISO_COMPLETO_RE = re.compile(r"piso (completo|entero|full)|planta completa", re.I)


def clasificar_habilitacion(a):
    texto = f"{a['titulo']} {a.get('descripcion', '')}".lower()
    for nombre, patron in HABILITACIONES:
        if re.search(patron, texto):
            return nombre
    if (a.get("privados") or 0) >= 3:
        return "Habilitada"
    return "Sin info"


def en_zona(a, b):
    texto = f"{a.get('direccion', '')} {a.get('titulo', '')}".lower()
    if b.get("texto_zona") and b["texto_zona"] in texto:
        return True
    caja = b.get("caja")
    if caja and a.get("lat") is not None:
        return caja[0] <= a["lat"] <= caja[1] and caja[2] <= a["lon"] <= caja[3]
    return not caja and not b.get("texto_zona")


def lineal(x, cero, cien):
    return max(0.0, min(100.0, (x - cero) / (cien - cero) * 100))


def mediana(xs):
    xs = sorted(xs)
    n = len(xs)
    return None if not n else xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def primero(sp, *claves):
    for k in claves:
        n = numeros(sp.get(k))
        if n:
            return n[0]
    return None


def evaluar(a, b, cfg, uf, dolar):
    sp = a.get("specs", {})
    util = primero(sp, "Superficie útil")
    total = primero(sp, "Superficie total")
    if not util and not total:  # sin ficha: lo que diga la tarjeta
        for t in a.get("atributos", []):
            if "m²" in t:
                util = numeros(t)[0]
    a["m2_util"], a["m2_total"] = util or None, total or None
    a["m2"] = util or total
    m2 = a["m2"]
    a["en_zona"] = en_zona(a, b)

    est = primero(sp, "Estacionamientos")
    if est is None:
        m = re.search(r"(\d{1,3})\s*(estacionamientos|estac\.|estacionamiento)", (a.get("descripcion") or "").lower())
        est = float(m.group(1)) if m else None
    a["estacionamientos"] = int(est) if est is not None else None
    a["m2_por_estac"] = round(m2 / a["estacionamientos"]) if m2 and a["estacionamientos"] else None
    a["banos"] = int(primero(sp, "Baños") or 0) or None
    a["privados"] = int(primero(sp, "Número de privados") or 0) or None
    a["piso"] = int(primero(sp, "Número de piso de la unidad") or 0) or None
    ant = primero(sp, "Antigüedad")
    a["antiguedad"] = int(ant) if ant else None  # "0 años" casi siempre es "no informado"
    texto = f"{a['titulo']} {a.get('descripcion', '')}"
    a["leed"] = bool(LEED_RE.search(texto))
    a["piso_completo"] = bool(PISO_COMPLETO_RE.search(texto))
    a["habilitacion"] = clasificar_habilitacion(a)
    a["publica"], a["publica_nota"] = clasificar_vendedor(a.get("vendedor"))

    a["precio_uf"] = round(a_uf(a["precio"], a["moneda"], uf, dolar), 1) if a.get("precio") else None
    a["precio_nota"] = None
    if a["precio_uf"] and m2 and a["operacion"] == "arriendo" and a["precio_uf"] < 2:
        # publicado como UF/m2 (ej. "UF 0,22" por 930 m2): se pasa a total mensual
        a["precio_uf"], a["precio_nota"] = round(a["precio_uf"] * m2, 1), "precio publicado por m²"
    a["uf_m2"] = round(a["precio_uf"] / m2, 3) if a["precio_uf"] and m2 else None
    lim = (0.1, 1.5) if a["operacion"] == "arriendo" else (15, 250)
    a["dudoso"] = a["uf_m2"] is not None and not (lim[0] <= a["uf_m2"] <= lim[1])

    # Gastos comunes: muchos corredores ponen el valor por m2 en vez del total
    gv, gm = monto(sp.get("Gastos comunes"))
    a["ggcc_uf"], a["ggcc_nota"] = None, None
    if gv and m2:
        g = a_uf(gv, gm, uf, dolar)
        if 0.02 <= g / m2 <= 0.4:
            a["ggcc_uf"] = round(g, 1)
        elif gm == "CLP" and 500 <= gv <= 15000:
            a["ggcc_uf"], a["ggcc_nota"] = round(gv * m2 / uf, 1), "GGCC informado por m²"
        elif gm == "CLF" and gv < 1:
            a["ggcc_uf"], a["ggcc_nota"] = round(gv * m2, 1), "GGCC informado por m²"
        else:
            a["ggcc_nota"] = f"GGCC informado ({sp.get('Gastos comunes')}) no cuadra con los m²"
    a["costo_uf_m2"] = round((a["precio_uf"] + a["ggcc_uf"]) / m2, 3) if a["precio_uf"] and a["ggcc_uf"] and m2 else None

    tol = cfg["tolerancia_cercano"]
    if not m2:
        a["calce"] = "sin m²"
    elif b["m2_min"] <= m2 <= b["m2_max"]:
        a["calce"] = "calza"
    elif b["m2_min"] * (1 - tol) <= m2 <= b["m2_max"] * (1 + tol):
        a["calce"] = "cercano"
    else:
        a["calce"] = "fuera"
    return a


def marcar_duplicados(avisos):
    """Mismo piso publicado por varios corredores: misma zona, m2, piso, precio y ubicacion aproximada."""
    grupos = {}
    for a in avisos.values():
        a.pop("duplicado_de", None)
        a["tambien_publican"] = []
        if not (a.get("activo") and a.get("en_zona") and a.get("m2") and a.get("precio_uf") and a.get("lat") is not None):
            continue
        # cada corredor pone el pin donde quiere: con piso informado basta la ubicacion a ~1 km;
        # sin piso se exige el mismo pin (~100 m) para no juntar pisos distintos del mismo edificio
        dec = 2 if a.get("piso") else 3
        k = (a["zona"], a["operacion"], round(a["m2"] / 5), a.get("piso"), round(a["precio_uf"] / 3),
             round(a["lat"], dec), round(a["lon"], dec))
        grupos.setdefault(k, []).append(a)
    for xs in grupos.values():
        if len(xs) < 2:
            continue
        xs.sort(key=lambda a: (a.get("publica") != "Dueño", -(a.get("puntaje") or 0), a.get("primera_vez") or ""))
        jefe = xs[0]
        for o in xs[1:]:
            o["duplicado_de"] = jefe["id"]
            jefe["tambien_publican"].append((o.get("vendedor") or {}).get("nombre") or "otro corredor")


def analizar(avisos, cfg):
    pesos = {k: v for k, v in cfg["pesos_puntaje"].items() if not k.startswith("_")}
    grupos = {}
    for a in avisos.values():
        if a.get("activo") and a.get("en_zona") and a.get("uf_m2") and not a["dudoso"]:
            grupos.setdefault((a["zona"], a["operacion"]), []).append(a["uf_m2"])
    gg = {}
    for a in avisos.values():
        if a.get("activo") and a.get("en_zona") and a.get("ggcc_uf") and a.get("m2"):
            gg.setdefault((a["zona"], a["operacion"]), []).append(a["ggcc_uf"] / a["m2"])
    b_por_zona = {b["zona"]: b for b in cfg["busquedas"]}
    for a in avisos.values():
        if not a.get("activo"):
            continue
        ref = grupos.get((a["zona"], a["operacion"]), [])
        med = mediana(ref)
        a["mercado_uf_m2"] = round(med, 3) if med else None
        mg = mediana(gg.get((a["zona"], a["operacion"]), []))
        a["mercado_ggcc_uf_m2"] = round(mg, 3) if mg else None
        a["comps"] = len(ref)
        a["vs_mercado"] = round(a["uf_m2"] / med - 1, 3) if a.get("uf_m2") and med and not a["dudoso"] else None

        b = b_por_zona[a["zona"]]
        m2 = a.get("m2")
        if not m2:
            calce = 0
        elif b["m2_min"] <= m2 <= b["m2_max"]:
            calce = 100
        else:
            lejos = (b["m2_min"] - m2) / b["m2_min"] if m2 < b["m2_min"] else (m2 - b["m2_max"]) / b["m2_max"]
            calce = lineal(lejos, 0.25, 0)
        est100 = (a["estacionamientos"] or 0) / m2 * 100 if m2 else 0
        edif = lineal(a["antiguedad"], 35, 5) if a.get("antiguedad") is not None else 50
        det = {
            "precio_vs_mercado": lineal(a["vs_mercado"], 0.3, -0.3) if a["vs_mercado"] is not None else 50,
            "calce_m2": calce,
            "estacionamientos": lineal(est100, 0, 2.5),
            "habilitacion": PUNTOS_HAB.get(a["habilitacion"], 50),
            "edificio": min(100, edif + (20 if a["leed"] else 0)),
        }
        a["puntaje_det"] = {k: round(v) for k, v in det.items()}
        a["puntaje"] = round(sum(det[k] * pesos[k] for k in det) / sum(pesos.values()))
        r = []
        if a["dudoso"]:
            r.append("⚠ revisar precio o m² (dato inusual)")
        elif a["vs_mercado"] is not None and abs(a["vs_mercado"]) >= 0.1:
            r.append(f"{abs(a['vs_mercado']):.0%} {'bajo' if a['vs_mercado'] < 0 else 'sobre'} la mediana de {a['zona']}")
        if a["habilitacion"] != "Sin info":
            r.append(a["habilitacion"].lower())
        if a["m2_por_estac"]:
            r.append(f"1 estac. cada {a['m2_por_estac']} m²")
        if a["piso_completo"]:
            r.append("piso completo")
        if a["leed"]:
            r.append("certificación LEED")
        if a.get("precio_nota"):
            r.append(a["precio_nota"])
        if a.get("ggcc_nota"):
            r.append(a["ggcc_nota"])
        if a.get("m2_util") and a.get("m2_total") and a["m2_util"] > a["m2_total"] * 1.05:
            r.append("útil mayor que total: revisar m²")
        a["razones"] = r
    marcar_duplicados(avisos)


# ---------------------------------------------------------------- salida

def escribir_dashboard(avisos, meta):
    visibles = [a for a in avisos.values() if a.get("en_zona")]
    payload = {"meta": meta, "avisos": sorted(visibles, key=lambda a: -(a.get("puntaje") or 0))}
    datos = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    escribir_seguro(DASHBOARD, PLANTILLA.read_text(encoding="utf-8").replace("/*__DATOS__*/null", datos, 1))


def construir_meta(cfg, avisos, uf, previo, corrida):
    activos = [a for a in avisos.values() if a.get("activo") and a.get("en_zona") and not a.get("duplicado_de")]
    return {
        "generado": ahora(), "uf": uf,
        "busquedas": cfg["busquedas"], "operaciones": cfg["operaciones"],
        "tolerancia_cercano": cfg["tolerancia_cercano"],
        "pesos": {k: v for k, v in cfg["pesos_puntaje"].items() if not k.startswith("_")},
        "activos": len(activos),
        "calzan": sum(1 for a in activos if a.get("calce") == "calza"),
        "carga_inicial_completa": previo.get("carga_inicial_completa", False),
        "corridas": (previo.get("corridas", []) + ([corrida] if corrida else []))[-50:],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--solo-dashboard", action="store_true")
    args = ap.parse_args()

    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    DATA.mkdir(exist_ok=True)
    avisos = json.loads(AVISOS_JSON.read_text(encoding="utf-8")) if AVISOS_JSON.exists() else {}
    meta_previa = avisos.pop("_meta", {})
    b_por_zona = {b["zona"]: b for b in cfg["busquedas"]}

    if args.solo_dashboard:
        uf = meta_previa.get("uf", cfg["uf_respaldo"])
        for a in avisos.values():
            if a["zona"] in b_por_zona:
                evaluar(a, b_por_zona[a["zona"]], cfg, uf, cfg["dolar_respaldo"])
        analizar(avisos, cfg)
        meta = construir_meta(cfg, avisos, uf, meta_previa, None)
        escribir_seguro(AVISOS_JSON, json.dumps({**avisos, "_meta": meta}, ensure_ascii=False, indent=1))
        escribir_dashboard(avisos, meta)
        log("dashboard.html regenerado")
        publicar_web(cfg)
        return

    log(f"Arranque (pid {os.getpid()})")
    if not tomar_lock():
        log("Otra ejecucion sigue corriendo; salgo")
        return
    vigilante = threading.Timer(50 * 60, lambda: (log("Pasada colgada; la cierro"), LOCK.unlink(missing_ok=True), os._exit(1)))
    vigilante.daemon = True
    vigilante.start()
    t0 = time.time()
    try:
        if not esperar_red():
            return
        carga_inicial = not meta_previa.get("carga_inicial_completa")
        uf, dolar = indicadores(cfg, meta_previa)
        cli = Cliente(cfg["pausa_entre_requests_seg"])
        log(f"Inicio busqueda oficinas (UF {uf:,.0f})")
        vistos, completas, bloqueado, nuevos, fichas = set(), True, None, [], 0
        limite = datetime.now() - timedelta(days=cfg["refrescar_detalle_cada_dias"])
        try:
            for b in cfg["busquedas"]:
                for op in cfg["operaciones"]:
                    tarjetas, ok = buscar(cli, cfg, b, op)
                    completas &= ok
                    for t in tarjetas:
                        k = t["id"]
                        vistos.add(k)
                        prev = avisos.get(k)
                        a = {**(prev or {}), **t, "zona": b["zona"], "operacion": op,
                             "activo": True, "ultima_vez": ahora()}
                        a.setdefault("primera_vez", ahora())
                        if prev is None:
                            a["carga_inicial"] = carga_inicial
                            a["historial_precio"] = [[ahora()[:10], t["precio"], t["moneda"]]]
                        elif (prev.get("precio"), prev.get("moneda")) != (t["precio"], t["moneda"]):
                            a["historial_precio"] = prev.get("historial_precio", []) + [[ahora()[:10], t["precio"], t["moneda"]]]
                        if not a.get("detalle_en") or datetime.fromisoformat(a["detalle_en"]) < limite:
                            html = cli.get(t["url"])
                            if html:
                                try:
                                    a.update(parsear_detalle(html))
                                    a["detalle_en"] = ahora()
                                    fichas += 1
                                except Bloqueado:
                                    raise
                                except Exception as e:
                                    log(f"  no pude leer ficha {k}: {e.__class__.__name__}")
                        calzaba = bool(prev and prev.get("calce") == "calza" and prev.get("en_zona"))
                        evaluar(a, b, cfg, uf, dolar)
                        if a["en_zona"] and a["calce"] == "calza" and not calzaba:
                            nuevos.append(a)
                        avisos[k] = a
        except Bloqueado as e:
            bloqueado, completas = str(e), False
            log(f"BLOQUEO: {e}")

        bajas = 0
        if completas:
            for k, a in avisos.items():
                if k not in vistos and a.get("activo"):
                    a["activo"], a["baja_en"] = False, ahora()
                    bajas += 1
        analizar(avisos, cfg)
        nuevos = [a for a in nuevos if not a.get("duplicado_de")]
        fuera = sum(1 for k in vistos if not avisos[k].get("en_zona"))
        corrida = {"fecha": ahora(), "segundos": round(time.time() - t0), "avisos": len(vistos), "fuera_zona": fuera,
                   "fichas": fichas, "nuevos": len(nuevos), "dados_de_baja": bajas, "bloqueo": bloqueado}
        meta = construir_meta(cfg, avisos, uf, meta_previa, corrida)
        meta["carga_inicial_completa"] = meta_previa.get("carga_inicial_completa") or (completas and not bloqueado)
        escribir_seguro(AVISOS_JSON, json.dumps({**avisos, "_meta": meta}, ensure_ascii=False, indent=1))
        escribir_dashboard(avisos, meta)
        log(f"Fin: {len(vistos)} avisos ({fuera} fuera de zona), {fichas} fichas, {meta['calzan']} calzan, "
            f"{len(nuevos)} nuevos que calzan, {bajas} dados de baja ({corrida['segundos']} s)")
        if nuevos and cfg["notificaciones"].get("windows", True):
            titulo = ("Carga inicial: " if carga_inicial else "") + f"{len(nuevos)} oficinas que calzan"
            toast(titulo, ", ".join(sorted({a["zona"] for a in nuevos})))
        publicar_web(cfg)
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR no controlado: {e.__class__.__name__}: {e}")
        raise
