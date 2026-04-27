#!/usr/bin/env python3
import os, json, logging, requests, re, threading, hashlib
from datetime import datetime
try:
    from bs4 import BeautifulSoup
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "beautifulsoup4", "lxml", "-q"])
    from bs4 import BeautifulSoup

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "8794992146:AAG5hZxAE0pIDTF6fxl-It11aZtRM1lEKzg")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
SHEET_ID        = os.getenv("SHEET_ID", "")
GOOGLE_CREDS    = os.getenv("GOOGLE_CREDS", "")
STATE_FILE      = "/tmp/state.json"
SEEN_FILE       = "/tmp/seen_listings.json"
POLL_INTERVAL   = 2
AUTHORIZED_CHAT = 813807479
PRECIO_MAX        = 600000
PRECIO_MIN        = 80000
PRECIO_M2_PALERMO = 3200   # USD/m2 max Palermo Chico
PRECIO_M2_RECOLETA= 2800   # USD/m2 max Recoleta
HORA_ALERTA       = 9

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)
HEADERS_HTTP = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36", "Accept-Language": "es-AR,es;q=0.9"}

def get_sheets_token():
    try: import jwt
    except ImportError:
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "PyJWT", "cryptography", "-q"])
        import jwt
    import time
    creds = json.loads(GOOGLE_CREDS)
    now = int(time.time())
    payload = {"iss": creds["client_email"], "scope": "https://www.googleapis.com/auth/spreadsheets", "aud": "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600}
    token = jwt.encode(payload, creds["private_key"], algorithm="RS256")
    res = requests.post("https://oauth2.googleapis.com/token", data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": token})
    return res.json()["access_token"]

def sheets_append(values):
    token = get_sheets_token()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Hoja1!A1:append"
    res = requests.post(url, headers={"Authorization": f"Bearer {token}"}, params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}, json={"values": [values]})
    log.info(f"Sheets: {res.status_code} {res.text[:100]}")
    return res.status_code == 200

def sheets_setup():
    try:
        token = get_sheets_token()
        res = requests.get(f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Hoja1!A1", headers={"Authorization": f"Bearer {token}"})
        if "values" not in res.json():
            requests.put(f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Hoja1!A1:L1", headers={"Authorization": f"Bearer {token}"}, params={"valueInputOption": "USER_ENTERED"}, json={"values": [["ID","FECHA","HORA","TIPO","DIVISA","MONTO","TIPO CAMBIO","TOTAL ARS","CLIENTE","GANANCIA ARS","SALDO USD","OBSERVACIONES"]]})
            log.info("Headers creados")
    except Exception as e: log.error(f"sheets_setup: {e}")

def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except: return {"last_update_id": 0, "op_counter": 0, "saldo_usd": 0.0}

def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f)

def load_seen():
    try:
        with open(SEEN_FILE) as f: return set(json.load(f))
    except: return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f: json.dump(list(seen), f)

def tg(method, **kwargs):
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}", json=kwargs, timeout=15)
    return r.json()

def send(chat_id, text):
    tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)

def get_updates(offset):
    return tg("getUpdates", offset=offset, timeout=30, limit=10).get("result", [])

SYSTEM_PROMPT = """Sos un asistente para una casa de cambio argentina.
Analizá el mensaje y extraé la operación. Respondé SOLO con JSON válido, sin texto extra ni backticks.
Formato: {"tipo":"COMPRA o VENTA","divisa":"USD/EUR/BRL/etc","monto":numero,"tipo_cambio":numero,"ganancia":numero o null,"cliente":"nombre o Sin nombre","observaciones":"extra o vacio","valido":true/false}
ganancia: monto en pesos que el operador dice que ganó. Si no se menciona, null.
COMPRA=cliente trae divisas. VENTA=cliente pide divisas.
Ejemplos:
"vendí 10k usd a 1250 gané 5000" → {"tipo":"VENTA","divisa":"USD","monto":10000,"tipo_cambio":1250,"ganancia":5000,"cliente":"Sin nombre","observaciones":"","valido":true}
"hola" → {"valido":false}"""

