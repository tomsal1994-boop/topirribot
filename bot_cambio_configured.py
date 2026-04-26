#!/usr/bin/env python3
"""
Bot de Casa de Cambio — Telegram + Excel
Recibe mensajes en texto libre, los parsea con IA y los registra en operaciones.xlsx
"""

import os
import json
import logging
import requests
import re
from datetime import datetime
from pathlib import Path
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "8794992146:AAG5hZxAE0pIDTF6fxl-It11aZtRM1lEKzg")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "TU_ANTHROPIC_KEY_AQUI")
EXCEL_FILE      = Path(os.getenv("EXCEL_PATH", str(Path(__file__).parent / "operaciones.xlsx")))
STATE_FILE      = Path(__file__).parent / "state.json"
POLL_INTERVAL   = 2  # segundos entre polls
AUTHORIZED_CHAT = 813807479  # Solo Tomas Salgado

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot_cambio.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── ESTADO ────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_update_id": 0, "op_counter": 0, "saldo_usd": 0.0}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────
def tg(method: str, **kwargs) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    r = requests.post(url, json=kwargs, timeout=15)
    return r.json()

def send(chat_id: int, text: str, parse_mode="HTML"):
    tg("sendMessage", chat_id=chat_id, text=text, parse_mode=parse_mode)

def get_updates(offset: int) -> list:
    res = tg("getUpdates", offset=offset, timeout=30, limit=10)
    return res.get("result", [])

# ─── PARSEO CON IA ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Sos un asistente para una casa de cambio argentina.
Analizá el mensaje y extraé la operación. Respondé SOLO con JSON válido, sin texto extra.

Formato de respuesta:
{
  "tipo": "COMPRA" o "VENTA",
  "divisa": "USD" | "EUR" | "BRL" | "GBP" | "UYU" | "CLP" | u otra,
  "monto": número flotante (cantidad de divisa extranjera),
  "tipo_cambio": número flotante (pesos por unidad de divisa),
  "cliente": "nombre del cliente o 'Sin nombre' si no se menciona",
  "observaciones": "cualquier dato extra mencionado o vacío",
  "valido": true o false
}

Ejemplos:
- "vendí 10k usd a 1250" → tipo:VENTA, divisa:USD, monto:10000, tipo_cambio:1250
- "compré 500 euros a Juan a 1380" → tipo:COMPRA, divisa:EUR, monto:500, tipo_cambio:1380, cliente:Juan
- "cambié 200 dólares a 1295 con comisión especial" → tipo:VENTA, divisa:USD, monto:200, tipo_cambio:1295, obs:comisión especial
- "hola" → valido:false

