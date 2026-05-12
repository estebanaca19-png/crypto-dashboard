from flask import Flask, jsonify, request
from flask_cors import CORS
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
import os, time, threading, logging, json
import pg8000.native as pg8000

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

API_KEY    = os.environ.get("API_KEY")
SECRET_KEY = os.environ.get("SECRET_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

# ─── PostgreSQL Persistencia ──────────────────────────────────────────────────
def get_db():
    from urllib.parse import urlparse
    url = urlparse(DATABASE_URL)
    return pg8000.Connection(
        host=url.hostname,
        port=url.port or 5432,
        database=url.path[1:],
        user=url.username,
        password=url.password,
        ssl_context=True
    )

def init_db():
    """Crea la tabla si no existe."""
    try:
        conn = get_db()
        conn.run("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.close()
        logger.info("✓ Base de datos inicializada.")
    except Exception as e:
        logger.error(f"Error iniciando DB: {e}")

def save_state():
    """Guarda posiciones y stats en PostgreSQL."""
    try:
        conn = get_db()
        data = json.dumps({
            "positions": bot_state["positions"],
            "stats":     bot_state["stats"],
        })
        conn.run("""
            INSERT INTO bot_state (key, value) VALUES ('state', :data)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, data=data)
        conn.close()
    except Exception as e:
        logger.error(f"Error guardando estado en DB: {e}")

def load_state():
    """Carga posiciones y stats desde PostgreSQL al arrancar."""
    try:
        conn = get_db()
        rows = conn.run("SELECT value FROM bot_state WHERE key = 'state'")
        conn.close()
        if rows:
            data = json.loads(rows[0][0])
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
    # Pares estables
    "pairs": ["BTCUSDT","ETHUSDT","DOGEUSDT","SOLUSDT","BNBUSDT","XRPUSDT","ADAUSDT","AVAXUSDT","MATICUSDT","DOTUSDT","LINKUSDT","LTCUSDT"],
    # Pares volátiles (monedas meme/alta volatilidad)
    "volatile_pairs": ["SHIBUSDT","PEPEUSDT","WIFUSDT","BONKUSDT","FLOKIUSDT"],
    "profit_target": 0.8,       # % ganancia para vender (pares estables)
    "volatile_profit": 1.5,     # % ganancia para vender (pares volátiles)
    "drop_to_buy": 0.15,        # % caída para comprar
    "trade_amount": 50,         # USDT por operación (estables)
    "volatile_amount": 20,      # USDT por operación (volátiles)
    "interval": 20,
    "positions": {},
    "log": [],
    "stats": {
        "trades": 0,
        "total_pnl": 0.0,
        "cycles": 0,
        "wins": 0,
        "losses": 0,
        "daily_pnl": 0.0,
        "daily_goal": 5.0,      # meta diaria en USDT
        "last_reset": time.strftime("%Y-%m-%d"),
    },
    "last_prices": {},
    "last_changes": {},
    "usdt_available": 0.0,
    "price_history": {},
    "consecutive_drops": {},
    "vol_avg": {},
    "max_positions": 8,
    "stop_loss": 3.0,
    "volatile_stop_loss": 2.0,
    "blacklist": {},
    "blacklist_hours": 4,
    "blacklist_threshold": 5.0,
    # ── DCA (Dollar Cost Averaging) ───────────────────────────────────────────
    "dca_enabled": True,
    "dca_levels": [
        {"drop": 0.15, "amount_pct": 0.4},  # caída 0.15% → compra 40% del trade
        {"drop": 0.30, "amount_pct": 0.35}, # caída 0.30% → compra 35% del trade
        {"drop": 0.50, "amount_pct": 0.25}, # caída 0.50% → compra 25% del trade
    ],
    # ── Reinversión automática ────────────────────────────────────────────────
    "reinvest_enabled": True,
    "reinvest_pct": 80,  # reinvierte el 80% de cada ganancia, 20% queda como reserva
    "total_reinvested": 0.0,
}

bot_thread = None
bot_lock   = threading.Lock()
state_lock = threading.Lock()  # lock ligero solo para modificar posiciones


def bot_log(msg, level="info"):
    entry = {"time": time.strftime("%H:%M:%S"), "msg": msg, "level": level}
    with state_lock:
        bot_state["log"].append(entry)
        if len(bot_state["log"]) > 200:
            bot_state["log"] = bot_state["log"][-200:]
    logger.info(msg)


def get_step_size(client, symbol):
    """Obtiene el step size real de Binance para un símbolo."""
    try:
        info = client.get_symbol_info(symbol)
        for f in info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step = float(f['stepSize'])
                return step
    except:
        pass
    return 0.001

def round_step(qty, step):
    """Redondea la cantidad al step size correcto."""
    import math
    precision = int(round(-math.log10(step)))
    return round(math.floor(qty / step) * step, precision)

def calc_qty(symbol, price, usdt_amount):
    """Calcula la cantidad a comprar según las reglas de LOT_SIZE de Binance."""
    qty = usdt_amount / price
    if symbol in ("DOGEUSDT", "ADAUSDT", "XRPUSDT"):
        return round(qty, 0)
    elif symbol in ("MATICUSDT",):
        return round(qty, 1)
    elif symbol in ("LTCUSDT","BNBUSDT","SOLUSDT","AVAXUSDT","DOTUSDT","LINKUSDT"):
        return round(qty, 2)
    elif symbol in ("ETHUSDT",):
        return round(qty, 4)
    elif symbol in ("BTCUSDT",):
        return round(qty, 5)
    else:
        return round(qty, 2)


def get_rsi(closes, period=14):
    """Calcula el RSI de una lista de precios de cierre."""
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def is_high_volatility_hour():
    """Retorna True si estamos en horario de alta volatilidad cripto (13-21 UTC)."""
    hour = time.gmtime().tm_hour
    return 13 <= hour <= 21


def apply_reinvestment(earned):
    """Reinvierte un porcentaje de la ganancia aumentando el capital por trade."""
    if not bot_state.get("reinvest_enabled"):
        return
    reinvest_pct = bot_state.get("reinvest_pct", 80) / 100
    reinvest_amt = earned * reinvest_pct
    if reinvest_amt <= 0:
        return
    with state_lock:
        old_amount = bot_state["trade_amount"]
        # Aumentar capital por trade gradualmente (máximo $200)
        new_amount = min(old_amount + reinvest_amt, 200)
        bot_state["trade_amount"]      = round(new_amount, 2)
        bot_state["total_reinvested"]  = bot_state.get("total_reinvested", 0) + reinvest_amt
    if new_amount > old_amount:
        bot_log(f"♻ REINVERSIÓN +${reinvest_amt:.2f} → capital por trade: ${new_amount:.2f}", "buy")


def get_dca_level(symbol, current_drop):
    """Determina qué nivel DCA aplicar según la caída acumulada."""
    pos = bot_state["positions"].get(symbol)
    if not pos or not pos.get("dca_entries"):
        return None
    dca_levels  = bot_state.get("dca_levels", [])
    dca_entries = pos.get("dca_entries", [])
    n_entries   = len(dca_entries)
    if n_entries >= len(dca_levels):
        return None  # ya usó todos los niveles DCA
    level = dca_levels[n_entries]
    buy_price   = pos["buy_price"]
    drop_from_buy = ((buy_price - current_drop) / buy_price) * 100
    if drop_from_buy >= level["drop"]:
        return level
    return None
    """Calcula el RSI de una lista de precios de cierre."""
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

def is_blacklisted(symbol):
    """Verifica si un símbolo está en blacklist."""
    blacklist = bot_state.get("blacklist", {})
    if symbol in blacklist:
        if time.time() < blacklist[symbol]:
            return True
        else:
            del bot_state["blacklist"][symbol]
    return False

def add_to_blacklist(symbol, hours=4):
    """Agrega un símbolo a la blacklist por N horas."""
    with state_lock:
        bot_state["blacklist"][symbol] = time.time() + (hours * 3600)
    bot_log(f"🚫 BLACKLIST {symbol} por {hours}h — tendencia bajista detectada", "error")

def check_daily_reset():
    """Resetea el P&L diario si es un nuevo día."""
    today = time.strftime("%Y-%m-%d")
    with state_lock:
        if bot_state["stats"].get("last_reset", "") != today:
            bot_state["stats"]["daily_pnl"]   = 0.0
            bot_state["stats"]["last_reset"]  = today
            bot_state["stats"]["daily_goal"]  = bot_state["stats"].get("daily_goal", 5.0)
            bot_log("📅 Nuevo día — P&L diario reseteado", "info")

def daily_goal_reached():
    """Verifica si se alcanzó la meta diaria."""
    return bot_state["stats"]["daily_pnl"] >= bot_state["stats"]["daily_goal"]
    """Calcula el RSI de una lista de precios de cierre."""
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def execute_sell(client, symbol, position, price, reason=""):
    """Ejecuta una venta y actualiza el estado."""
    buy_price = position["buy_price"]
    qty       = position["qty"]
    pnl_pct   = (price - buy_price) / buy_price

    try:
        asset = symbol.replace("USDT", "")
        real_qty = float(client.get_asset_balance(asset=asset)["free"])
    except:
        real_qty = qty

    step     = get_step_size(client, symbol)
    sell_qty = round_step(real_qty, step)

    if sell_qty <= 0:
        bot_log(f"⚠ Sin balance para vender {symbol}", "info")
        return False

    try:
        order      = client.create_order(symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=sell_qty)
        fill_price = float(order["fills"][0]["price"]) if order.get("fills") else price
        earned     = (fill_price - buy_price) * sell_qty
        with state_lock:
            del bot_state["positions"][symbol]
            bot_state["stats"]["total_pnl"] += earned
            bot_state["stats"]["trades"]    += 1
            if earned >= 0:
                bot_state["stats"]["wins"]   += 1
            else:
                bot_state["stats"]["losses"] += 1
        save_state()
        if earned > 0:
            apply_reinvestment(earned)
        emoji = "▼" if reason == "profit" else "🛑"
        bot_log(f"{emoji} SELL {symbol} @ ${fill_price:.4f} | {'+' if earned>=0 else ''}${earned:.2f} | {pnl_pct*100:.2f}% [{reason}]",
                "sell" if earned >= 0 else "error")
        return True
    except Exception as e:
        bot_log(f"✗ Error SELL {symbol}: {e}", "error")
        return False


def bot_cycle():
    client = get_client()

    # Reset diario
    check_daily_reset()

    with state_lock:
        pairs              = list(bot_state["pairs"])
        volatile_pairs     = list(bot_state["volatile_pairs"])
        profit_target      = bot_state["profit_target"] / 100
        volatile_profit    = bot_state["volatile_profit"] / 100
        drop_to_buy        = bot_state["drop_to_buy"] / 100
        trade_amount       = bot_state["trade_amount"]
        volatile_amount    = bot_state["volatile_amount"]
        max_positions      = bot_state.get("max_positions", 8)
        stop_loss          = bot_state.get("stop_loss", 3.0) / 100
        volatile_stop_loss = bot_state.get("volatile_stop_loss", 2.0) / 100
        blacklist_threshold = bot_state.get("blacklist_threshold", 5.0)
        blacklist_hours    = bot_state.get("blacklist_hours", 4)
        all_pairs          = pairs + [p for p in volatile_pairs if p not in pairs]

    # Balance USDT
    try:
        account   = client.get_account()
        usdt_free = next((float(b["free"]) for b in account["balances"] if b["asset"] == "USDT"), 0.0)
        with state_lock:
            bot_state["usdt_available"] = usdt_free
    except Exception as e:
        bot_log(f"✗ Error obteniendo balance: {e}", "error")
        usdt_free = 0.0

    high_vol = is_high_volatility_hour()

    for symbol in all_pairs:
        try:
            is_volatile = symbol in volatile_pairs
            p_target    = volatile_profit if is_volatile else profit_target
            sl          = volatile_stop_loss if is_volatile else stop_loss
            t_amount    = volatile_amount if is_volatile else trade_amount

            ticker   = client.get_ticker(symbol=symbol)
            price    = float(ticker["lastPrice"])
            vol24h   = float(ticker["quoteVolume"])
            chg24h   = float(ticker["priceChangePercent"])

            # ── Blacklist automática por caída fuerte en 24h ──────────────────
            if chg24h <= -blacklist_threshold and symbol not in bot_state["positions"]:
                if not is_blacklisted(symbol):
                    add_to_blacklist(symbol, blacklist_hours)
                continue

            # ── Saltar si está en blacklist ───────────────────────────────────
            if is_blacklisted(symbol):
                continue

            with state_lock:
                history     = bot_state["price_history"].get(symbol, [])
                history.append(price)
                if len(history) > 20:
                    history = history[-20:]
                bot_state["price_history"][symbol] = history
                prev_price  = bot_state["last_prices"].get(symbol)
                real_change = ((price - prev_price) / prev_price * 100) if prev_price else 0.0
                bot_state["last_prices"][symbol]  = price
                bot_state["last_changes"][symbol] = real_change
                position    = bot_state["positions"].get(symbol)
                n_positions = len(bot_state["positions"])

            rsi = get_rsi(history) if len(history) >= 5 else 50

            # Confirmación caída consecutiva
            drops = bot_state.get("consecutive_drops", {})
            if real_change <= -drop_to_buy:
                drops[symbol] = drops.get(symbol, 0) + 1
            else:
                drops[symbol] = 0
            with state_lock:
                bot_state["consecutive_drops"] = drops
            confirmed_drop = drops.get(symbol, 0) >= 2

            # ════════════════════════════════════════════════════════════════
            # SEÑAL DE COMPRA INICIAL (primera entrada DCA)
            # ════════════════════════════════════════════════════════════════
            if (position is None and
                prev_price and
                confirmed_drop and
                rsi < (55 if high_vol else 50) and
                n_positions < max_positions and
                usdt_free >= t_amount * 0.4 and  # solo necesita 40% del trade para primera entrada
                not daily_goal_reached()):

                # Primera entrada DCA: usa 40% del capital
                first_amount = t_amount * 0.4
                qty  = calc_qty(symbol, price, first_amount)
                step = get_step_size(client, symbol)
                qty  = round_step(qty, step)
                try:
                    order      = client.create_order(symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=qty)
                    fill_price = float(order["fills"][0]["price"]) if order.get("fills") else price
                    costo      = fill_price * qty
                    usdt_free -= costo
                    tag        = "🔥VOLATILE" if is_volatile else "estable"
                    with state_lock:
                        bot_state["positions"][symbol] = {
                            "buy_price":    fill_price,
                            "qty":          qty,
                            "cost":         costo,
                            "buy_time":     time.time(),
                            "order_id":     order["orderId"],
                            "partial_sold": False,
                            "is_volatile":  is_volatile,
                            "dca_entries":  [{"price": fill_price, "qty": qty, "cost": costo}],
                            "avg_price":    fill_price,
                        }
                        bot_state["stats"]["trades"] += 1
                        bot_state["usdt_available"]  = usdt_free
                        drops[symbol] = 0
                    save_state()
                    bot_log(f"▲ BUY [{tag}] {symbol} @ ${fill_price:.4f} | DCA 1/3 ${costo:.2f} | RSI:{rsi}", "buy")
                except Exception as e:
                    bot_log(f"✗ Error BUY {symbol}: {e}", "error")

            # ════════════════════════════════════════════════════════════════
            # DCA: compras adicionales si el precio sigue bajando
            # ════════════════════════════════════════════════════════════════
            elif (position is not None and
                  bot_state.get("dca_enabled") and
                  not position.get("partial_sold")):

                dca_levels  = bot_state.get("dca_levels", [])
                dca_entries = position.get("dca_entries", [])
                n_entries   = len(dca_entries)
                avg_price   = position.get("avg_price", position["buy_price"])

                if n_entries < len(dca_levels):
                    level         = dca_levels[n_entries]
                    drop_from_avg = ((avg_price - price) / avg_price) * 100
                    dca_amount    = t_amount * level["amount_pct"]

                    if drop_from_avg >= level["drop"] and usdt_free >= dca_amount:
                        qty  = calc_qty(symbol, price, dca_amount)
                        step = get_step_size(client, symbol)
                        qty  = round_step(qty, step)
                        try:
                            order      = client.create_order(symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=qty)
                            fill_price = float(order["fills"][0]["price"]) if order.get("fills") else price
                            costo      = fill_price * qty
                            usdt_free -= costo
                            # Recalcular precio promedio
                            total_qty   = position["qty"] + qty
                            total_cost  = position["cost"] + costo
                            new_avg     = total_cost / total_qty
                            with state_lock:
                                bot_state["positions"][symbol]["qty"]     = total_qty
                                bot_state["positions"][symbol]["cost"]    = total_cost
                                bot_state["positions"][symbol]["avg_price"] = new_avg
                                bot_state["positions"][symbol]["dca_entries"].append(
                                    {"price": fill_price, "qty": qty, "cost": costo}
                                )
                                bot_state["usdt_available"] = usdt_free
                            save_state()
                            bot_log(f"▲ DCA {n_entries+1}/{len(dca_levels)} {symbol} @ ${fill_price:.4f} | avg:${new_avg:.4f} | -{drop_from_avg:.2f}%", "buy")
                        except Exception as e:
                            bot_log(f"✗ Error DCA {symbol}: {e}", "error")

            # ════════════════════════════════════════════════════════════════
            # GESTIÓN DE POSICIÓN ABIERTA
            # ════════════════════════════════════════════════════════════════
            elif position is not None:
                buy_price   = position["buy_price"]
                qty         = position["qty"]
                pnl_pct     = (price - buy_price) / buy_price
                pos_volatile = position.get("is_volatile", False)
                p_target    = volatile_profit if pos_volatile else profit_target
                sl          = volatile_stop_loss if pos_volatile else stop_loss

                # 🛑 STOP-LOSS
                if pnl_pct <= -sl:
                    bot_log(f"🛑 STOP-LOSS {symbol} @ ${price:.4f} | {pnl_pct*100:.2f}%", "error")
                    if execute_sell(client, symbol, position, price, reason="stop-loss"):
                        add_to_blacklist(symbol, blacklist_hours * 2)  # doble blacklist tras stop-loss
                        with state_lock:
                            bot_state["stats"]["daily_pnl"] += (price - buy_price) * qty

                # ✅ VENTA PARCIAL al llegar a profit_target
                elif pnl_pct >= p_target and not position.get("partial_sold"):
                    try:
                        asset    = symbol.replace("USDT", "")
                        real_qty = float(client.get_asset_balance(asset=asset)["free"])
                        step     = get_step_size(client, symbol)
                        half_qty = round_step(real_qty * 0.5, step)
                        if half_qty > 0:
                            order      = client.create_order(symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=half_qty)
                            fill_price = float(order["fills"][0]["price"]) if order.get("fills") else price
                            earned     = (fill_price - buy_price) * half_qty
                            with state_lock:
                                bot_state["positions"][symbol]["partial_sold"] = True
                                bot_state["positions"][symbol]["qty"]          = real_qty - half_qty
                                bot_state["stats"]["total_pnl"]  += earned
                                bot_state["stats"]["daily_pnl"]  += earned
                                bot_state["stats"]["trades"]     += 1
                                bot_state["stats"]["wins"]       += 1
                            save_state()
                            bot_log(f"½ SELL 50% {symbol} @ ${fill_price:.4f} | +${earned:.2f} | meta día: ${bot_state['stats']['daily_pnl']:.2f}/${bot_state['stats']['daily_goal']:.2f}", "sell")
                    except Exception as e:
                        bot_log(f"✗ Error venta parcial {symbol}: {e}", "error")

                # ✅ VENTA TOTAL al llegar a profit_target*2
                elif pnl_pct >= p_target * 2 and position.get("partial_sold"):
                    if execute_sell(client, symbol, position, price, reason="profit"):
                        earned = (price - buy_price) * qty
                        with state_lock:
                            bot_state["stats"]["daily_pnl"] += earned
                        if daily_goal_reached():
                            bot_log(f"🎯 META DIARIA ALCANZADA: ${bot_state['stats']['daily_pnl']:.2f} — el bot seguirá vendiendo pero pausará nuevas compras", "buy")

                else:
                    goal     = bot_state["stats"]["daily_goal"]
                    daily    = bot_state["stats"]["daily_pnl"]
                    sl_dist  = (pnl_pct + sl) / sl * 100
                    tag      = "🔥" if pos_volatile else "◷"
                    bot_log(f"{tag} HOLD {symbol} @ ${price:.4f} | P&L:{pnl_pct*100:.2f}% | RSI:{rsi} | SL:{sl_dist:.0f}% | día:${daily:.2f}/${goal:.2f}", "info")

        except Exception as e:
            bot_log(f"✗ Error ciclo {symbol}: {e}", "error")

    with state_lock:
        bot_state["stats"]["cycles"] += 1

    # ── Balance USDT disponible ───────────────────────────────────────────────
    try:
        account   = client.get_account()
        usdt_free = next((float(b["free"]) for b in account["balances"] if b["asset"] == "USDT"), 0.0)
        with state_lock:
            bot_state["usdt_available"] = usdt_free
    except Exception as e:
        bot_log(f"✗ Error obteniendo balance: {e}", "error")
        usdt_free = 0.0

    high_vol = is_high_volatility_hour()

    for symbol in pairs:
        try:
            ticker = client.get_ticker(symbol=symbol)
            price  = float(ticker["lastPrice"])
            vol24h = float(ticker["quoteVolume"])

            with state_lock:
                # Historial de precios para confirmar tendencia
                history = bot_state["price_history"].get(symbol, [])
                history.append(price)
                if len(history) > 20:
                    history = history[-20:]
                bot_state["price_history"][symbol] = history

                prev_price  = bot_state["last_prices"].get(symbol)
                real_change = ((price - prev_price) / prev_price * 100) if prev_price else 0.0
                bot_state["last_prices"][symbol]  = price
                bot_state["last_changes"][symbol] = real_change
                position   = bot_state["positions"].get(symbol)
                n_positions = len(bot_state["positions"])

            # ── RSI con últimos 15 precios ────────────────────────────────────
            rsi = get_rsi(history) if len(history) >= 5 else 50

            # ── Volumen promedio (últimas 20 velas) ───────────────────────────
            vol_avg = bot_state.get("vol_avg", {}).get(symbol, vol24h)
            with state_lock:
                if "vol_avg" not in bot_state:
                    bot_state["vol_avg"] = {}
                bot_state["vol_avg"][symbol] = vol24h * 0.1 + vol_avg * 0.9  # EMA suave

            # ── Confirmación de caída en 3 ciclos consecutivos ────────────────
            drops = bot_state.get("consecutive_drops", {})
            if real_change <= -drop_to_buy:
                drops[symbol] = drops.get(symbol, 0) + 1
            else:
                drops[symbol] = 0
            with state_lock:
                bot_state["consecutive_drops"] = drops
            confirmed_drop = drops.get(symbol, 0) >= 2  # 2 ciclos consecutivos bajando

            # ════════════════════════════════════════════════════════════════
            # SEÑAL DE COMPRA
            # Condiciones: caída confirmada + RSI bajo + volumen ok + capital
            # ════════════════════════════════════════════════════════════════
            if (position is None and
                prev_price and
                confirmed_drop and
                rsi < 50 and
                n_positions < max_positions and
                usdt_free >= trade_amount):

                # En horario de alta volatilidad ser más agresivo
                min_rsi = 55 if high_vol else 50

                if rsi < min_rsi:
                    qty  = calc_qty(symbol, price, trade_amount)
                    step = get_step_size(client, symbol)
                    qty  = round_step(qty, step)
                    try:
                        order      = client.create_order(symbol=symbol, side=SIDE_BUY, type=ORDER_TYPE_MARKET, quantity=qty)
                        fill_price = float(order["fills"][0]["price"]) if order.get("fills") else price
                        costo      = fill_price * qty
                        usdt_free -= costo
                        with state_lock:
                            bot_state["positions"][symbol] = {
                                "buy_price": fill_price,
                                "qty":       qty,
                                "cost":      costo,
                                "buy_time":  time.time(),
                                "order_id":  order["orderId"],
                                "partial_sold": False,
                            }
                            bot_state["stats"]["trades"] += 1
                            bot_state["usdt_available"]  = usdt_free
                            drops[symbol] = 0
                        save_state()
                        bot_log(f"▲ BUY {symbol} @ ${fill_price:.4f} | ${costo:.2f} | RSI:{rsi} | caída:{real_change:.3f}%", "buy")
                    except Exception as e:
                        bot_log(f"✗ Error BUY {symbol}: {e}", "error")

            # ════════════════════════════════════════════════════════════════
            # GESTIÓN DE POSICIÓN ABIERTA
            # ════════════════════════════════════════════════════════════════
            elif position is not None:
                buy_price = position["buy_price"]
                qty       = position["qty"]
                pnl_pct   = (price - buy_price) / buy_price

                # 🛑 STOP-LOSS: vender si pierde más del 3%
                if pnl_pct <= -stop_loss:
                    bot_log(f"🛑 STOP-LOSS {symbol} @ ${price:.4f} | {pnl_pct*100:.2f}%", "error")
                    execute_sell(client, symbol, position, price, reason="stop-loss")

                # ✅ VENTA PARCIAL: vender 50% al llegar a profit_target
                elif pnl_pct >= profit_target and not position.get("partial_sold"):
                    try:
                        asset    = symbol.replace("USDT", "")
                        real_qty = float(client.get_asset_balance(asset=asset)["free"])
                        step     = get_step_size(client, symbol)
                        half_qty = round_step(real_qty * 0.5, step)
                        if half_qty > 0:
                            order      = client.create_order(symbol=symbol, side=SIDE_SELL, type=ORDER_TYPE_MARKET, quantity=half_qty)
                            fill_price = float(order["fills"][0]["price"]) if order.get("fills") else price
                            earned     = (fill_price - buy_price) * half_qty
                            with state_lock:
                                bot_state["positions"][symbol]["partial_sold"] = True
                                bot_state["positions"][symbol]["qty"]          = real_qty - half_qty
                                bot_state["stats"]["total_pnl"] += earned
                                bot_state["stats"]["trades"]    += 1
                                bot_state["stats"]["wins"]      += 1
                            save_state()
                            bot_log(f"½ SELL 50% {symbol} @ ${fill_price:.4f} | +${earned:.2f} | esperando +{profit_target*2*100:.1f}%", "sell")
                    except Exception as e:
                        bot_log(f"✗ Error venta parcial {symbol}: {e}", "error")

                # ✅ VENTA TOTAL: vender el resto al llegar a profit_target*2
                elif pnl_pct >= profit_target * 2 and position.get("partial_sold"):
                    execute_sell(client, symbol, position, price, reason="profit")

                # ✅ VENTA TOTAL SIMPLE: si no hizo venta parcial y ya llegó al objetivo
                elif pnl_pct >= profit_target and not position.get("partial_sold"):
                    execute_sell(client, symbol, position, price, reason="profit")

                else:
                    sl_dist = (pnl_pct + stop_loss) / stop_loss * 100
                    bot_log(f"◷ HOLD {symbol} @ ${price:.4f} | P&L:{pnl_pct*100:.2f}% | RSI:{rsi} | SL:{sl_dist:.0f}%", "info")

        except Exception as e:
            bot_log(f"✗ Error ciclo {symbol}: {e}", "error")

    with state_lock:
        bot_state["stats"]["cycles"] += 1


def bot_loop():
    bot_log("▶ Bot iniciado. Operando 24/7.", "info")
    while True:
        with state_lock:
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
    acquired = state_lock.acquire(timeout=5)
    if not acquired:
        return jsonify({"ok": False, "msg": "Bot ocupado, intenta de nuevo"}), 503
    try:
        bot_state["positions"][symbol] = {
            "buy_price":    buy_price,
            "qty":          qty,
            "cost":         round(buy_price * qty, 2),
            "buy_time":     time.time(),
            "order_id":     "manual",
            "partial_sold": False,
            "is_volatile":  False,
        }
    finally:
        state_lock.release()
    save_state()
    return jsonify({"ok": True, "msg": f"Posición {symbol} registrada @ ${buy_price}"})


@app.route("/bot/clear_position", methods=["POST"])
def clear_position():
    """Elimina manualmente una posición del estado del bot."""
    data = request.get_json(silent=True) or {}
    symbol = data.get("symbol")
    if not symbol:
        return jsonify({"ok": False, "msg": "Falta symbol"}), 400
    acquired = state_lock.acquire(timeout=5)
    if not acquired:
        return jsonify({"ok": False, "msg": "Bot ocupado, intenta de nuevo"}), 503
    try:
        if symbol in bot_state["positions"]:
            del bot_state["positions"][symbol]
    finally:
        state_lock.release()
    save_state()
    return jsonify({"ok": True, "msg": f"Posición {symbol} eliminada"})

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

    with state_lock:
        if bot_state["running"]:
            return jsonify({"ok": False, "msg": "Bot ya está corriendo"}), 400
        if "pairs"          in data: bot_state["pairs"]          = data["pairs"]
        if "profit_target"  in data: bot_state["profit_target"]  = float(data["profit_target"])
        if "drop_to_buy"    in data: bot_state["drop_to_buy"]    = float(data["drop_to_buy"])
        if "trade_amount"   in data: bot_state["trade_amount"]   = float(data["trade_amount"])
        if "interval"       in data: bot_state["interval"]       = int(data["interval"])
        if "stop_loss"      in data: bot_state["stop_loss"]      = float(data["stop_loss"])
        if "max_positions"  in data: bot_state["max_positions"]  = int(data["max_positions"])
        if "reinvest_pct"   in data: bot_state["reinvest_pct"]   = int(data["reinvest_pct"])
        bot_state["running"] = True

    # Asegurarse que el hilo anterior terminó
    if bot_thread and bot_thread.is_alive():
        bot_state["running"] = False
        bot_thread.join(timeout=5)
        bot_state["running"] = True

    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    return jsonify({"ok": True, "msg": "Bot iniciado"})


@app.route("/bot/stop", methods=["POST"])
def bot_stop():
    if not bot_state["running"]:
        return jsonify({"ok": False, "msg": "Bot no está corriendo"}), 400
    bot_state["running"] = False
    return jsonify({"ok": True, "msg": "Bot deteniendo..."})


@app.route("/bot/status")
def bot_status():
    # Lectura directa sin lock para evitar deadlock con bot_cycle
    return jsonify({
        "running":            bot_state["running"],
        "pairs":              bot_state.get("pairs", []),
        "volatile_pairs":     bot_state.get("volatile_pairs", []),
        "profit_target":      bot_state.get("profit_target", 0.8),
        "volatile_profit":    bot_state.get("volatile_profit", 1.5),
        "drop_to_buy":        bot_state.get("drop_to_buy", 0.15),
        "trade_amount":       bot_state.get("trade_amount", 50),
        "volatile_amount":    bot_state.get("volatile_amount", 20),
        "interval":           bot_state.get("interval", 20),
        "positions":          dict(bot_state.get("positions", {})),
        "stats":              dict(bot_state.get("stats", {})),
        "last_prices":        dict(bot_state.get("last_prices", {})),
        "last_changes":       dict(bot_state.get("last_changes", {})),
        "usdt_available":     bot_state.get("usdt_available", 0),
        "max_positions":      bot_state.get("max_positions", 8),
        "stop_loss":          bot_state.get("stop_loss", 3.0),
        "volatile_stop_loss": bot_state.get("volatile_stop_loss", 2.0),
        "blacklist":          {k: round(v - time.time()) for k, v in bot_state.get("blacklist", {}).items() if v > time.time()},
        "total_reinvested":   bot_state.get("total_reinvested", 0),
        "dca_enabled":        bot_state.get("dca_enabled", True),
        "reinvest_enabled":   bot_state.get("reinvest_enabled", True),
        "log":                bot_state.get("log", [])[-50:],
    })


@app.route("/bot/config", methods=["POST"])
def bot_config():
    data = request.get_json(silent=True) or {}
    with state_lock:
        if "pairs"          in data: bot_state["pairs"]          = data["pairs"]
        if "profit_target"  in data: bot_state["profit_target"]  = float(data["profit_target"])
        if "drop_to_buy"    in data: bot_state["drop_to_buy"]    = float(data["drop_to_buy"])
        if "trade_amount"   in data: bot_state["trade_amount"]   = float(data["trade_amount"])
        if "interval"       in data: bot_state["interval"]       = int(data["interval"])
        if "max_positions"  in data: bot_state["max_positions"]  = int(data["max_positions"])
        if "stop_loss"      in data: bot_state["stop_loss"]      = float(data["stop_loss"])
    return jsonify({"ok": True, "config": {
        "pairs":          bot_state["pairs"],
        "profit_target":  bot_state["profit_target"],
        "drop_to_buy":    bot_state["drop_to_buy"],
        "trade_amount":   bot_state["trade_amount"],
        "interval":       bot_state["interval"],
        "max_positions":  bot_state["max_positions"],
        "stop_loss":      bot_state["stop_loss"],
    }})


# ─── Auto-arranque al cargar el módulo (compatible con gunicorn) ──────────────
def _auto_start():
    global bot_thread
    init_db()
    load_state()
    with state_lock:
        if bot_state["running"]:
            return  # ya hay una instancia corriendo
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