def parsear(texto):
    try:
        res = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 300, "system": SYSTEM_PROMPT, "messages": [{"role": "user", "content": texto}]},
            timeout=20)
        data = res.json()
        log.info(f"API: {res.status_code}")
        if "content" not in data: log.error(f"API err: {data}"); return None
        raw = re.sub(r"```json|```", "", data["content"][0]["text"]).strip()
        parsed = json.loads(raw)
        if isinstance(parsed, list): parsed = parsed[0] if parsed else None
        return parsed
    except Exception as e: log.error(f"parsear: {e}"); return None

def registrar(op, state):
    monto = float(op["monto"]); tc = float(op["tipo_cambio"])
    total_ars = round(monto * tc, 2)
    ganancia = float(op["ganancia"]) if op.get("ganancia") else None
    state["saldo_usd"] = round(state["saldo_usd"] + (monto if op["tipo"]=="COMPRA" else -monto), 2)
    state["op_counter"] += 1
    op_id = f"OP-{state['op_counter']:04d}"
    ahora = datetime.now()
    fila = [op_id, "'"+ahora.strftime("%d/%m/%Y"), "'"+ahora.strftime("%H:%M"), op["tipo"], op["divisa"],
            monto, tc, total_ars, op.get("cliente","Sin nombre"), ganancia if ganancia is not None else "",
            state["saldo_usd"], op.get("observaciones","")]
    ok = sheets_append(fila)
    return {"op_id": op_id, "total_ars": total_ars, "ganancia": ganancia, "saldo_usd": state["saldo_usd"], "sheets_ok": ok}

def msg_confirmacion(op, calc):
    emoji = "🟢" if op["tipo"]=="COMPRA" else "🔴"
    gan = f"📈 Ganancia: <b>${calc['ganancia']:,.0f}</b>\n" if calc.get("ganancia") else "📈 Ganancia: no registrada\n"
    st = "✅ Guardado en Google Sheets" if calc["sheets_ok"] else "⚠️ Error al guardar"
    return (f"{emoji} <b>{calc['op_id']} — {op['tipo']}</b>\n\n"
            f"💱 <b>{op['divisa']}</b>: {op['monto']:,.2f} @ ${op['tipo_cambio']:,.2f}\n"
            f"💵 Total ARS: <b>${calc['total_ars']:,.2f}</b>\n"
            f"👤 {op.get('cliente','Sin nombre')}\n{gan}"
            f"🏦 Saldo USD: <b>{calc['saldo_usd']:,.2f}</b>\n\n{st} 📊\n"
            f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}")

