#!/usr/bin/env python3
"""
Bot de Casa de Cambio — Telegram + Google Sheets
"""

import os, json, logging, requests, re
from datetime import datetime

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "8794992146:AAG5hZxAE0pIDTF6fxl-It11aZtRM1lEKzg")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
SHEET_ID        = os.getenv("SHEET_ID", "TU_SHEET_ID_AQUI")
GOOGLE_CREDS    = os.getenv("GOOGLE_CREDS", "")  # JSON como string
STATE_FILE      = "/tmp/state.json"
POLL_INTERVAL   = 2
AUTHORIZED_CHAT = 813807479

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
def get_sheets_token():
    import json, time, base64, hashlib, hmac
    try:
        import jwt
    except ImportError:
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "PyJWT", "cryptography", "-q"])
        import jwt

    creds = json.loads(GOOGLE_CREDS)
    now = int(time.time())
    payload = {
        "iss": creds["client_email"],
        "scope": "https://www.googleapis.com/auth/spreadsheets",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600,
    }
    private_key = creds["private_key"]
    token = jwt.encode(payload, private_key, algorithm="RS256")
    res = requests.post("https://oauth2.googleapis.com/token", data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": token,
    })
    return res.json()["access_token"]

def sheets_append(values: list):
    token = get_sheets_token()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Operaciones!A1:append"
    params = {"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}
    body = {"values": [values]}
    res = requests.post(url, headers={"Authorization": f"Bearer {token}"}, params=params, json=body)
    log.info(f"Sheets append: {res.status_code} - {res.text[:200]}")
    return res.status_code == 200

def sheets_setup():
    """Crea los headers si la hoja está vacía"""
    try:
        token = get_sheets_token()
        # Leer A1
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/A1"
        res = requests.get(url, headers={"Authorization": f"Bearer {token}"})
        data = res.json()
        if "values" not in data:
            # Escribir headers
            headers_url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/A1:N1"
            headers = [["ID","FECHA","HORA","TIPO","DIVISA","MONTO","TIPO CAMBIO","TOTAL ARS",
                        "CLIENTE","COMISION %","COMISION ARS","GANANCIA ARS","SALDO USD","OBSERVACIONES"]]
            requests.put(headers_url,
                headers={"Authorization": f"Bearer {token}"},
                params={"valueInputOption": "USER_ENTERED"},
                json={"values": headers})
            log.info("Headers creados en Google Sheets")
    except Exception as e:
        log.error(f"Error en sheets_setup: {e}")

# ─── ESTADO ────────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"last_update_id": 0, "op_counter": 0, "saldo_usd": 0.0}

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────
def tg(method, **kwargs):
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}", json=kwargs, timeout=15)
    return r.json()

def send(chat_id, text):
    tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")

def get_updates(offset):
    res = tg("getUpdates", offset=offset, timeout=30, limit=10)
    return res.get("result", [])

# ─── PARSEO CON IA ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Sos un asistente para una casa de cambio argentina.
Analizá el mensaje y extraé la operación. Respondé SOLO con JSON válido, sin texto extra ni backticks.

Formato:
{"tipo":"COMPRA o VENTA","divisa":"USD/EUR/BRL/etc","monto":numero,"tipo_cambio":numero,"ganancia":numero o null,"cliente":"nombre o Sin nombre","observaciones":"extra o vacio","valido":true/false}

