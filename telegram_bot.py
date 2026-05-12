#!/usr/bin/env python3
"""
CruzTrading — Bot de Telegram
Centro de comando completo: alertas + control total desde el teléfono.
"""
import os, time, threading, requests, json, logging, subprocess
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID         = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
BOT_API         = "https://bot.cruztrd.com"
CHECK_INTERVAL  = 60   # segundos entre health checks
last_update_id  = 0

# ─── Estado de monitoreo ──────────────────────────────────────────────────────
monitor_state = {
    "last_cycles":     0,
    "last_cycle_time": time.time(),
    "bot_was_down":    False,
    "alerted_apis":    set(),
    "last_daily_report": "",
    "positions_snapshot": {},
}

# ─── Telegram helpers ─────────────────────────────────────────────────────────
def send(msg, parse_mode="Markdown"):
    """Envía mensaje a Telegram."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram no configurado")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": parse_mode},
            timeout=10
        )
    except Exception as e:
        logger.error(f"Error Telegram: {e}")


def get_updates():
    """Obtiene mensajes nuevos de Telegram."""
    global last_update_id
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 10},
            timeout=15
        )
        return r.json().get("result", [])
    except:
        return []


# ─── Helpers de API ───────────────────────────────────────────────────────────
def get_status():
    try:
        r = requests.get(f"{BOT_API}/bot/status", timeout=10)
        return r.json() if r.ok else None
    except:
        return None


def get_portfolio():
    try:
        r = requests.get(f"{BOT_API}/bot/portfolio", timeout=10)
        return r.json() if r.ok else []
    except:
        return []


def get_futures():
    try:
        r = requests.get(f"{BOT_API}/futures/status", timeout=10)
        return r.json() if r.ok else None
    except:
        return None


def get_balance():
    try:
        r = requests.get(f"{BOT_API}/balance", timeout=10)
        return r.json() if r.ok else []
    except:
        return []


def get_ml_signals():
    try:
        r = requests.get(f"{BOT_API}/bot/ml_signals", timeout=10)
        return r.json() if r.ok else {}
    except:
        return {}


# ─── Comandos ─────────────────────────────────────────────────────────────────
def cmd_status():
    d = get_status()
    if not d:
        return "❌ *Error* — No se puede conectar al servidor"

    running = "✅ Operando 24/7" if d.get("running") else "⏹ Detenido"
    pnl     = d.get("stats", {}).get("total_pnl", 0)
    daily   = d.get("stats", {}).get("daily_pnl", 0)
    trades  = d.get("stats", {}).get("trades", 0)
    wins    = d.get("stats", {}).get("wins", 0)
    losses  = d.get("stats", {}).get("losses", 0)
    cycles  = d.get("stats", {}).get("cycles", 0)
    usdt    = d.get("usdt_available", 0)
    n_pos   = len(d.get("positions", {}))
    wr      = round(wins/trades*100) if trades > 0 else 0

    fg = d.get("fear_greed", {})
    fg_val = "—"

    pnl_emoji = "📈" if pnl >= 0 else "📉"

    return f"""*CruzTrading Bot — Estado*

{running} | Ciclo #{cycles}