def scrape_zonaprop():
    res = []
    for amb in [2,3,4]:
        try:
            r = requests.get(f"https://www.zonaprop.com.ar/departamentos-venta-palermo-chico-{amb}-dormitorios-con-cochera-precio-hasta-{PRECIO_MAX}usd.html", headers=HEADERS_HTTP, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            for card in soup.select("[data-id]"):
                pid = card.get("data-id",""); 
                if not pid: continue
                pe = card.select_one("[data-price]"); precio = int(pe.get("data-price","0")) if pe else 0
                le = card.select_one("a[href]"); href = le["href"] if le else ""
                link = f"https://www.zonaprop.com.ar{href}" if href.startswith("/") else href
                te = card.select_one(".postingCardTitle,h2,.title"); titulo = te.get_text(strip=True) if te else f"{amb} amb"
                de = card.select_one(".postingCardDescription,.description"); desc = de.get_text(strip=True)[:300] if de else ""
                se = card.select_one("[data-surface]"); sup = se.get("data-surface","") if se else ""
                if PRECIO_MIN <= precio <= PRECIO_MAX:
                    res.append({"id":f"zp_{pid}","fuente":"Zonaprop","titulo":titulo,"precio":precio,"link":link,"ambientes":amb,"descripcion":desc,"superficie":sup})
        except Exception as e: log.warning(f"ZP: {e}")
    return res

def scrape_argenprop():
    res = []
    try:
        s = requests.Session(); s.max_redirects = 5
        r = s.get(f"https://www.argenprop.com/departamentos/venta/barrio-palermo-chico?cochera=true&ambientes=2,3,4&precio-hasta={PRECIO_MAX}", headers=HEADERS_HTTP, timeout=15, allow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select(".listing__item,[class*='listing-item']"):
            le = card.select_one("a[href]");
            if not le: continue
            href = le["href"]; pid = hashlib.md5(href.encode()).hexdigest()[:12]
            link = f"https://www.argenprop.com{href}" if href.startswith("/") else href
            pe = card.select_one("[class*='price'],.price"); pt = pe.get_text(strip=True) if pe else ""
            precio = 0
            for p in pt.replace(".","").replace(",","").split():
                if p.isdigit() and len(p)>=5: precio=int(p); break
            te = card.select_one("h2,h3,[class*='title']"); titulo = te.get_text(strip=True) if te else "Depto Palermo Chico"
            de = card.select_one("[class*='description'],p"); desc = de.get_text(strip=True)[:300] if de else ""
            if PRECIO_MIN <= precio <= PRECIO_MAX:
                res.append({"id":f"ap_{pid}","fuente":"Argenprop","titulo":titulo,"precio":precio,"link":link,"ambientes":"2/3/4","descripcion":desc,"superficie":""})
    except Exception as e: log.warning(f"AP: {e}")
    return res


def scrape_zonaprop_recoleta():
    res = []
    for amb in [3,4,5]:
        try:
            r = requests.get(f"https://www.zonaprop.com.ar/departamentos-venta-recoleta-{amb}-dormitorios-con-cochera-precio-hasta-{PRECIO_MAX}usd.html", headers=HEADERS_HTTP, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            for card in soup.select("[data-id]"):
                pid = card.get("data-id","")
                if not pid: continue
                pe = card.select_one("[data-price]"); precio = int(pe.get("data-price","0")) if pe else 0
                le = card.select_one("a[href]"); href = le["href"] if le else ""
                link = f"https://www.zonaprop.com.ar{href}" if href.startswith("/") else href
                te = card.select_one(".postingCardTitle,h2,.title"); titulo = te.get_text(strip=True) if te else f"{amb} amb Recoleta"
                de = card.select_one(".postingCardDescription,.description"); desc = de.get_text(strip=True)[:300] if de else ""
                se = card.select_one("[data-surface]"); sup = se.get("data-surface","") if se else ""
                if PRECIO_MIN <= precio <= PRECIO_MAX:
                    res.append({"id":f"zp_rec_{pid}","fuente":"Zonaprop","titulo":titulo,"precio":precio,"link":link,"ambientes":amb,"descripcion":desc,"superficie":sup,"zona":"recoleta"})
        except Exception as e: log.warning(f"ZP Recoleta: {e}")
    return res

def scrape_argenprop_recoleta():
    res = []
    try:
        s = requests.Session(); s.max_redirects = 5
        r = s.get(f"https://www.argenprop.com/departamentos/venta/barrio-recoleta?cochera=true&ambientes=3,4,5&precio-hasta={PRECIO_MAX}", headers=HEADERS_HTTP, timeout=15, allow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select(".listing__item,[class*='listing-item']"):
            le = card.select_one("a[href]")
            if not le: continue
            href = le["href"]; pid = hashlib.md5(href.encode()).hexdigest()[:12]
            link = f"https://www.argenprop.com{href}" if href.startswith("/") else href
            pe = card.select_one("[class*='price'],.price"); pt = pe.get_text(strip=True) if pe else ""
            precio = 0
            for p in pt.replace(".","").replace(",","").split():
                if p.isdigit() and len(p)>=5: precio=int(p); break
            te = card.select_one("h2,h3,[class*='title']"); titulo = te.get_text(strip=True) if te else "Depto Recoleta"
            de = card.select_one("[class*='description'],p"); desc = de.get_text(strip=True)[:300] if de else ""
            if PRECIO_MIN <= precio <= PRECIO_MAX:
                res.append({"id":f"ap_rec_{pid}","fuente":"Argenprop","titulo":titulo,"precio":precio,"link":link,"ambientes":"3/4/5","descripcion":desc,"superficie":"","zona":"recoleta"})
    except Exception as e: log.warning(f"AP Recoleta: {e}")
    return res


def scrape_mercadolibre_palermo():
    res = []
    try:
        for ambientes in ["2-ambientes", "3-ambientes", "4-ambientes"]:
            url = f"https://inmuebles.mercadolibre.com.ar/departamentos/venta/con-cochera/{ambientes}/palermo-chico-capital-federal/"
            r = requests.get(url, headers=HEADERS_HTTP, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select(".ui-search-result__wrapper, [class*='poly-card']")
            for card in cards:
                try:
                    link_el = card.select_one("a[href]")
                    if not link_el: continue
                    href = link_el["href"]
                    pid = hashlib.md5(href.encode()).hexdigest()[:12]
                    titulo_el = card.select_one("h2, .poly-component__title, [class*='title']")
                    titulo = titulo_el.get_text(strip=True) if titulo_el else "Depto Palermo Chico"
                    precio_el = card.select_one(".andes-money-amount__fraction, [class*='price']")
                    precio_text = precio_el.get_text(strip=True) if precio_el else "0"
                    precio = int(precio_text.replace(".","").replace(",","").strip() or 0)
                    desc_el = card.select_one("[class*='attributes'], [class*='details']")
                    desc = desc_el.get_text(strip=True)[:300] if desc_el else ""
                    if PRECIO_MIN <= precio <= PRECIO_MAX:
                        res.append({"id":f"ml_{pid}","fuente":"MercadoLibre","titulo":titulo,
                                   "precio":precio,"link":href,"ambientes":ambientes.split("-")[0],
                                   "descripcion":desc,"superficie":"","zona":"palermo"})
                except: continue
    except Exception as e: log.warning(f"ML Palermo: {e}")
    return res

def scrape_mercadolibre_recoleta():
    res = []
    try:
        for ambientes in ["3-ambientes", "4-ambientes", "5-ambientes"]:
            url = f"https://inmuebles.mercadolibre.com.ar/departamentos/venta/con-cochera/{ambientes}/recoleta-capital-federal/"
            r = requests.get(url, headers=HEADERS_HTTP, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select(".ui-search-result__wrapper, [class*='poly-card']")
            for card in cards:
                try:
                    link_el = card.select_one("a[href]")
                    if not link_el: continue
                    href = link_el["href"]
                    pid = hashlib.md5(href.encode()).hexdigest()[:12]
                    titulo_el = card.select_one("h2, .poly-component__title, [class*='title']")
                    titulo = titulo_el.get_text(strip=True) if titulo_el else "Depto Recoleta"
                    precio_el = card.select_one(".andes-money-amount__fraction, [class*='price']")
                    precio_text = precio_el.get_text(strip=True) if precio_el else "0"
                    precio = int(precio_text.replace(".","").replace(",","").strip() or 0)
                    desc_el = card.select_one("[class*='attributes'], [class*='details']")
                    desc = desc_el.get_text(strip=True)[:300] if desc_el else ""
                    if PRECIO_MIN <= precio <= PRECIO_MAX:
                        res.append({"id":f"ml_rec_{pid}","fuente":"MercadoLibre","titulo":titulo,
                                   "precio":precio,"link":href,"ambientes":ambientes.split("-")[0],
                                   "descripcion":desc,"superficie":"","zona":"recoleta"})
                except: continue
    except Exception as e: log.warning(f"ML Recoleta: {e}")
    return res

def es_oportunidad(prop):
    texto = ((prop.get("titulo") or "") + " " + (prop.get("descripcion") or "")).lower()
    keywords = ["oportunidad","urgente","liquido","liquida","dueño directo","dueno directo","sin intermediarios","rebajado","gran oportunidad","remate","bajo precio","negociable","oferta","precio reducido","luminoso","buena luz","muy luminoso"]
    kw = any(k in texto for k in keywords)
    zona = prop.get("zona","palermo")
    pm2_limite = PRECIO_M2_RECOLETA if zona == "recoleta" else PRECIO_M2_PALERMO
    pm2_bajo = False; pm2 = None
    try:
        sup = float(''.join(c for c in str(prop.get("superficie","")) if c.isdigit() or c=='.'))
        if sup > 0 and prop.get("precio",0) > 0:
            pm2 = round(prop["precio"] / sup); pm2_bajo = pm2 < pm2_limite
    except: pass
    motivo = ("🏷 Dice oportunidad" if kw else "") + ((" | " if kw else "") + f"📉 USD {pm2}/m²" if pm2_bajo else "")
    return (kw or pm2_bajo), motivo

def check_propiedades():
    seen = load_seen(); nuevas = []
    fuentes = []
    try: fuentes += scrape_zonaprop()
    except Exception as e: log.warning(f"scrape_zonaprop: {e}")
    try: fuentes += scrape_argenprop()
    except Exception as e: log.warning(f"scrape_argenprop: {e}")
    try: fuentes += scrape_zonaprop_recoleta()
    except Exception as e: log.warning(f"scrape_zonaprop_recoleta: {e}")
    try: fuentes += scrape_argenprop_recoleta()
    except Exception as e: log.warning(f"scrape_argenprop_recoleta: {e}")
    for prop in fuentes:
        if prop["id"] not in seen:
            seen.add(prop["id"])
            ok, motivo = es_oportunidad(prop)
            if ok: prop["motivo"] = motivo; nuevas.append(prop)
    save_seen(seen); return nuevas

def msg_depto(p):
    pf = f"USD {p['precio']:,}".replace(",",".")
    zona_emoji = "🌳 Palermo Chico" if p.get("zona","palermo") != "recoleta" else "🏛 Recoleta"
    return (f"🏠 <b>{p['titulo'][:70]}</b>\n💰 {pf} | 🛏 {p['ambientes']} amb | 🚗 Cochera\n"
            f"✅ {p.get('motivo','Oportunidad')}\n📍 {zona_emoji} — {p['fuente']}\n"
            f"🔗 <a href='{p['link']}'>Ver publicación</a>")

def scrape_openinsider():
    resultados = []
    try:
        url = "http://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd=30&fdr=&td=0&tdr=&fdlyl=&fdlyh=&daysago=&xp=1&vl=100&vh=&ocl=&och=&sic1=-1&sicl=100&sich=9999&grp=0&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h=&oc2l=&oc2h=&sortcol=0&cnt=20&action=1"
        r = requests.get(url, headers=HEADERS_HTTP, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        tabla = soup.select_one("table.tinytable")
        if not tabla:
            return []
        for fila in tabla.select("tbody tr")[:20]:
            celdas = fila.select("td")
            if len(celdas) < 12: continue
            try:
                ticker = celdas[3].get_text(strip=True)
                cargo = celdas[6].get_text(strip=True)
                fecha = celdas[1].get_text(strip=True)
                precio = celdas[8].get_text(strip=True)
                qty = celdas[9].get_text(strip=True)
                valor_raw = celdas[10].get_text(strip=True).replace('$','').replace(',','').replace('+','').strip()
                valor_num = float(valor_raw or 0)
                link_el = celdas[3].select_one("a")
                link = f"http://openinsider.com{link_el['href']}" if link_el and link_el.get('href') else f"http://openinsider.com/search?q={ticker}"
                if valor_num >= 100000:
                    resultados.append({"ticker":ticker,"cargo":cargo,"fecha":fecha,"precio_compra":precio,"cantidad":qty,"valor_usd":valor_num,"link":link})
            except: continue
    except Exception as e: log.warning(f"OpenInsider: {e}")
    return resultados

def get_precio_actual(ticker):
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d", headers={"User-Agent":"Mozilla/5.0"}, timeout=8)
        return round(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"], 2)
    except: return None

def get_insiders_claude(insiders_raw):
    if not insiders_raw: return []
    try:
        prompt = f"""Analizá estas compras de insiders recientes (>100k USD, últimos 30 días) y seleccioná las 6 más relevantes:
{json.dumps(insiders_raw[:15], indent=2)}
Priorizá: montos grandes, CEOs/CFOs, empresas conocidas, compras múltiples.
Respondé SOLO con JSON array sin texto extra:
[{{"ticker":"X","cargo":"CEO","nombre_insider":"Nombre","fecha":"2026-04-01","precio_compra":"50.00","valor_usd":500000,"contexto":"Por qué es relevante esta compra","link":"http://openinsider.com/..."}}]"""
        res = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
            json={"model":"claude-haiku-4-5-20251001","max_tokens":2000,"messages":[{"role":"user","content":prompt}]},
            timeout=30)
        data = res.json()
        if "content" not in data: return []
        raw = re.sub(r"```json|```","",data["content"][0]["text"]).strip()
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except Exception as e: log.error(f"get_insiders_claude: {e}"); return []

def formato_insider(ins, idx):
    ticker = ins.get("ticker","?"); cargo = ins.get("cargo","?")
    nombre = ins.get("nombre_insider", ins.get("nombre","?")); fecha = ins.get("fecha","?")
    precio_compra = ins.get("precio_compra","?"); valor = ins.get("valor_usd",0)
    contexto = ins.get("contexto",""); link = ins.get("link","")
    precio_actual = get_precio_actual(ticker)
    try:
        pc = float(str(precio_compra).replace('$','').replace(',',''))
        diff = round(((precio_actual - pc) / pc) * 100, 1) if precio_actual else None
        precio_txt = f"${pc:.2f} → actual ${precio_actual} ({'+' if diff>=0 else ''}{diff}%)" if diff is not None else f"Compra: ${precio_compra}"
    except: precio_txt = f"Compra: ${precio_compra}"
    return (f"{'🟢' if idx<=2 else '🔵'} <b>{ticker}</b> — {cargo}\n"
            f"👤 {nombre} | 📅 {fecha}\n"
            f"💰 USD {valor:,.0f} | 📈 {precio_txt}\n"
            f"💡 {contexto}\n"
            f"🔗 <a href='{link}'>Ver en OpenInsider</a>")

def enviar_alertas_insiders():
    import time
    log.info("Buscando insiders...")
    insiders_raw = scrape_openinsider()
    log.info(f"OpenInsider: {len(insiders_raw)} compras")
    if not insiders_raw:
        send(AUTHORIZED_CHAT, "📊 <b>Insider Trading</b>\n\n😴 Sin compras >100k hoy.\n🔗 <a href='http://openinsider.com'>Ver manualmente</a>")
        return
    top6 = get_insiders_claude(insiders_raw) or insiders_raw[:6]
    send(AUTHORIZED_CHAT, f"📊 <b>Top Insiders Comprando — {datetime.now().strftime('%d/%m/%Y')}</b>\nCompras >USD 100k | Últimos 30 días | Fuente: OpenInsider + SEC Form 4")
    for i, ins in enumerate(top6[:6], 1):
        try: send(AUTHORIZED_CHAT, formato_insider(ins, i)); time.sleep(0.5)
        except Exception as e: log.error(f"insider msg {i}: {e}")
    send(AUTHORIZED_CHAT, "🔗 <b>Ver todos:</b>\n• <a href='http://openinsider.com/screener?fd=30&vl=100&xp=1'>OpenInsider +100k</a>\n• <a href='https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4'>SEC Form 4</a>")

def get_squeeze_claude(tickers_raw):
    try:
        prompt = f"""Sos analista especializado en short squeeze. Analizá estos tickers con alto short interest:
{json.dumps(tickers_raw[:10], indent=2)}
Seleccioná los 5 mejores candidatos a short squeeze. Priorizá: borrow rate >30%, días cubrir >3, catalizador concreto próximo.
Respondé SOLO con JSON array sin texto extra:
[{{"ticker":"X","short_float_pct":25.5,"dias_para_cubrir":3.2,"borrow_rate_pct":45.0,"catalizar":"Earnings 15/05","estrategia_entrada":"Compra sobre $18.50 stop $16 target $24","riesgo_squeeze_fallido":"Si earnings decepciona cae 20%","sentiment":"Retail activo en WSB","fuente_url":"https://finviz.com/quote.ashx?t=X"}}]"""
        res = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
            json={"model":"claude-haiku-4-5-20251001","max_tokens":2500,"messages":[{"role":"user","content":prompt}]},
            timeout=30)
        data = res.json()
        if "content" not in data: return []
        raw = re.sub(r"```json|```","",data["content"][0]["text"]).strip()
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except Exception as e: log.error(f"get_squeeze_claude: {e}"); return []

def enviar_screener_squeeze():
    import time
    log.info("Buscando short squeeze...")
    tickers_raw = [
        {"ticker":"CVNA","short_float":"~25%"},{"ticker":"BYND","short_float":"~30%"},
        {"ticker":"UPST","short_float":"~28%"},{"ticker":"RIVN","short_float":"~22%"},
        {"ticker":"PLUG","short_float":"~35%"},{"ticker":"SOFI","short_float":"~20%"},
        {"ticker":"MSTR","short_float":"~24%"},{"ticker":"COIN","short_float":"~21%"},
        {"ticker":"HOOD","short_float":"~18%"},{"ticker":"IONQ","short_float":"~26%"},
    ]
    try:
        url = "http://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd=30&fdr=&td=0"
        r = requests.get(f"https://finviz.com/screener.ashx?v=111&f=sh_short_o20&o=-short", headers={**HEADERS_HTTP,"Referer":"https://finviz.com/"}, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        finviz_tickers = [{"ticker":el.get_text(strip=True),"short_float":"Finviz >20%"} for el in soup.select("td.screener-short, a.tab-link")[:15] if len(el.get_text(strip=True)) <= 5]
        if finviz_tickers: tickers_raw = finviz_tickers + tickers_raw
    except Exception as e: log.warning(f"Finviz squeeze: {e}")
    top5 = get_squeeze_claude(tickers_raw)
    if not top5:
        send(AUTHORIZED_CHAT, "🔥 <b>Short Squeeze Screener</b>\n\n😴 Sin candidatos fuertes hoy.\n🔗 <a href='https://finviz.com/screener.ashx?v=111&f=sh_short_o20'>Ver Finviz</a>")
        return
    send(AUTHORIZED_CHAT, f"🔥 <b>Short Squeeze Screener — {datetime.now().strftime('%d/%m/%Y')}</b>\nShort Float >20% | Borrow rate elevado | Catalizador próximo\nFuentes: Finviz + ShortQuote + SEC")
    for i, sq in enumerate(top5[:5], 1):
        try:
            ticker=sq.get("ticker","?"); short_pct=sq.get("short_float_pct","?"); dias=sq.get("dias_para_cubrir","?")
            borrow=sq.get("borrow_rate_pct","?"); catalizar=sq.get("catalizar","?")
            estrategia=sq.get("estrategia_entrada","?"); riesgo=sq.get("riesgo_squeeze_fallido","?")
            sentiment=sq.get("sentiment",""); url=sq.get("fuente_url",f"https://finviz.com/quote.ashx?t={ticker}")
            emoji = "🔥" if i==1 else ("⚡" if i==2 else "📌")
            msg = (f"{emoji} <b>#{i} {ticker}</b>\n"
                   f"📊 Short: <b>{short_pct}%</b> | Días cubrir: <b>{dias}</b> | Borrow: <b>{borrow}%</b>\n"
                   f"⚡ Catalizador: {catalizar}\n🎯 Entrada: {estrategia}\n⚠️ Riesgo: {riesgo}\n"
                   + (f"💬 {sentiment}\n" if sentiment else "")
                   + f"🔗 <a href='{url}'>Ver en Finviz</a>")
            send(AUTHORIZED_CHAT, msg); time.sleep(0.8)
        except Exception as e: log.error(f"squeeze msg {i}: {e}")
    send(AUTHORIZED_CHAT, "🔗 <b>Fuentes:</b>\n• <a href='https://finviz.com/screener.ashx?v=111&f=sh_short_o20'>Finviz Short >20%</a>\n• <a href='https://shortquote.com'>ShortQuote.com</a>")


def alertas_loop():
    import time
    log.info("🏠 Alertas inmobiliarias iniciadas")
    ultimo_dia = -1
    while True:
        try:
            ahora = datetime.now()
            if ahora.hour == HORA_ALERTA and ahora.day != ultimo_dia:
                ultimo_dia = ahora.day
                # Alertas inmobiliarias
                nuevas = check_propiedades()
                if nuevas:
                    send(AUTHORIZED_CHAT, f"🏠 <b>{len(nuevas)} oportunidad(es) nueva(s) en Palermo Chico</b>")
                    for p in nuevas[:5]: send(AUTHORIZED_CHAT, msg_depto(p)); time.sleep(1)
                # Alertas insiders
                try:
                    time.sleep(5)
                    enviar_alertas_insiders()
                except Exception as e:
                    log.error(f"alertas insiders: {e}")
                # Short squeeze screener
                try:
                    time.sleep(5)
                    enviar_screener_squeeze()
                except Exception as e:
                    log.error(f"alertas insiders: {e}")
        except Exception as e: log.error(f"alertas_loop: {e}")
        time.sleep(1800)

def main():
    import time
    log.info("🏦 Bot Topirri iniciado")
    sheets_setup()
    state = load_state()
    threading.Thread(target=alertas_loop, daemon=True).start()

    while True:
        try: updates = get_updates(state["last_update_id"] + 1)
        except Exception as e: log.warning(f"updates: {e}"); time.sleep(5); continue

        for upd in updates:
            state["last_update_id"] = upd["update_id"]
            msg = upd.get("message", {}); chat_id = msg.get("chat", {}).get("id"); texto = msg.get("text", "").strip()
            if not chat_id or not texto: continue
            if chat_id != AUTHORIZED_CHAT: send(chat_id, "⛔ No autorizado."); continue
            log.info(f"Msg: {texto}")

            if texto.startswith("/start") or texto.startswith("/ayuda"):
                send(chat_id, "🤖 <b>Bot Topirri</b>\n\n<b>💱 Casa de cambio:</b>\n• <i>vendí 5000 usd a 1280</i>\n• <i>compré 200 euros a Juan a 1390 gané 3000</i>\n\n<b>🏠 Inmuebles:</b>\n• /deptos — oportunidades Palermo Chico\n\n/saldo — saldo USD en caja\n/ayuda — este mensaje")
            elif texto.startswith("/deptos"):
                send(chat_id, "🔍 Buscando oportunidades en Palermo Chico...")
                nuevas = check_propiedades()
                if not nuevas:
                    send(chat_id, "😴 Sin oportunidades nuevas en Palermo Chico ni Recoleta.\nTe aviso a las 9 AM si aparece algo.")
                else:
                    palermo = [p for p in nuevas if p.get("zona","palermo") != "recoleta"]
                    recoleta = [p for p in nuevas if p.get("zona") == "recoleta"]
                    resumen = f"🏠 <b>{len(nuevas)} oportunidad(es)</b>"
                    if palermo: resumen += f"\n🌳 Palermo Chico: {len(palermo)}"
                    if recoleta: resumen += f"\n🏛 Recoleta: {len(recoleta)}"
                    send(chat_id, resumen)
                    for p in nuevas[:6]: send(chat_id, msg_depto(p)); time.sleep(0.5)
            elif texto.startswith("/insiders"):
                send(chat_id, "📊 Buscando insiders comprando... puede tardar 30 segundos.")
                try:
                    enviar_alertas_insiders()
                except Exception as e:
                    log.error(f"insiders cmd: {e}")
                    send(chat_id, f"⚠️ Error: {e}")
            elif texto.startswith("/reset"):
                import os
                try:
                    os.remove(SEEN_FILE)
                    send(chat_id, "✅ Memoria de deptos reseteada. Mañana a las 9 AM vas a recibir todos los listings de nuevo.")
                except:
                    send(chat_id, "ℹ️ No había memoria guardada.")
            elif texto.startswith("/squeeze"):
                send(chat_id, "🔥 Buscando candidatos a short squeeze... puede tardar 30 segundos.")
                try:
                    enviar_screener_squeeze()
                except Exception as e:
                    log.error(f"squeeze cmd: {e}")
                    send(chat_id, f"⚠️ Error: {e}")
            elif texto.startswith("/saldo"):
                send(chat_id, f"🏦 <b>Saldo USD:</b> {state['saldo_usd']:,.2f}\n📊 Operaciones: {state['op_counter']}")
            else:
                send(chat_id, "⏳ Procesando...")
                op = parsear(texto)
                if not op or not op.get("valido"): send(chat_id, "❓ No entendí.\nEjemplo: <i>vendí 1000 usd a 1280 gané 5000</i>")
                else:
                    try:
                        calc = registrar(op, state); save_state(state); send(chat_id, msg_confirmacion(op, calc))
                    except Exception as e: log.error(f"registrar: {e}"); send(chat_id, f"⚠️ Error: {e}")
            save_state(state)
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