- ganancia: monto en pesos que el operador dice que ganó. Si no se menciona, poner null.
COMPRA = cliente trae divisas, vos das pesos. VENTA = cliente pide divisas, vos las vendés.
Ejemplos:
"vendí 10k usd a 1250 gané 5000" → tipo:VENTA, divisa:USD, monto:10000, tipo_cambio:1250, ganancia:5000, valido:true
"compré 500 euros a Juan a 1380 gané 2500" → tipo:COMPRA, divisa:EUR, monto:500, tipo_cambio:1380, ganancia:2500, cliente:Juan, valido:true
"vendí 1000 usd a 1280" → tipo:VENTA, divisa:USD, monto:1000, tipo_cambio:1280, ganancia:null, valido:true
"hola" → valido:false"""

def parsear(texto):
    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 300, "system": SYSTEM_PROMPT,
                  "messages": [{"role": "user", "content": texto}]},
            timeout=20,
        )
        data = res.json()
        log.info(f"API status: {res.status_code}")
        if "content" not in data:
            log.error(f"API error: {data}")
            return None
        raw = re.sub(r"```json|```", "", data["content"][0]["text"]).strip()
        parsed = json.loads(raw)
        if isinstance(parsed, list): parsed = parsed[0] if parsed else None
        return parsed
    except Exception as e:
        log.error(f"Error parseando: {e}")
        return None

# ─── REGISTRAR OPERACIÓN ───────────────────────────────────────────────────────

def registrar(op, state):
    monto     = float(op["monto"])
    tc        = float(op["tipo_cambio"])
    total_ars = round(monto * tc, 2)
    ganancia  = float(op["ganancia"]) if op.get("ganancia") else None

    if op["tipo"] == "COMPRA":
        state["saldo_usd"] = round(state["saldo_usd"] + monto, 2)
    else:
        state["saldo_usd"] = round(state["saldo_usd"] - monto, 2)

    state["op_counter"] += 1
    op_id = f"OP-{state['op_counter']:04d}"
    ahora = datetime.now()

    fila = [
        op_id,
        ahora.strftime("%d/%m/%Y"),
        ahora.strftime("%H:%M"),
        op["tipo"],
        op["divisa"],
        monto,
        tc,
        total_ars,
        op.get("cliente", "Sin nombre"),
        ganancia if ganancia is not None else "",
        state["saldo_usd"],
        op.get("observaciones", ""),
    ]

    ok = sheets_append(fila)
    return {"op_id": op_id, "total_ars": total_ars,
            "ganancia": ganancia, "saldo_usd": state["saldo_usd"], "sheets_ok": ok}

def msg_confirmacion(op, calc):
    emoji = "🟢" if op["tipo"] == "COMPRA" else "🔴"
    sheets_status = "✅ Guardado en Google Sheets" if calc["sheets_ok"] else "⚠️ Error al guardar en Sheets"
    return (
        f"{emoji} <b>{calc['op_id']} — {'COMPRA' if op['tipo']=='COMPRA' else 'VENTA'} de divisas</b>\n\n"
        f"💱 <b>{op['divisa']}</b>: {op['monto']:,.2f} @ ${op['tipo_cambio']:,.2f}\n"
        f"💵 Total ARS: <b>${calc['total_ars']:,.2f}</b>\n"
        f"👤 Cliente: {op.get('cliente','Sin nombre')}\n"
        f"📈 Ganancia: <b>${calc['ganancia']:,.2f}</b>\n" if calc.get('ganancia') else "📈 Ganancia: no registrada\n"
        f"🏦 Saldo USD caja: <b>{calc['saldo_usd']:,.2f}</b>\n\n"
        f"{sheets_status} 📊\n"
        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )

# ─── LOOP PRINCIPAL ────────────────────────────────────────────────────────────
def main():
    import time
    log.info("🏦 Bot Casa de Cambio iniciado")
    sheets_setup()
    state = load_state()

    while True:
        try:
            updates = get_updates(state["last_update_id"] + 1)
        except Exception as e:
            log.warning(f"Error updates: {e}")
            time.sleep(5)
            continue

        for upd in updates:
            state["last_update_id"] = upd["update_id"]
            msg = upd.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            texto = msg.get("text", "").strip()
            if not chat_id or not texto:
                continue
            if chat_id != AUTHORIZED_CHAT:
                send(chat_id, "⛔ No autorizado.")
                continue

            log.info(f"Msg: {texto}")

            if texto.startswith("/start") or texto.startswith("/ayuda"):
                send(chat_id,
                    "🤖 <b>Bot Casa de Cambio</b>\n\n"
                    "Escribí en texto libre:\n"
                    "• <i>vendí 5000 usd a 1280</i>\n"
                    "• <i>compré 200 euros a Juan a 1390</i>\n\n"
                    "/saldo — ver saldo USD en caja\n"
                    "/ayuda — este mensaje")
            elif texto.startswith("/saldo"):
                send(chat_id, f"🏦 <b>Saldo USD en caja:</b> {state['saldo_usd']:,.2f}\n📊 Operaciones: {state['op_counter']}")
            else:
                send(chat_id, "⏳ Procesando...")
                op = parsear(texto)
                if not op or not op.get("valido"):
                    send(chat_id, "❓ No entendí. Ejemplo:\n<i>vendí 1000 usd a 1280</i>")
                else:
                    try:
                        calc = registrar(op, state)
                        save_state(state)
                        send(chat_id, msg_confirmacion(op, calc))
                    except Exception as e:
                        log.error(f"Error registrando: {e}")
                        send(chat_id, f"⚠️ Error: {e}")

            save_state(state)
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
