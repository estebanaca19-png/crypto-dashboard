from flask import Flask, jsonify, request
from flask_cors import CORS
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
import os, time, threading, logging, json
import psycopg2
from psycopg2.extras import Json

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

API_KEY    = os.environ.get("API_KEY")
SECRET_KEY = os.environ.get("SECRET_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

# ─── PostgreSQL Persistencia ──────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    """Crea la tabla si no existe."""
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value JSONB NOT NULL
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("✓ Base de datos inicializada.")
    except Exception as e:
        logger.error(f"Error iniciando DB: {e}")

def save_state():
    """Guarda posiciones y stats en PostgreSQL."""
    try:
        conn = get_db()
        cur  = conn.cursor()
        data = json.dumps({
            "positions": bot_state["positions"],
            "stats":     bot_state["stats"],
        })
        cur.execute("""
            INSERT INTO bot_state (key, value) VALUES ('state', %s::jsonb)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (data,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error guardando estado en DB: {e}")

def load_state():
    """Carga posiciones y stats desde PostgreSQL al arrancar."""
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT value FROM bot_state WHERE key = 'state'")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            data = row[0]
            bot_state["positions"] = data.get("positions", {})
            bot_state["stats"]     = data.get("stats", bot_state["stats"])
            logger.info(f"✓ Estado restaurado desde DB: {len(bot_state['positions'])} posiciones.")
    except Exception as e:
        logger.error(f"Error cargando estado desde DB: {e}")

# ─── Cache ───────────────────────────────────────────────────────────────────
cache = {}
CACHE_TTL = 5

def get_cache(key):
    if key in cache:
        data, ts = cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
    return None

def set_cache(key, data):
    cache[key] = (data, time.time())

def get_client():
    return Client(API_KEY, SECRET_KEY, tld="com")

# ─── Bot state ────────────────────────────────────────────────────────────────
bot_state = {
    "running": False,
    "pairs": ["BTCUSDT","ETHUSDT","DOGEUSDT","SOLUSDT","BNBUSDT","XRPUSDT","ADAUSDT","AVAXUSDT","MATICUSDT","DOTUSDT","LINKUSDT","LTCUSDT"],
    "profit_target": 0.8,   # % mínimo de ganancia para vender vs precio de compra
    "drop_to_buy": 0.15,    # % de caída en los últimos N segundos para comprar
    "trade_amount": 50,    # USDT por operación
    "interval": 20,         # segundos entre ciclos
    "positions": {},         # {symbol: {buy_price, qty, buy_time}}
    "log": [],
    "stats": {
        "trades": 0,
        "total_pnl": 0.0,
        "cycles": 0,
        "wins": 0,
        "losses": 0,
    },
    "last_prices": {},
    "last_changes": {},  # cambio 24h por par
    "usdt_available": 0.0,
}

bot_thread = None
bot_lock   = threading.Lock()


def bot_log(msg, level="info"):
    entry = {"time": time.strftime("%H:%M:%S"), "msg": msg, "level": level}
    with bot_lock:
        bot_state["log"].append(entry)
        if len(bot_state["log"]) > 200:
            bot_state["log"] = bot_state["log"][-200:]
    logger.info(msg)


def calc_qty(symbol, price, usdt_amount):
    """Calcula la cantidad a comprar según las reglas de LOT_SIZE de Binance."""
    qty = usdt_amount / price
    if symbol in ("DOGEUSDT", "ADAUSDT", "XRPUSDT"):
        return round(qty, 0)   # enteros
    elif symbol in ("MATICUSDT",):
        return round(qty, 1)
    elif symbol in ("LTCUSDT", "BNBUSDT", "SOLUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT"):
        return round(qty, 2)
    elif symbol in ("ETHUSDT",):
        return round(qty, 4)
    elif symbol in ("BTCUSDT",):
        return round(qty, 5)
    else:
        return round(qty, 2)


def bot_cycle():
    client = get_client()
    with bot_lock:
        pairs         = list(bot_state["pairs"])
        profit_target = bot_state["profit_target"] / 100
        drop_to_buy   = bot_state["drop_to_buy"] / 100
        trade_amount  = bot_state["trade_amount"]

    # ── Verificar balance USDT disponible ────────────────────────────────────
    try:
        account = client.get_account()
        usdt_free = 0.0
        for b in account["balances"]:
            if b["asset"] == "USDT":
                usdt_free = float(b["free"])
                break
        with bot_lock:
            bot_state["usdt_available"] = usdt_free
    except Exception as e:
        bot_log(f"✗ Error obteniendo balance: {e}", "error")
        usdt_free = 0.0

    for symbol in pairs:
        try:
            ticker = client.get_ticker(symbol=symbol)
            price  = float(ticker["lastPrice"])

            with bot_lock:
                prev_price = bot_state["last_prices"].get(symbol)
                if prev_price:
                    real_change = ((price - prev_price) / prev_price) * 100
                else:
                    real_change = 0.0
                bot_state["last_prices"][symbol]  = price
                bot_state["last_changes"][symbol] = real_change
                position = bot_state["positions"].get(symbol)

            # ── Señal de COMPRA ───────────────────────────────────────────────
            if position is None and prev_price and real_change <= -drop_to_buy:
                # Verificar que hay suficiente USDT
                if usdt_free < trade_amount:
                    bot_log(f"⚠ Sin USDT suficiente para {symbol} (disponible: ${usdt_free:.2f})", "info")
                    continue
                qty = calc_qty(symbol, price, trade_amount)
                try:
                    order = client.create_order(
                        symbol=symbol,
                        side=SIDE_BUY,
                        type=ORDER_TYPE_MARKET,
                        quantity=qty
                    )
                    fill_price = float(order["fills"][0]["price"]) if order.get("fills") else price
                    costo = fill_price * qty
                    usdt_free -= costo  # actualizar balance local
                    with bot_lock:
                        bot_state["positions"][symbol] = {
                            "buy_price": fill_price,
                            "qty": qty,
                            "cost": costo,
                            "buy_time": time.time(),
                            "order_id": order["orderId"],
                        }
                        bot_state["stats"]["trades"] += 1
                        bot_state["usdt_available"] = usdt_free
                    save_state()
                    bot_log(f"▲ BUY  {symbol} @ ${fill_price:.4f} | qty: {qty} | costo: ${costo:.2f} | caída: {real_change:.3f}%", "buy")
                except Exception as e:
                    bot_log(f"✗ Error BUY {symbol}: {e}", "error")
                    fill_price = float(order["fills"][0]["price"]) if order.get("fills") else price
                    with bot_lock:
                        bot_state["positions"][symbol] = {
                            "buy_price": fill_price,
                            "qty": qty,
                            "buy_time": time.time(),
                            "order_id": order["orderId"],
                        }
                        bot_state["stats"]["trades"] += 1
                    bot_log(f"▲ BUY  {symbol} @ ${fill_price:.4f} | qty: {qty} | caída: {real_change:.3f}%", "buy")
                except Exception as e:
                    bot_log(f"✗ Error BUY {symbol}: {e}", "error")

            # ── Señal de VENTA: precio subió ≥X% respecto al precio de compra ─
            elif position is not None:
                buy_price = position["buy_price"]
                qty       = position["qty"]
                pnl_pct   = (price - buy_price) / buy_price

                if pnl_pct >= profit_target:
                    # Obtener cantidad real disponible en Binance
                    try:
                        asset = symbol.replace("USDT", "")
                        asset_balance = client.get_asset_balance(asset=asset)
                        real_qty = float(asset_balance["free"])
                    except:
                        real_qty = qty

                    # Normalizar según LOT_SIZE
                    if symbol in ("DOGEUSDT", "ADAUSDT", "XRPUSDT"):
                        sell_qty = int(real_qty)
                    elif symbol in ("MATICUSDT",):
                        sell_qty = round(real_qty, 1)
                    elif symbol in ("LTCUSDT","BNBUSDT","SOLUSDT","AVAXUSDT","DOTUSDT","LINKUSDT"):
                        sell_qty = round(real_qty, 2)
                    elif symbol == "ETHUSDT":
                        sell_qty = round(real_qty, 4)
                    elif symbol == "BTCUSDT":
                        sell_qty = round(real_qty, 5)
                    else:
                        sell_qty = round(real_qty, 2)

                    if sell_qty <= 0:
                        bot_log(f"⚠ Sin balance para vender {symbol}", "info")
                        continue
                    try:
                        order = client.create_order(
                            symbol=symbol,
                            side=SIDE_SELL,
                            type=ORDER_TYPE_MARKET,
                            quantity=sell_qty
                        )
                        fill_price = float(order["fills"][0]["price"]) if order.get("fills") else price
                        earned = (fill_price - buy_price) * sell_qty
                        with bot_lock:
                            del bot_state["positions"][symbol]
                            bot_state["stats"]["total_pnl"] += earned
                            bot_state["stats"]["trades"] += 1
                            if earned >= 0:
                                bot_state["stats"]["wins"] += 1
                            else:
                                bot_state["stats"]["losses"] += 1
                        bot_log(f"▼ SELL {symbol} @ ${fill_price:.4f} | +${earned:.2f} USDT | {pnl_pct*100:.2f}%", "sell")
                        save_state()
                    except Exception as e:
                        bot_log(f"✗ Error SELL {symbol}: {e}", "error")
                else:
                    bot_log(f"◷ HOLD {symbol} @ ${price:.4f} | P&L: {pnl_pct*100:.2f}%", "info")

        except Exception as e:
            bot_log(f"✗ Error ciclo {symbol}: {e}", "error")

    with bot_lock:
        bot_state["stats"]["cycles"] += 1


def bot_loop():
    bot_log("▶ Bot iniciado. Operando 24/7.", "info")
    while True:
        with bot_lock:
            running  = bot_state["running"]
            interval = bot_state["interval"]
        if not running:
            break
        try:
            bot_cycle()
        except Exception as e:
            bot_log(f"✗ Error general: {e}", "error")
        time.sleep(interval)
    bot_log("⏹ Bot detenido.", "info")


# ─── Endpoints existentes ─────────────────────────────────────────────────────

@app.route("/precios")
def precios():
    cached = get_cache("precios")
    if cached:
        return jsonify(cached)
    client = get_client()
    tickers = client.get_ticker()
    data = []
    for t in tickers:
        if t["symbol"].endswith("USDT") and float(t["quoteVolume"]) > 0:
            name = t["symbol"].replace("USDT", "")
            if name and name.isascii():
                data.append({
                    "symbol": t["symbol"],
                    "name": name,
                    "price": float(t["lastPrice"]),
                    "change": float(t["priceChangePercent"]),
                    "volume": float(t["quoteVolume"])
                })
    data.sort(key=lambda x: x["volume"], reverse=True)
    set_cache("precios", data)
    return jsonify(data)


@app.route("/historial/<symbol>")
def historial(symbol):
    intervalo = request.args.get("interval", "4h")
    limite    = int(request.args.get("limit", 24))
    cache_key = f"historial_{symbol}_{intervalo}_{limite}"
    cached    = get_cache(cache_key)
    if cached:
        return jsonify(cached)
    try:
        client = get_client()
        intervalos = {
            "1h": Client.KLINE_INTERVAL_1HOUR,
            "4h": Client.KLINE_INTERVAL_4HOUR,
            "1d": Client.KLINE_INTERVAL_1DAY,
            "1w": Client.KLINE_INTERVAL_1WEEK
        }
        velas = client.get_klines(
            symbol=symbol + "USDT",
            interval=intervalos.get(intervalo, Client.KLINE_INTERVAL_4HOUR),
            limit=limite
        )
        data = [{"tiempo": v[0], "open": float(v[1]), "high": float(v[2]),
                 "low": float(v[3]), "close": float(v[4]), "volumen": float(v[5])}
                for v in velas]
        set_cache(cache_key, data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/resumen/<symbol>")
def resumen(symbol):
    cache_key = f"resumen_{symbol}"
    cached    = get_cache(cache_key)
    if cached:
        return jsonify(cached)
    try:
        client  = get_client()
        velas   = client.get_klines(symbol=symbol + "USDT", interval=Client.KLINE_INTERVAL_4HOUR, limit=24)
        ticker  = client.get_ticker(symbol=symbol + "USDT")
        cierres = [float(v[4]) for v in velas]
        volumenes = [float(v[5]) for v in velas]
        precio    = float(ticker["lastPrice"])
        cambio24h = float(ticker["priceChangePercent"])
        max24h    = float(ticker["highPrice"])
        min24h    = float(ticker["lowPrice"])
        vol24h    = float(ticker["quoteVolume"])
        ganancias, perdidas = 0, 0
        for i in range(1, len(cierres)):
            diff = cierres[i] - cierres[i - 1]
            if diff > 0: ganancias += diff
            else: perdidas += abs(diff)
        rsi = 100 if perdidas == 0 else 100 - (100 / (1 + (ganancias / len(cierres)) / (perdidas / len(cierres))))
        mitad     = len(cierres) // 2
        tendencia = "alcista" if sum(cierres[mitad:]) / len(cierres[mitad:]) > sum(cierres[:mitad]) / mitad else "bajista"
        cambio4h  = ((cierres[-1] - cierres[-2]) / cierres[-2]) * 100
        vol_prom  = sum(volumenes) / len(volumenes)
        vol_estado = "alto" if volumenes[-1] > vol_prom * 1.2 else "bajo" if volumenes[-1] < vol_prom * 0.8 else "normal"
        if rsi < 35 and tendencia == "alcista": señal = "COMPRAR"
        elif rsi > 65 and tendencia == "bajista": señal = "VENDER"
        elif rsi < 40: señal = "COMPRAR"
        elif rsi > 60: señal = "VENDER"
        else: señal = "MANTENER"
        prob_subida = 50
        if tendencia == "alcista": prob_subida += 20
        else: prob_subida -= 20
        if rsi < 40: prob_subida += 15
        elif rsi > 60: prob_subida -= 15
        if cambio4h > 0: prob_subida += 10
        else: prob_subida -= 10
        if vol_estado == "alto": prob_subida += 5
        prob_subida = max(5, min(95, prob_subida))
        data = {
            "symbol": symbol, "precio": precio,
            "cambio24h": round(cambio24h, 2), "cambio4h": round(cambio4h, 2),
            "max24h": max24h, "min24h": min24h, "vol24h": round(vol24h, 0),
            "rsi": round(rsi, 1), "tendencia": tendencia,
            "volumen_estado": vol_estado, "señal": señal,
            "prob_subida": round(prob_subida, 0),
            "prob_bajada": round(100 - prob_subida, 0),
            "cierres": cierres[-8:]
        }
        set_cache(cache_key, data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/balance")
def balance():
    cached = get_cache("balance")
    if cached:
        return jsonify(cached)
    try:
        client = get_client()
        cuenta = client.get_account()
        data = []
        for b in cuenta["balances"]:
            if float(b["free"]) > 0 or float(b["locked"]) > 0:
                data.append({"asset": b["asset"], "free": float(b["free"]), "locked": float(b["locked"])})
        set_cache("balance", data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/mi-ip")
def mi_ip():
    import requests
    r = requests.get("https://api.ipify.org?format=json")
    return jsonify(r.json())


@app.route("/bot/set_position", methods=["POST"])
def set_position():
    """Registra manualmente una posición con precio de compra conocido."""
    data = request.get_json(silent=True) or {}
    symbol    = data.get("symbol")
    buy_price = float(data.get("buy_price", 0))
    qty       = float(data.get("qty", 0))
    if not symbol or not buy_price or not qty:
        return jsonify({"ok": False, "msg": "Faltan datos"}), 400
    with bot_lock:
        bot_state["positions"][symbol] = {
            "buy_price": buy_price,
            "qty":       qty,
            "cost":      round(buy_price * qty, 2),
            "buy_time":  time.time(),
            "order_id":  "manual",
        }
    save_state()
    return jsonify({"ok": True, "msg": f"Posición {symbol} registrada @ ${buy_price}"})

@app.route("/bot/portfolio")
def bot_portfolio():
    """Retorna el portafolio real desde Binance con valor actual por moneda."""
    try:
        client  = get_client()
        cuenta  = client.get_account()
        data    = []
        exclude = {"USDT", "BNB"}  # BNB se usa para fees, lo excluimos del portfolio trading

        for b in cuenta["balances"]:
            asset = b["asset"]
            free  = float(b["free"])
            locked = float(b["locked"])
            total = free + locked
            if total <= 0 or asset == "USDT":
                continue
            symbol = asset + "USDT"
            try:
                ticker     = client.get_ticker(symbol=symbol)
                price      = float(ticker["lastPrice"])
                valor_actual = total * price
                if valor_actual < 0.5:  # ignorar dust
                    continue
                # Precio de compra del bot si existe posición
                pos = bot_state["positions"].get(symbol)
                buy_price  = pos["buy_price"] if pos else None
                costo      = pos["cost"] if pos and "cost" in pos else (buy_price * pos["qty"] if pos else None)
                pnl        = (price - buy_price) * total if buy_price else None
                pnl_pct    = ((price - buy_price) / buy_price * 100) if buy_price else None

                data.append({
                    "asset":        asset,
                    "symbol":       symbol,
                    "cantidad":     round(total, 6),
                    "precio":       price,
                    "valor_actual": round(valor_actual, 2),
                    "buy_price":    round(buy_price, 6) if buy_price else None,
                    "costo":        round(costo, 2) if costo else None,
                    "pnl":          round(pnl, 2) if pnl is not None else None,
                    "pnl_pct":      round(pnl_pct, 2) if pnl_pct is not None else None,
                })
            except Exception:
                continue  # par no existe en Binance (ej. activos de staking)

        data.sort(key=lambda x: x["valor_actual"], reverse=True)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Endpoints del Bot ────────────────────────────────────────────────────────

@app.route("/bot/start", methods=["POST"])
def bot_start():
    global bot_thread
    data = request.get_json(silent=True) or {}

    with bot_lock:
        if bot_state["running"]:
            return jsonify({"ok": False, "msg": "Bot ya está corriendo"}), 400

        # Actualizar configuración si se envió
        if "pairs"         in data: bot_state["pairs"]         = data["pairs"]
        if "profit_target" in data: bot_state["profit_target"] = float(data["profit_target"])
        if "drop_to_buy"   in data: bot_state["drop_to_buy"]   = float(data["drop_to_buy"])
        if "trade_amount"  in data: bot_state["trade_amount"]  = float(data["trade_amount"])
        if "interval"      in data: bot_state["interval"]      = int(data["interval"])

        bot_state["running"] = True

    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    return jsonify({"ok": True, "msg": "Bot iniciado"})


@app.route("/bot/stop", methods=["POST"])
def bot_stop():
    with bot_lock:
        if not bot_state["running"]:
            return jsonify({"ok": False, "msg": "Bot no está corriendo"}), 400
        bot_state["running"] = False
    return jsonify({"ok": True, "msg": "Bot deteniendo..."})


@app.route("/bot/status")
def bot_status():
    with bot_lock:
        return jsonify({
            "running":       bot_state["running"],
            "pairs":         bot_state["pairs"],
            "profit_target": bot_state["profit_target"],
            "drop_to_buy":   bot_state["drop_to_buy"],
            "trade_amount":  bot_state["trade_amount"],
            "interval":      bot_state["interval"],
            "positions":     bot_state["positions"],
            "stats":         bot_state["stats"],
            "last_prices":   bot_state["last_prices"],
            "last_changes":  bot_state["last_changes"],
            "usdt_available": bot_state["usdt_available"],
            "log":           bot_state["log"][-50:],
        })


@app.route("/bot/config", methods=["POST"])
def bot_config():
    data = request.get_json(silent=True) or {}
    with bot_lock:
        if "pairs"         in data: bot_state["pairs"]         = data["pairs"]
        if "profit_target" in data: bot_state["profit_target"] = float(data["profit_target"])
        if "drop_to_buy"   in data: bot_state["drop_to_buy"]   = float(data["drop_to_buy"])
        if "trade_amount"  in data: bot_state["trade_amount"]  = float(data["trade_amount"])
        if "interval"      in data: bot_state["interval"]      = int(data["interval"])
    return jsonify({"ok": True, "config": {
        "pairs":         bot_state["pairs"],
        "profit_target": bot_state["profit_target"],
        "drop_to_buy":   bot_state["drop_to_buy"],
        "trade_amount":  bot_state["trade_amount"],
        "interval":      bot_state["interval"],
    }})


# ─── Auto-arranque al cargar el módulo (compatible con gunicorn) ──────────────
def _auto_start():
    global bot_thread
    init_db()       # inicializar tabla en PostgreSQL
    load_state()    # restaurar posiciones y stats desde DB
    bot_state["running"] = True
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    logger.info("🚀 Bot arrancado automáticamente al iniciar el servidor.")

_auto_start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Arrancar bot automáticamente al iniciar
    bot_state["running"] = True
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    logger.info("🚀 Servidor iniciado — bot arrancado automáticamente.")
    app.run(host="0.0.0.0", port=port, debug=False)