COMPRA = el cliente te trae divisas y vos le das pesos (comprás divisas).
VENTA = el cliente te pide divisas y vos se las vendés (vendés divisas)."""

def parsear_operacion(texto: str) -> dict | None:
    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": texto}],
            },
            timeout=20,
        )
        data = res.json()
        log.info(f"API response status: {res.status_code}")
        if "content" not in data:
            log.error(f"API error response: {data}")
            return None
        raw = data["content"][0]["text"].strip()
        # Limpiar posibles backticks
        raw = re.sub(r"```json|```", "", raw).strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"Error parseando operación: {e}")
        return None

# ─── EXCEL ─────────────────────────────────────────────────────────────────────
VERDE_BG = "E8F5E9"
ROJO_BG  = "FFEBEE"
AZUL_BG  = "E3F2FD"

def registrar_en_excel(op: dict, state: dict) -> dict:
    wb = load_workbook(EXCEL_FILE)
    ws = wb["Operaciones"]
    ws3 = wb["Config"]

    # Leer comisiones desde Config
    com_compra = float(ws3.cell(row=3, column=2).value or 1.5)
    com_venta  = float(ws3.cell(row=4, column=2).value or 1.5)
    com_pct    = com_compra if op["tipo"] == "COMPRA" else com_venta

    # Calcular
    monto      = float(op["monto"])
    tc         = float(op["tipo_cambio"])
    total_ars  = round(monto * tc, 2)
    com_ars    = round(total_ars * com_pct / 100, 2)
    ganancia   = com_ars  # simplificado; se puede extender con spread

    # Saldo USD
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
        com_pct,
        com_ars,
        ganancia,
        state["saldo_usd"],
        op.get("observaciones", ""),
    ]

    # Encontrar primera fila vacía (desde row 5)
    next_row = 5
    for r in range(5, 1005):
        if ws.cell(row=r, column=1).value is None:
            next_row = r
            break

    color = VERDE_BG if op["tipo"] == "COMPRA" else ROJO_BG
    for col, val in enumerate(fila, 1):
        c = ws.cell(row=next_row, column=col, value=val)
        c.font = Font(name="Arial", size=10)
        c.alignment = Alignment(horizontal="center", vertical="center")
        if col in (4,):  # TIPO con color
            c.fill = PatternFill("solid", fgColor=color)
            c.font = Font(name="Arial", size=10, bold=True,
                          color="27500A" if op["tipo"]=="COMPRA" else "791F1F")

    # Actualizar Resumen
    ws2 = wb["Resumen"]
    fecha_hoy = ahora.strftime("%d/%m/%Y")
    fila_resumen = None
    for r in range(3, 500):
        v = ws2.cell(row=r, column=1).value
        if v == fecha_hoy:
            fila_resumen = r
            break
        if v is None:
            fila_resumen = r
            ws2.cell(row=r, column=1, value=fecha_hoy)
            break

    if fila_resumen:
        if op["tipo"] == "COMPRA":
            prev = ws2.cell(row=fila_resumen, column=2).value or 0
            ws2.cell(row=fila_resumen, column=2, value=prev + 1)
        else:
            prev = ws2.cell(row=fila_resumen, column=3).value or 0
            ws2.cell(row=fila_resumen, column=3, value=prev + 1)
        prev_ars = ws2.cell(row=fila_resumen, column=4).value or 0
        ws2.cell(row=fila_resumen, column=4, value=prev_ars + total_ars)
        prev_com = ws2.cell(row=fila_resumen, column=5).value or 0
        ws2.cell(row=fila_resumen, column=5, value=prev_com + com_ars)
        prev_gan = ws2.cell(row=fila_resumen, column=6).value or 0
        ws2.cell(row=fila_resumen, column=6, value=prev_gan + ganancia)

    wb.save(EXCEL_FILE)

    return {
        "op_id": op_id,
        "total_ars": total_ars,
        "com_ars": com_ars,
        "ganancia": ganancia,
        "saldo_usd": state["saldo_usd"],
        "com_pct": com_pct,
    }

# ─── MENSAJE DE CONFIRMACIÓN ───────────────────────────────────────────────────
def mensaje_confirmacion(op: dict, calc: dict) -> str:
    emoji = "🟢" if op["tipo"] == "COMPRA" else "🔴"
    tipo_txt = "COMPRA de divisas" if op["tipo"] == "COMPRA" else "VENTA de divisas"
    return (
        f"{emoji} <b>{calc['op_id']} — {tipo_txt}</b>\n\n"
        f"💱 <b>{op['divisa']}</b>: {op['monto']:,.2f} @ ${op['tipo_cambio']:,.2f}\n"
        f"💵 Total ARS: <b>${calc['total_ars']:,.2f}</b>\n"
        f"📋 Cliente: {op.get('cliente','Sin nombre')}\n"
        f"💼 Comisión ({calc['com_pct']}%): ${calc['com_ars']:,.2f}\n"
        f"📈 Ganancia: <b>${calc['ganancia']:,.2f}</b>\n"
        f"🏦 Saldo USD caja: <b>{calc['saldo_usd']:,.2f}</b>\n\n"
        f"✅ Registrado en Excel — {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )

# ─── COMANDOS ──────────────────────────────────────────────────────────────────
def cmd_saldo(chat_id, state):
    send(chat_id,
        f"🏦 <b>Saldo actual USD en caja:</b> {state['saldo_usd']:,.2f}\n"
        f"📊 Operaciones totales: {state['op_counter']}")

def cmd_ayuda(chat_id):
    send(chat_id,
        "🤖 <b>Bot Casa de Cambio</b>\n\n"
        "<b>Registrar operación:</b> escribí en texto libre\n"
        "Ejemplos:\n"
        "• <i>vendí 5000 usd a 1280</i>\n"
        "• <i>compré 200 euros a maria a 1390</i>\n"
        "• <i>cambié 10k dólares a 1265 cliente: empresa XYZ</i>\n\n"
        "<b>Comandos:</b>\n"
        "/saldo — ver saldo USD en caja\n"
        "/ayuda — ver este mensaje\n\n"
        "Cada operación se guarda automáticamente en el Excel 📁")

# ─── LOOP PRINCIPAL ────────────────────────────────────────────────────────────
def main():
    state = load_state()
    log.info("🏦 Bot Casa de Cambio iniciado")

    while True:
        try:
            updates = get_updates(state["last_update_id"] + 1)
        except Exception as e:
            log.warning(f"Error obteniendo updates: {e}")
            import time; import time as t; t.sleep(5); continue

        for upd in updates:
            state["last_update_id"] = upd["update_id"]
            msg = upd.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            texto = msg.get("text", "").strip()

            if not chat_id or not texto:
                continue

            # Solo responde a Tomas
            if chat_id != AUTHORIZED_CHAT:
                send(chat_id, "⛔ No autorizado.")
                continue

            log.info(f"Mensaje de {chat_id}: {texto}")

            if texto.startswith("/start") or texto.startswith("/ayuda"):
                cmd_ayuda(chat_id)
            elif texto.startswith("/saldo"):
                cmd_saldo(chat_id, state)
            else:
                send(chat_id, "⏳ Procesando operación...")
                op = parsear_operacion(texto)

                if not op or not op.get("valido"):
                    send(chat_id,
                        "❓ No entendí la operación. Probá con:\n"
                        "<i>vendí 1000 usd a 1270</i>\n"
                        "<i>compré 500 euros a Juan a 1385</i>")
                else:
                    try:
                        calc = registrar_en_excel(op, state)
                        save_state(state)
                        send(chat_id, mensaje_confirmacion(op, calc))
                    except Exception as e:
                        log.error(f"Error registrando: {e}")
                        send(chat_id, f"⚠️ Error al guardar en Excel: {e}")

            save_state(state)

        import time
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