{pnl_emoji} *P&L Total:* `{'+'if pnl>=0 else ''}${pnl:.2f}`
📅 *P&L Hoy:* `{'+'if daily>=0 else ''}${daily:.2f}` / meta $5.00
💰 *USDT libre:* `${usdt:.2f}`
📊 *Posiciones:* {n_pos} abiertas
🎯 *Win rate:* {wr}% ({wins}W / {losses}L / {trades} total)
⚙️ *Config:* caída {d.get('drop_to_buy')}% | trade ${d.get('trade_amount')} | SL {d.get('stop_loss')}%"""


def cmd_portfolio():
    portfolio = get_portfolio()
    if not portfolio:
        return "📭 *Sin posiciones abiertas*"

    lines = ["*Portfolio actual:*\n"]
    total_inv = 0
    total_val = 0

    for p in portfolio:
        pnl    = p.get("pnl", 0) or 0
        emoji  = "🟢" if pnl >= 0 else "🔴"
        lines.append(
            f"{emoji} *{p['asset']}* — ${p['valor_actual']:.2f} "
            f"({'+'if pnl>=0 else ''}${pnl:.2f} / {p.get('pnl_pct',0):.2f}%)"
        )
        total_inv += p.get("costo", 0) or 0
        total_val += p.get("valor_actual", 0)

    total_pnl = total_val - total_inv
    lines.append(f"\n💼 *Total:* ${total_val:.2f} | P&L: {'+'if total_pnl>=0 else ''}${total_pnl:.2f}")
    return "\n".join(lines)


def cmd_futuros():
    d = get_futures()
    if not d:
        return "❌ *Error* — Futuros no disponible"

    enabled = "✅ Operando" if d.get("enabled") else "⏹ Detenido"
    stats   = d.get("stats", {})
    pnl     = stats.get("total_pnl", 0)
    pos     = d.get("positions", {})

    lines = [f"*Bot de Futuros — {enabled}*\n"]
    lines.append(f"📈 P&L: `{'+'if pnl>=0 else ''}${pnl:.2f}`")
    lines.append(f"🎯 Trades: {stats.get('trades',0)} | W:{stats.get('wins',0)} L:{stats.get('losses',0)}")
    lines.append(f"⚙️ Capital: ${d.get('capital')} | 2x | SL:{d.get('stop_loss')}% TP:{d.get('take_profit')}%")

    if pos:
        lines.append("\n*Posiciones abiertas:*")
        for sym, p in pos.items():
            side = "LONG 🟢" if p["side"] == "BUY" else "SHORT 🔴"
            lines.append(f"• {sym.replace('USDT','')} {side} @ ${p['entry']:.2f} | score:{p.get('score','—')}")
    else:
        lines.append("\n_Sin posiciones abiertas_")

    return "\n".join(lines)


def cmd_balance():
    balances = get_balance()
    if not balances:
        return "❌ *Error* — No se puede obtener balance"

    lines = ["*Balance en Binance:*\n"]
    total = 0
    for b in balances:
        asset = b.get("asset")
        free  = float(b.get("free", 0))
        if asset == "USDT":
            lines.insert(1, f"💵 *USDT:* `${free:.2f}`")
            total += free
        elif free > 0:
            lines.append(f"• *{asset}:* `{free:.6f}`")

    return "\n".join(lines)


def cmd_apis():
    signals = get_ml_signals()
    lines   = ["*Estado de APIs ML:*\n"]

    apis = {
        "Taapi":       signals.get("BTCUSDT", {}).get("taapi"),
        "Santiment":   signals.get("BTCUSDT", {}).get("santiment"),
        "CryptoQuant": signals.get("BTCUSDT", {}).get("cryptoquant"),
        "OpenAI":      signals.get("openai"),
        "Fear&Greed":  signals.get("fear_greed"),
    }

    status = get_status()
    binance_ok = status and len(status.get("last_prices", {})) > 0

    for name, data in apis.items():
        if data:
            emoji = "✅"
            if name == "Fear&Greed":
                detail = f"{data.get('value')} ({data.get('label')})"
            elif name == "OpenAI":
                detail = data.get("signal", "neutral")
            elif name == "Taapi":
                detail = f"RSI:{data.get('rsi')} score:{data.get('buy_score')}/3"
            else:
                detail = "activa"
        else:
            emoji  = "⚠️"
            detail = "sin datos"
        lines.append(f"{emoji} *{name}:* {detail}")

    lines.append(f"{'✅' if binance_ok else '❌'} *Binance:* {'conectado' if binance_ok else 'error'}")
    return "\n".join(lines)


def cmd_start_bot():
    try:
        r = requests.post(f"{BOT_API}/bot/start",
            json={"drop_to_buy":1.5,"trade_amount":15,"stop_loss":2.0,"max_positions":6},
            timeout=10)
        d = r.json()
        return "✅ *Bot iniciado*" if d.get("ok") else f"❌ {d.get('msg')}"
    except:
        return "❌ Error al iniciar el bot"


def cmd_stop_bot():
    try:
        r = requests.post(f"{BOT_API}/bot/stop", timeout=10)
        d = r.json()
        return "⏹ *Bot detenido*" if d.get("ok") else f"❌ {d.get('msg')}"
    except:
        return "❌ Error al detener el bot"


def cmd_reporte():
    d       = get_status()
    port    = get_portfolio()
    futures = get_futures()

    if not d:
        return "❌ Error obteniendo reporte"

    stats  = d.get("stats", {})
    pnl    = stats.get("total_pnl", 0)
    daily  = stats.get("daily_pnl", 0)
    trades = stats.get("trades", 0)
    wins   = stats.get("wins", 0)
    wr     = round(wins/trades*100) if trades > 0 else 0

    total_inv = sum(p.get("costo",0) or 0 for p in port)
    total_val = sum(p.get("valor_actual",0) for p in port)
    open_pnl  = total_val - total_inv

    fut_pnl = futures.get("stats",{}).get("total_pnl",0) if futures else 0

    return f"""📊 *Reporte CruzTrading — {datetime.now().strftime('%d/%m/%Y %H:%M')}*

*Bot Spot:*
• P&L Total: `{'+'if pnl>=0 else ''}${pnl:.2f}`
• P&L Hoy: `{'+'if daily>=0 else ''}${daily:.2f}`
• P&L Posiciones abiertas: `{'+'if open_pnl>=0 else ''}${open_pnl:.2f}`
• Win rate: {wr}% ({wins}W de {trades} trades)
• USDT libre: `${d.get('usdt_available',0):.2f}`

*Bot Futuros:*
• P&L Total: `{'+'if fut_pnl>=0 else ''}${fut_pnl:.2f}`

*Total general: `{'+'if (pnl+fut_pnl)>=0 else ''}${(pnl+fut_pnl):.2f}`*"""


def cmd_help():
    return """*CruzTrading Bot — Comandos*

📊 `/status` — Estado del bot
💼 `/portfolio` — Posiciones abiertas
📈 `/futuros` — Estado de futuros
💰 `/balance` — Balance Binance
🔌 `/apis` — Estado de APIs
📑 `/reporte` — Reporte completo

▶️ `/start_bot` — Iniciar bot spot
⏹ `/stop_bot` — Detener bot spot

ℹ️ `/help` — Esta ayuda"""


# ─── Procesador de comandos ───────────────────────────────────────────────────
COMMANDS = {
    "/status":     cmd_status,
    "/portfolio":  cmd_portfolio,
    "/futuros":    cmd_futuros,
    "/balance":    cmd_balance,
    "/apis":       cmd_apis,
    "/start_bot":  cmd_start_bot,
    "/stop_bot":   cmd_stop_bot,
    "/reporte":    cmd_reporte,
    "/help":       cmd_help,
    "/start":      cmd_help,
}


def process_commands():
    """Escucha y procesa comandos de Telegram."""
    global last_update_id
    while True:
        try:
            updates = get_updates()
            for update in updates:
                last_update_id = update["update_id"]
                msg  = update.get("message", {})
                text = msg.get("text", "").strip().split("@")[0]
                chat = str(msg.get("chat", {}).get("id", ""))

                if chat != CHAT_ID:
                    continue

                if text in COMMANDS:
                    response = COMMANDS[text]()
                    send(response)
                else:
                    send(f"Comando no reconocido: `{text}`\nUsa /help para ver los comandos disponibles.")
        except Exception as e:
            logger.error(f"Error procesando comandos: {e}")
        time.sleep(2)


# ─── Monitor de salud ─────────────────────────────────────────────────────────
def health_monitor():
    """Monitorea el estado del bot y envía alertas."""
    while True:
        try:
            d = get_status()

            # ── Bot caído ──────────────────────────────────────────────────
            if not d:
                if not monitor_state["bot_was_down"]:
                    monitor_state["bot_was_down"] = True
                    send("🚨 *ALERTA — Bot no responde*\nIntentando reiniciar...")
                    try:
                        subprocess.run(["systemctl", "restart", "cruztrd"], timeout=30)
                        time.sleep(15)
                        if get_status():
                            send("✅ *Bot reiniciado correctamente*")
                        else:
                            send("❌ *Bot no pudo reiniciarse* — revisión manual necesaria")
                    except Exception as e:
                        send(f"❌ Error al reiniciar: {e}")
                time.sleep(CHECK_INTERVAL)
                continue

            monitor_state["bot_was_down"] = False

            # ── Ciclos no avanzan ──────────────────────────────────────────
            cycles = d.get("stats", {}).get("cycles", 0)
            if cycles == monitor_state["last_cycles"]:
                elapsed = time.time() - monitor_state["last_cycle_time"]
                if elapsed > 120:  # 2 minutos sin ciclos
                    send("⚠️ *ALERTA — Bot congelado*\nLos ciclos no avanzan. Reiniciando...")
                    subprocess.run(["systemctl", "restart", "cruztrd"], timeout=30)
                    monitor_state["last_cycle_time"] = time.time()
            else:
                monitor_state["last_cycles"]     = cycles
                monitor_state["last_cycle_time"] = time.time()

            # ── Stop-loss ejecutado ────────────────────────────────────────
            positions = d.get("positions", {})
            snapshot  = monitor_state["positions_snapshot"]
            for sym in list(snapshot.keys()):
                if sym not in positions:
                    send(f"🛑 *Stop-loss ejecutado* — {sym.replace('USDT','')}\nPosición cerrada automáticamente.")
            monitor_state["positions_snapshot"] = dict(positions)

            # ── Balance bajo ───────────────────────────────────────────────
            usdt = d.get("usdt_available", 0)
            if usdt < 30 and usdt > 0:
                send(f"⚠️ *Balance bajo* — Solo ${usdt:.2f} USDT disponibles")

            # ── Meta diaria alcanzada ──────────────────────────────────────
            daily = d.get("stats", {}).get("daily_pnl", 0)
            goal  = d.get("stats", {}).get("daily_goal", 5)
            today = datetime.now().strftime("%Y-%m-%d")
            if daily >= goal and monitor_state["last_daily_report"] != today + "_goal":
                monitor_state["last_daily_report"] = today + "_goal"
                send(f"🎯 *META DIARIA ALCANZADA*\nGanancia de hoy: +${daily:.2f}")

            # ── Reporte diario a las 9am UTC ───────────────────────────────
            hour = datetime.utcnow().hour
            if hour == 9 and monitor_state["last_daily_report"] != today:
                monitor_state["last_daily_report"] = today
                send(cmd_reporte())

        except Exception as e:
            logger.error(f"Error monitor: {e}")

        time.sleep(CHECK_INTERVAL)


# ─── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("🤖 CruzTrading Telegram Bot iniciando...")
    send("🚀 *CruzTrading Monitor activo*\nEscribe /help para ver los comandos disponibles.")

    # Hilo de comandos
    t1 = threading.Thread(target=process_commands, daemon=True)
    t1.start()

    # Hilo de monitoreo
    t2 = threading.Thread(target=health_monitor, daemon=True)
    t2.start()

    logger.info("✅ Bot de Telegram corriendo")
    t1.join()
