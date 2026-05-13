from flask import Flask, jsonify, request
from flask_cors import CORS
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
import os, time, threading, logging, json, requests as req_lib
import pg8000.native as pg8000

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

API_KEY      = os.environ.get("API_KEY")
SECRET_KEY        = os.environ.get("SECRET_KEY")
DATABASE_URL      = os.environ.get("DATABASE_URL")
TAAPI_SECRET      = os.environ.get("TAAPI_SECRET", "")
SANTIMENT_KEY     = os.environ.get("SANTIMENT_KEY", "")
CRYPTOQUANT_KEY   = os.environ.get("CRYPTOQUANT_KEY", "")
OPENAI_KEY        = os.environ.get("OPENAI_KEY", "")

# ─── Cache de señales externas ────────────────────────────────────────────────
taapi_cache       = {}
santiment_cache   = {}
cryptoquant_cache = {}
openai_cache      = {}
TAAPI_TTL         = 30    # segundos
SANTIMENT_TTL     = 300   # 5 minutos
CRYPTOQUANT_TTL   = 300   # 5 minutos
OPENAI_TTL        = 1800   # 30 minutos

# ─── Santiment — Sentimiento social ──────────────────────────────────────────
def get_santiment_sentiment(symbol):
    """
    Obtiene sentimiento social de Santiment para una moneda.
    Retorna score entre -1 (muy negativo) y 1 (muy positivo).
    """
    # Mapeo de símbolos Binance a slugs de Santiment
    slug_map = {
        "BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "BNBUSDT": "binance-coin",
        "SOLUSDT": "solana", "XRPUSDT": "xrp", "ADAUSDT": "cardano",
        "DOGEUSDT": "dogecoin", "DOTUSDT": "polkadot", "LINKUSDT": "chainlink",
        "LTCUSDT": "litecoin", "AVAXUSDT": "avalanche", "MATICUSDT": "matic-network",
        "SHIBUSDT": "shiba-inu", "PEPEUSDT": "pepe", "FLOKIUSDT": "floki",
    }
    slug = slug_map.get(symbol)
    if not slug or not SANTIMENT_KEY:
        return None

    now = time.time()
    if symbol in santiment_cache:
        data, ts = santiment_cache[symbol]
        if now - ts < SANTIMENT_TTL:
            return data

    try:
        # Query GraphQL de Santiment para sentiment_balance
        query = """
        {
          getMetric(metric: "sentiment_balance_total") {
            timeseriesData(
              slug: "%s"
              from: "%s"
              to: "%s"
              interval: "1h"
            ) {
              datetime
              value
            }
          }
        }
        """ % (slug,
               time.strftime("%Y-%m-%dT%H:00:00Z", time.gmtime(now - 7200)),
               time.strftime("%Y-%m-%dT%H:00:00Z", time.gmtime(now)))

        resp = req_lib.post(
            "https://api.santiment.net/graphql",
            json={"query": query},
            headers={"Authorization": f"Apikey {SANTIMENT_KEY}"},
            timeout=8
        )
        if resp.status_code != 200:
            return None

        data_points = resp.json().get("data", {}).get("getMetric", {}).get("timeseriesData", [])
        if not data_points:
            return None

        # Promedio de las últimas 2 horas
        values = [p["value"] for p in data_points if p["value"] is not None]
        if not values:
            return None
        avg_sentiment = sum(values) / len(values)

        # Normalizar a -1 a 1
        score = max(-1, min(1, avg_sentiment / 10))

        result = {
            "slug":      slug,
            "score":     round(score, 3),
            "raw":       round(avg_sentiment, 3),
            "positive":  score > 0.1,
            "negative":  score < -0.1,
            "neutral":   -0.1 <= score <= 0.1,
        }
        santiment_cache[symbol] = (result, now)
        return result

    except Exception as e:
        logger.error(f"Error Santiment {symbol}: {e}")
        return None


# ─── CryptoQuant — Flujos on-chain y presión de ballenas ─────────────────────
def get_cryptoquant_signal(symbol):
    """
    Obtiene señal de flujos on-chain de CryptoQuant.
    Retorna señal de compra/venta basada en flujos a exchanges.
    """
    # Solo disponible para BTC y ETH en plan gratuito
    asset_map = {"BTCUSDT": "btc", "ETHUSDT": "eth"}
    asset = asset_map.get(symbol)
    if not asset or not CRYPTOQUANT_KEY:
        return None

    now = time.time()
    if symbol in cryptoquant_cache:
        data, ts = cryptoquant_cache[symbol]
        if now - ts < CRYPTOQUANT_TTL:
            return data

    try:
        # Flujo neto hacia exchanges — si es positivo = presión de venta
        resp = req_lib.get(
            f"https://api.cryptoquant.com/v1/{asset}/exchange-flows/netflow",
            headers={"Authorization": f"Bearer {CRYPTOQUANT_KEY}"},
            params={"window": "hour", "limit": 6},
            timeout=8
        )
        if resp.status_code != 200:
            return None

        data_points = resp.json().get("result", {}).get("data", [])
        if not data_points:
            return None

        # Promedio de últimas 6 horas
        netflows = [p.get("netflow_total", 0) for p in data_points]
        avg_netflow = sum(netflows) / len(netflows) if netflows else 0

        # Netflow positivo = dinero entrando a exchanges = presión de venta
        # Netflow negativo = dinero saliendo de exchanges = acumulación (alcista)
        bullish = avg_netflow < 0
        bearish = avg_netflow > 0

        result = {
            "asset":      asset,
            "avg_netflow": round(avg_netflow, 2),
            "bullish":    bullish,
            "bearish":    bearish,
            "signal":     "comprar" if bullish else "vender" if bearish else "neutral",
        }
        cryptoquant_cache[symbol] = (result, now)
        return result

    except Exception as e:
        logger.error(f"Error CryptoQuant {symbol}: {e}")
        return None


# ─── OpenAI — Análisis de noticias con GPT ───────────────────────────────────
_openai_market_signal = {"signal": "neutral", "reason": "Sin datos", "ts": 0}

def update_openai_signal():
    """
    Analiza el sentimiento general del mercado cripto con GPT.
    Se ejecuta cada 2 horas solo en horario activo (8am-10pm UTC).
    """
    global _openai_market_signal
    if not OPENAI_KEY:
        return

    # Solo en horario activo
    hour = time.gmtime().tm_hour
    if not (8 <= hour <= 22):
        return

    now = time.time()
    if now - _openai_market_signal.get("ts", 0) < OPENAI_TTL:
        return

    try:
        resp = req_lib.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 100,
                "messages": [
                    {
                        "role": "system",
                        "content": "Eres un analista de mercados cripto. Responde SOLO con JSON: {\"signal\": \"buy\"|\"sell\"|\"neutral\", \"reason\": \"explicación en 10 palabras\", \"confidence\": 0-100}"
                    },
                    {
                        "role": "user",
                        "content": f"Analiza el sentimiento actual del mercado cripto en {time.strftime('%Y-%m-%d %H:%M UTC')}. ¿Es buen momento para comprar?"
                    }
                ]
            },
            timeout=10
        )
        if resp.status_code != 200:
            return

        content = resp.json()["choices"][0]["message"]["content"]
        # Limpiar posibles backticks
        content = content.replace("```json", "").replace("```", "").strip()
        parsed  = json.loads(content)
        parsed["ts"] = now
        _openai_market_signal = parsed
        logger.info(f"🤖 OpenAI signal: {parsed['signal']} — {parsed.get('reason','')}")

    except Exception as e:
        logger.error(f"Error OpenAI: {e}")

def get_openai_signal():
    """Retorna la última señal de OpenAI."""
    update_openai_signal()
    return _openai_market_signal


# ─── Fear & Greed Index ───────────────────────────────────────────────────────
_fear_greed_cache = {"value": 50, "label": "Neutral", "ts": 0}
FEAR_GREED_TTL    = 3600  # actualizar cada hora

def get_fear_greed():
    """
    Obtiene el índice Fear & Greed de Alternative.me.
    0-24: Extreme Fear (mejor momento para comprar)
    25-49: Fear
    50-74: Greed
    75-100: Extreme Greed (mejor momento para vender)
    """
    global _fear_greed_cache
    now = time.time()
    if now - _fear_greed_cache.get("ts", 0) < FEAR_GREED_TTL:
        return _fear_greed_cache

    try:
        resp = req_lib.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=5
        )
        if resp.status_code != 200:
            return _fear_greed_cache

        data  = resp.json().get("data", [{}])[0]
        value = int(data.get("value", 50))
        label = data.get("value_classification", "Neutral")

        _fear_greed_cache = {
            "value":        value,
            "label":        label,
            "ts":           now,
            "extreme_fear": value < 25,
            "fear":         25 <= value < 45,
            "neutral":      45 <= value < 55,
            "greed":        55 <= value < 75,
            "extreme_greed": value >= 75,
        }
        logger.info(f"😱 Fear & Greed: {value} ({label})")
        return _fear_greed_cache

    except Exception as e:
        logger.error(f"Error Fear & Greed: {e}")
        return _fear_greed_cache


# ─── CoinGlass — Liquidaciones y Open Interest ───────────────────────────────
_coinglass_cache = {}
COINGLASS_TTL    = 300  # 5 minutos

def get_coinglass_signal(symbol):
    """
    Obtiene datos de liquidaciones y open interest de CoinGlass.
    Liquidaciones altas = volatilidad = oportunidad de rebote.
    """
    asset_map = {
        "BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL",
        "BNBUSDT": "BNB", "XRPUSDT": "XRP", "ADAUSDT": "ADA",
        "DOGEUSDT": "DOGE", "DOTUSDT": "DOT", "AVAXUSDT": "AVAX",
        "LINKUSDT": "LINK", "LTCUSDT": "LTC", "MATICUSDT": "MATIC",
    }
    asset = asset_map.get(symbol)
    if not asset:
        return None

    now = time.time()
    if symbol in _coinglass_cache:
        data, ts = _coinglass_cache[symbol]
        if now - ts < COINGLASS_TTL:
            return data

    try:
        # Liquidaciones en las últimas 24h
        resp = req_lib.get(
            f"https://open-api.coinglass.com/public/v2/liquidation_chart",
            params={"symbol": asset, "interval": "1h", "limit": 4},
            headers={"coinglassSecret": ""},
            timeout=6
        )
        if resp.status_code != 200:
            return None

        data_raw = resp.json().get("data", [])
        if not data_raw:
            return None

        # Calcular liquidaciones totales recientes
        long_liq  = sum(d.get("longLiquidationUsd", 0) for d in data_raw)
        short_liq = sum(d.get("shortLiquidationUsd", 0) for d in data_raw)
        total_liq = long_liq + short_liq

        # Si hay muchas liquidaciones de longs = presión bajista
        # Si hay muchas liquidaciones de shorts = rebote alcista
        long_dominant  = long_liq > short_liq * 1.5
        short_dominant = short_liq > long_liq * 1.5

        result = {
            "asset":          asset,
            "long_liq":       round(long_liq, 0),
            "short_liq":      round(short_liq, 0),
            "total_liq":      round(total_liq, 0),
            "long_dominant":  long_dominant,
            "short_dominant": short_dominant,
            "bullish":        short_dominant,  # shorts liquidados = rebote
            "bearish":        long_dominant,   # longs liquidados = caída
        }
        _coinglass_cache[symbol] = (result, now)
        return result

    except Exception as e:
        logger.error(f"Error CoinGlass {symbol}: {e}")
        return None

def get_taapi_signal(symbol):
    """
    Obtiene señales ML de Taapi para un símbolo.
    Retorna dict con rsi, macd_hist, bb_percent, señal general.
    """
    now = time.time()
    if symbol in taapi_cache:
        data, ts = taapi_cache[symbol]
        if now - ts < TAAPI_TTL:
            return data

    # Convertir símbolo Binance a formato Taapi (BTCUSDT → BTC/USDT)
    base = symbol.replace("USDT", "")
    taapi_sym = f"{base}/USDT"

    try:
        # Llamada bulk — RSI + MACD + Bollinger en una sola petición
        payload = {
            "secret": TAAPI_SECRET,
            "construct": {
                "exchange": "binance",
                "symbol": taapi_sym,
                "interval": "5m",
                "indicators": [
                    {"id": "rsi",  "indicator": "rsi"},
                    {"id": "macd", "indicator": "macd"},
                    {"id": "bb",   "indicator": "bbands"},
                    {"id": "ema20","indicator": "ema", "period": 20},
                ]
            }
        }
        resp = req_lib.post(
            "https://api.taapi.io/bulk",
            json=payload,
            timeout=8
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("data", [])
        parsed  = {r["id"]: r["result"] for r in results if "result" in r}

        rsi       = parsed.get("rsi", {}).get("value", 50)
        macd_hist = parsed.get("macd", {}).get("valueHist", 0)
        bb        = parsed.get("bb", {})
        bb_upper  = bb.get("valueUpperBand", 0)
        bb_lower  = bb.get("valueLowerBand", 0)
        bb_mid    = bb.get("valueMiddleBand", 0)
        ema20     = parsed.get("ema20", {}).get("value", 0)

        # Calcular bb_percent (qué tan cerca está del lower band, 0=lower, 1=upper)
        bb_range   = bb_upper - bb_lower if bb_upper > bb_lower else 1
        last_price = bb_mid  # aproximación
        bb_pct     = (last_price - bb_lower) / bb_range if bb_range > 0 else 0.5

        # Señal de compra: RSI bajo + MACD histograma subiendo + precio cerca del lower band
        buy_score = 0
        if rsi < 45:        buy_score += 1
        if macd_hist > 0:   buy_score += 1  # histograma positivo = momentum alcista
        if bb_pct < 0.35:   buy_score += 1  # precio cerca del lower band

        signal = {
            "rsi":       round(rsi, 1),
            "macd_hist": round(macd_hist, 6),
            "bb_pct":    round(bb_pct, 3),
            "ema20":     round(ema20, 4),
            "buy_score": buy_score,  # 0-3, necesitamos ≥2 para comprar
            "ok":        True,
        }
        taapi_cache[symbol] = (signal, now)
        return signal

    except Exception as e:
        logger.error(f"Error Taapi {symbol}: {e}")
        return None

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
    """Guarda posiciones, stats y configuración en PostgreSQL."""
    try:
        conn = get_db()
        data = json.dumps({
            "positions": bot_state["positions"],
            "stats":     bot_state["stats"],
            "config": {
                "profit_target":  bot_state["profit_target"],
                "drop_to_buy":    bot_state["drop_to_buy"],
                "trade_amount":   bot_state["trade_amount"],
                "interval":       bot_state["interval"],
                "stop_loss":      bot_state["stop_loss"],
                "max_positions":  bot_state["max_positions"],
                "dca_enabled":    bot_state["dca_enabled"],
                "volatile_amount": bot_state["volatile_amount"],
                "reinvest_pct":   bot_state.get("reinvest_pct", 80),
            }
        })
        conn.run("""
            INSERT INTO bot_state (key, value) VALUES ('state', :data)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, data=data)
        conn.close()
    except Exception as e:
        logger.error(f"Error guardando estado en DB: {e}")

def load_state():
    """Carga posiciones, stats y configuración desde PostgreSQL al arrancar."""
    try:
        conn = get_db()
        rows = conn.run("SELECT value FROM bot_state WHERE key = 'state'")
        conn.close()
        if rows:
            data = json.loads(rows[0][0])
            bot_state["positions"] = data.get("positions", {})
            bot_state["stats"]     = data.get("stats", bot_state["stats"])
            # Restaurar configuración
            cfg = data.get("config", {})
            if cfg:
                bot_state["profit_target"]  = cfg.get("profit_target",  bot_state["profit_target"])
                bot_state["drop_to_buy"]    = cfg.get("drop_to_buy",    bot_state["drop_to_buy"])
                bot_state["trade_amount"]   = cfg.get("trade_amount",   bot_state["trade_amount"])
                bot_state["interval"]       = cfg.get("interval",       bot_state["interval"])
                bot_state["stop_loss"]      = cfg.get("stop_loss",      bot_state["stop_loss"])
                bot_state["max_positions"]  = cfg.get("max_positions",  bot_state["max_positions"])
                bot_state["dca_enabled"]    = cfg.get("dca_enabled",    bot_state["dca_enabled"])
                bot_state["volatile_amount"] = cfg.get("volatile_amount", bot_state["volatile_amount"])
                bot_state["reinvest_pct"]   = cfg.get("reinvest_pct",   80)
            logger.info(f"✓ Estado restaurado: {len(bot_state['positions'])} posiciones | DCA:{bot_state['dca_enabled']} | trade:${bot_state['trade_amount']}")
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
    "reinvest_pct": 80,
    "total_reinvested": 0.0,
    # ── Trailing Stop ─────────────────────────────────────────────────────────
    "trailing_stop_enabled": True,
    "trailing_stop_pct":     0.5,   # si el precio baja 0.5% desde el máximo, vender
    "price_highs":           {},    # precio máximo alcanzado por posición
    # ── Profit target dinámico ────────────────────────────────────────────────
    "dynamic_profit_enabled": True,
    # ── Tamaño de posición dinámico según score ML ────────────────────────────
    "dynamic_size_enabled":  True,
    # ── Intervalo óptimo ─────────────────────────────────────────────────────
    "interval":              45,    # 45 segundos
    # ── Reentrada inteligente ─────────────────────────────────────────────────
    "reentry_enabled":       True,
    "reentry_drop":          0.8,   # compra si cae 0.8% después de una venta exitosa
    "recent_sells":          {},    # {symbol: {"price": x, "time": t, "won": bool}}
    "max_investment": {
        "BTCUSDT":   100,
        "ETHUSDT":   100,
        "BNBUSDT":    60,
        "SOLUSDT":    60,
        "XRPUSDT":    60,
        "ADAUSDT":    40,
        "MATICUSDT":  40,
        "DOTUSDT":    40,
        "AVAXUSDT":   40,
        "LINKUSDT":   40,
        "LTCUSDT":    40,
        "DOGEUSDT":   40,
        "SHIBUSDT":   20,
        "PEPEUSDT":   20,
        "WIFUSDT":    20,
        "BONKUSDT":   20,
        "FLOKIUSDT":  20,
    },
}

bot_thread    = None
bot_lock      = threading.Lock()
state_lock    = threading.Lock()
_bot_started  = False  # flag para evitar múltiples instancias


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


def check_max_investment(symbol, additional_cost):
    """Verifica si agregar más inversión supera el techo permitido."""
    max_inv = bot_state.get("max_investment", {})
    ceiling = max_inv.get(symbol, 50)  # por defecto $50
    pos = bot_state.get("positions", {}).get(symbol)
    current_cost = pos.get("cost", 0) if pos else 0
    if current_cost + additional_cost > ceiling:
        bot_log(f"⛔ Techo de inversión alcanzado para {symbol}: ${current_cost:.2f}/${ceiling} — no se compra más", "info")
        return False
    return True


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


def get_market_context(client, all_pairs):
    """
    Analiza el contexto general del mercado antes de cualquier compra.
    Retorna un dict con señales macro del mercado.
    """
    try:
        # 1. Tendencia de BTC en las últimas horas
        btc_ticker = client.get_ticker(symbol="BTCUSDT")
        btc_change_24h = float(btc_ticker["priceChangePercent"])
        btc_price = float(btc_ticker["lastPrice"])

        # 2. Calcular cuántos pares están cayendo vs subiendo en 24h
        falling = 0
        rising  = 0
        for sym in all_pairs[:8]:
            try:
                t = client.get_ticker(symbol=sym)
                chg = float(t["priceChangePercent"])
                if chg < -1:   falling += 1
                elif chg > 1:  rising  += 1
                else:          rising  += 1  # neutral cuenta como no cayendo
            except:
                pass

        total = falling + rising if (falling + rising) > 0 else 1
        market_bearish_pct = (falling / total) * 100

        # 3. Historial de pérdidas recientes del bot
        stats = bot_state.get("stats", {})
        recent_losses = stats.get("consecutive_losses", 0)

        context = {
            "btc_change_24h":      round(btc_change_24h, 2),
            "btc_price":           btc_price,
            "market_bearish_pct":  round(market_bearish_pct, 0),
            "recent_losses":       recent_losses,
            "market_crash":        btc_change_24h < -5,      # BTC cayó >5% hoy
            "market_weak":         btc_change_24h < -2,      # BTC cayó >2% hoy
            "market_strong":       btc_change_24h > 2,       # BTC subió >2%
            "broad_selloff":       market_bearish_pct > 70,  # >70% pares cayendo
            "on_losing_streak":    recent_losses >= 3,       # 3+ pérdidas seguidas
        }
        return context
    except Exception as e:
        logger.error(f"Error market context: {e}")
        return {}


def update_consecutive_losses(won):
    """Actualiza el contador de pérdidas/ganancias consecutivas."""
    with state_lock:
        if won:
            bot_state["stats"]["consecutive_losses"] = 0
        else:
            current = bot_state["stats"].get("consecutive_losses", 0)
            bot_state["stats"]["consecutive_losses"] = current + 1


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
                update_consecutive_losses(True)
            else:
                bot_state["stats"]["losses"] += 1
                update_consecutive_losses(False)
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

    # Actualizar señal OpenAI cada 30 minutos en background
    threading.Thread(target=update_openai_signal, daemon=True).start()

    # Actualizar Fear & Greed cada hora en background
    threading.Thread(target=get_fear_greed, daemon=True).start()

    # Contexto de mercado deshabilitado temporalmente para evitar bloqueos
    market_ctx = {
        "btc_change_24h": 0,
        "market_crash": False,
        "market_weak": False,
        "market_strong": False,
        "broad_selloff": False,
        "on_losing_streak": False,
        "market_bearish_pct": 0,
    }

    # Veto global — si el mercado está en crash, no comprar nada
    if market_ctx.get("market_crash"):
        bot_log(f"🔴 VETO GLOBAL — BTC cayó {market_ctx['btc_change_24h']}% hoy. Sin nuevas compras.", "error")
        # Solo gestionar posiciones existentes, no comprar
    if market_ctx.get("broad_selloff"):
        bot_log(f"🔴 SELLOFF GENERALIZADO — {market_ctx['market_bearish_pct']}% pares cayendo. Modo defensivo.", "error")

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
            # SEÑAL DE COMPRA — SISTEMA DE PUNTUACIÓN ML (0-100 pts)
            # ════════════════════════════════════════════════════════════════
            if (position is None and
                prev_price and
                confirmed_drop and
                n_positions < max_positions and
                usdt_free >= t_amount * 0.4 and
                not daily_goal_reached() and
                check_max_investment(symbol, t_amount * 0.4)):

                score   = 0
                reasons = []
                vetoes  = []

                # ── VETO GLOBAL — contexto de mercado ────────────────────────
                if market_ctx.get("market_crash"):
                    vetoes.append(f"BTC crash:{market_ctx.get('btc_change_24h')}%")
                if market_ctx.get("broad_selloff"):
                    vetoes.append(f"Selloff:{market_ctx.get('market_bearish_pct')}% pares bajando")
                if market_ctx.get("on_losing_streak"):
                    vetoes.append(f"Racha perdedora:{market_ctx.get('recent_losses')} pérdidas seguidas")

                # ── VETO — caída con volumen alto (trampa bajista) ────────────
                if vol24h > 0:
                    avg_vol = bot_state.get("avg_volumes", {}).get(symbol, vol24h)
                    if real_change < -1.5 and vol24h > avg_vol * 2:
                        vetoes.append(f"Caída con volumen 2x — trampa bajista")
                # Guardar volumen promedio
                avg_vols = bot_state.get("avg_volumes", {})
                avg_vols[symbol] = (avg_vols.get(symbol, vol24h) * 0.9 + vol24h * 0.1)
                bot_state["avg_volumes"] = avg_vols

                # ── 1. RSI propio (0-20 pts) ──────────────────────────────────
                if rsi < 25:
                    score += 20; reasons.append(f"RSI:{rsi}(sobreventa extrema)")
                elif rsi < 35:
                    score += 15; reasons.append(f"RSI:{rsi}(sobreventa)")
                elif rsi < 45:
                    score += 10; reasons.append(f"RSI:{rsi}(bajo)")
                elif rsi < 50:
                    score += 5;  reasons.append(f"RSI:{rsi}(neutro-bajo)")
                else:
                    score += 0;  reasons.append(f"RSI:{rsi}(alto)")
                    if rsi > 60:
                        vetoes.append(f"RSI alto:{rsi} — no es buen momento")

                # ── 2. Fear & Greed (0-25 pts) ────────────────────────────────
                fg = get_fear_greed()
                fg_val = fg.get("value", 50)
                if fg.get("extreme_greed"):
                    vetoes.append(f"Fear&Greed euforia extrema:{fg_val}")
                elif fg.get("extreme_fear"):
                    score += 25; reasons.append(f"F&G:{fg_val}(miedo extremo=oportunidad)")
                elif fg.get("fear"):
                    score += 15; reasons.append(f"F&G:{fg_val}(miedo)")
                elif fg.get("neutral"):
                    score += 8;  reasons.append(f"F&G:{fg_val}(neutro)")
                elif fg.get("greed"):
                    score += 3;  reasons.append(f"F&G:{fg_val}(codicia)")

                # ── 3. Taapi ML (0-25 pts) ────────────────────────────────────
                taapi_signal = get_taapi_signal(symbol)
                if taapi_signal and taapi_signal.get("ok"):
                    ts  = taapi_signal.get("buy_score", 0)
                    pts = [0, 8, 17, 25][min(ts, 3)]
                    score += pts; reasons.append(f"Taapi:{ts}/3")
                    if taapi_signal.get("rsi", 50) > 70:
                        vetoes.append(f"Taapi RSI sobrecomprado:{taapi_signal['rsi']}")
                else:
                    score += 8  # neutral

                # ── 4. Santiment (0-10 pts) ───────────────────────────────────
                sentiment = get_santiment_sentiment(symbol)
                if sentiment:
                    if sentiment["negative"]:
                        vetoes.append(f"Santiment negativo:{sentiment['score']}")
                    elif sentiment["positive"]:
                        score += 10; reasons.append(f"Sentimiento:positivo")
                    else:
                        score += 5;  reasons.append(f"Sentimiento:neutro")
                else:
                    score += 5

                # ── 5. CryptoQuant (0-10 pts) ─────────────────────────────────
                cq = get_cryptoquant_signal(symbol)
                if cq:
                    if cq["bearish"]:
                        vetoes.append(f"CryptoQuant bearish")
                    elif cq["bullish"]:
                        score += 10; reasons.append(f"CryptoQuant:bullish")
                    else:
                        score += 5;  reasons.append(f"CryptoQuant:neutro")
                else:
                    score += 5

                # ── 6. CoinGlass liquidaciones (-5 a +5 pts) ─────────────────
                cg = get_coinglass_signal(symbol)
                if cg:
                    if cg["bearish"]:
                        score -= 5; reasons.append(f"CoinGlass:longs liquidados")
                    elif cg["bullish"]:
                        score += 5; reasons.append(f"CoinGlass:rebote")

                # ── 7. OpenAI (0-5 pts) ───────────────────────────────────────
                ai = get_openai_signal()
                if ai.get("signal") == "sell" and ai.get("confidence", 0) > 80:
                    vetoes.append(f"OpenAI sell:{ai.get('confidence')}%")
                elif ai.get("signal") == "buy":
                    score += 5; reasons.append(f"OpenAI:buy({ai.get('confidence',0)}%)")
                else:
                    score += 2

                # ── 8. Bonus por contexto positivo ───────────────────────────
                if market_ctx.get("market_strong"):
                    score += 5; reasons.append("BTC tendencia alcista")
                if market_ctx.get("btc_change_24h", 0) > 0 and not is_volatile:
                    score += 3; reasons.append("BTC en verde")

                # ── Umbral dinámico según condiciones ────────────────────────
                min_score = 50  # base
                if is_volatile:           min_score += 10  # volátiles requieren más
                if market_ctx.get("market_weak"): min_score += 10  # mercado débil = más exigente
                if market_ctx.get("on_losing_streak"): min_score += 15  # racha mala = más exigente

                # ── Evaluación final ──────────────────────────────────────────
                if vetoes:
                    bot_log(f"🚫 VETO {symbol} | score:{score} | {' | '.join(vetoes)}", "info")
                    continue

                if score < min_score:
                    bot_log(f"⏸ Sin señal {symbol} | score:{score}/{min_score} | {' · '.join(reasons[:3])}", "info")
                    continue

                bot_log(f"✅ COMPRA APROBADA {symbol} | score:{score}/{min_score} | {' · '.join(reasons)}", "buy")

                # ── Tamaño dinámico según score ML ───────────────────────────
                if bot_state.get("dynamic_size_enabled", True):
                    if score >= 80:     size_mult = 1.5   # señal muy fuerte
                    elif score >= 65:   size_mult = 1.2   # señal fuerte
                    elif score >= 55:   size_mult = 1.0   # señal normal
                    else:               size_mult = 0.7   # señal débil = menos capital
                else:
                    size_mult = 1.0

                # ── Horario óptimo de trading (13-21 UTC) ────────────────────
                hour = time.gmtime().tm_hour
                if 13 <= hour <= 21:
                    size_mult = min(size_mult * 1.1, 2.0)  # +10% en horario pico
                    drop_effective = drop_to_buy * 0.7     # umbral más bajo en horario pico
                else:
                    drop_effective = drop_to_buy

                # ── Reentrada inteligente ─────────────────────────────────────
                recent_sell = bot_state.get("recent_sells", {}).get(symbol)
                reentry_mult = 1.0
                if recent_sell and recent_sell.get("won") and bot_state.get("reentry_enabled", True):
                    time_since = time.time() - recent_sell.get("time", 0)
                    if time_since < 3600:  # dentro de 1 hora
                        reentry_drop = (recent_sell["price"] - price) / recent_sell["price"]
                        if reentry_drop >= bot_state.get("reentry_drop", 0.8) / 100:
                            reentry_mult = 1.3  # más capital en reentrada exitosa
                            bot_log(f"🔄 REENTRADA {symbol} | caída desde venta: {reentry_drop*100:.2f}%", "buy")

                final_amount = t_amount * size_mult * reentry_mult
                first_amount = min(final_amount * 0.4, usdt_free * 0.9)  # máximo 90% del USDT libre
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

                    if drop_from_avg >= level["drop"] and usdt_free >= dca_amount and check_max_investment(symbol, dca_amount):
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
                                bot_state["positions"][symbol]["qty"]      = total_qty
                                bot_state["positions"][symbol]["cost"]     = total_cost
                                bot_state["positions"][symbol]["avg_price"] = new_avg
                                if "dca_entries" not in bot_state["positions"][symbol]:
                                    bot_state["positions"][symbol]["dca_entries"] = []
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
                buy_price    = position.get("avg_price", position["buy_price"])
                qty          = position["qty"]
                pnl_pct      = (price - buy_price) / buy_price
                pos_volatile = position.get("is_volatile", False)
                sl           = volatile_stop_loss if pos_volatile else stop_loss

                # ── Profit target dinámico según Fear & Greed ────────────────
                fg_val = get_fear_greed().get("value", 50)
                base_target = volatile_profit if pos_volatile else profit_target
                if bot_state.get("dynamic_profit_enabled", True):
                    if fg_val >= 75:   p_target = base_target * 3.0
                    elif fg_val >= 60: p_target = base_target * 2.0
                    elif fg_val >= 45: p_target = base_target
                    else:              p_target = base_target * 0.8
                else:
                    p_target = base_target

                # ── Trailing Stop ─────────────────────────────────────────────
                if bot_state.get("trailing_stop_enabled", True):
                    price_highs  = bot_state.get("price_highs", {})
                    current_high = price_highs.get(symbol, buy_price)
                    if price > current_high:
                        price_highs[symbol] = price
                        bot_state["price_highs"] = price_highs
                        current_high = price
                    trail_pct  = bot_state.get("trailing_stop_pct", 0.5) / 100
                    trail_drop = (current_high - price) / current_high if current_high > 0 else 0
                    if trail_drop >= trail_pct and pnl_pct > 0.003:
                        bot_log(f"📉 TRAILING STOP {symbol} | máx:${current_high:.4f} caída:{trail_drop*100:.2f}% P&L:{pnl_pct*100:.2f}%", "sell")
                        if execute_sell(client, symbol, position, price, reason="trailing-stop"):
                            with state_lock:
                                bot_state["stats"]["daily_pnl"] += (price - buy_price) * qty
                                bot_state["recent_sells"][symbol] = {"price": price, "time": time.time(), "won": True}
                                ph = bot_state.get("price_highs", {})
                                if symbol in ph: del ph[symbol]
                        continue

                # 🛑 STOP-LOSS
                if pnl_pct <= -sl:
                    bot_log(f"🛑 STOP-LOSS {symbol} @ ${price:.4f} | {pnl_pct*100:.2f}%", "error")
                    if execute_sell(client, symbol, position, price, reason="stop-loss"):
                        add_to_blacklist(symbol, blacklist_hours * 2)
                        with state_lock:
                            bot_state["stats"]["daily_pnl"] += (price - buy_price) * qty
                            bot_state["recent_sells"][symbol] = {"price": price, "time": time.time(), "won": False}
                            ph = bot_state.get("price_highs", {})
                            if symbol in ph: del ph[symbol]

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
                                update_consecutive_losses(True)
                            save_state()
                            bot_log(f"½ SELL 50% {symbol} @ ${fill_price:.4f} | +${earned:.2f} | F&G:{fg_val} | día:${bot_state['stats']['daily_pnl']:.2f}/${bot_state['stats']['daily_goal']:.2f}", "sell")
                    except Exception as e:
                        bot_log(f"✗ Error venta parcial {symbol}: {e}", "error")

                # ✅ VENTA TOTAL al llegar a profit_target*2
                elif pnl_pct >= p_target * 2 and position.get("partial_sold"):
                    if execute_sell(client, symbol, position, price, reason="profit"):
                        earned = (price - buy_price) * qty
                        with state_lock:
                            bot_state["stats"]["daily_pnl"] += earned
                            bot_state["recent_sells"][symbol] = {"price": price, "time": time.time(), "won": True}
                            ph = bot_state.get("price_highs", {})
                            if symbol in ph: del ph[symbol]
                        if daily_goal_reached():
                            bot_log(f"🎯 META DIARIA: ${bot_state['stats']['daily_pnl']:.2f}", "buy")

                else:
                    goal    = bot_state["stats"]["daily_goal"]
                    daily   = bot_state["stats"]["daily_pnl"]
                    sl_dist = (pnl_pct + sl) / sl * 100
                    tag     = "🔥" if pos_volatile else "◷"
                    ph      = bot_state.get("price_highs", {}).get(symbol, buy_price)
                    bot_log(f"{tag} HOLD {symbol} @ ${price:.4f} | P&L:{pnl_pct*100:.2f}% | RSI:{rsi} | SL:{sl_dist:.0f}% | máx:${ph:.4f}", "info")

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
    import os
    my_pid = os.getpid()
    while True:
        with state_lock:
            running  = bot_state["running"]
            interval = bot_state["interval"]
        if not running:
            break
        # Verificar que somos el worker activo via DB
        try:
            now = int(time.time())
            conn = get_db()
            conn.run("""
                CREATE TABLE IF NOT EXISTS active_worker (
                    id TEXT PRIMARY KEY, pid INT, ts BIGINT
                )
            """)
            # Solo ejecutar si somos el worker registrado o si el último registro
            # tiene más de 30 segundos (el otro worker murió)
            rows = conn.run("SELECT pid, ts FROM active_worker WHERE id='bot'")
            if rows:
                db_pid, db_ts = rows[0][0], rows[0][1]
                if db_pid != my_pid and (now - db_ts) < 30:
                    conn.close()
                    time.sleep(interval)
                    continue
            # Registrar este worker como activo
            conn.run("""
                INSERT INTO active_worker (id, pid, ts) VALUES ('bot', :pid, :ts)
                ON CONFLICT (id) DO UPDATE SET pid=:pid, ts=:ts
            """, pid=my_pid, ts=now)
            conn.close()
        except Exception as e:
            logger.error(f"Error worker lock: {e}")
        try:
            bot_cycle()
        except Exception as e:
            bot_log(f"✗ Error general: {e}", "error")
        time.sleep(interval)
    bot_log("⏹ Bot detenido.", "info")


def _is_primary_worker():
    """Usa PostgreSQL para elegir un solo worker primario."""
    try:
        import os
        worker_id = str(os.getpid())
        conn = get_db()
        # Crear tabla si no existe
        conn.run("""
            CREATE TABLE IF NOT EXISTS bot_worker (
                id TEXT PRIMARY KEY,
                pid TEXT NOT NULL,
                ts BIGINT NOT NULL
            )
        """)
        now = int(time.time())
        # Intentar insertar o actualizar solo si han pasado más de 60 segundos
        # sin que otro worker actualice (significa que el otro murió)
        conn.run("""
            INSERT INTO bot_worker (id, pid, ts) VALUES ('primary', :pid, :ts)
            ON CONFLICT (id) DO UPDATE SET pid = :pid, ts = :ts
            WHERE bot_worker.ts < :old_ts
        """, pid=worker_id, ts=now, old_ts=now-60)
        # Verificar si somos el worker registrado
        rows = conn.run("SELECT pid FROM bot_worker WHERE id = 'primary'")
        conn.close()
        is_primary = rows[0][0] == worker_id if rows else False
        if is_primary:
            logger.info(f"Worker primario: PID {worker_id}")
        else:
            logger.info(f"Worker secundario PID {worker_id} — bot no arrancado")
        return is_primary
    except Exception as e:
        logger.error(f"Error worker check: {e}")
        return True


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

@app.route("/bot/warmup_apis")
def warmup_apis():
    """Pre-carga todas las APIs en background para mantener el cache actualizado."""
    def _warmup():
        pairs = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
        for sym in pairs:
            get_taapi_signal(sym)
            get_santiment_sentiment(sym)
            get_cryptoquant_signal(sym)
            get_coinglass_signal(sym)
        get_fear_greed()
        update_openai_signal()
        logger.info("✅ APIs pre-cargadas correctamente")
    threading.Thread(target=_warmup, daemon=True).start()
    return jsonify({"ok": True, "msg": "Warmup iniciado en background"})


@app.route("/bot/ml_signals")
def ml_signals():
    """Retorna todas las señales ML activas."""
    pairs = bot_state.get("pairs", []) + bot_state.get("volatile_pairs", [])
    signals = {}
    for sym in pairs:
        entry = {}
        if sym in taapi_cache:
            entry["taapi"] = taapi_cache[sym][0]
        if sym in santiment_cache:
            entry["santiment"] = santiment_cache[sym][0]
        if sym in cryptoquant_cache:
            entry["cryptoquant"] = cryptoquant_cache[sym][0]
        if entry:
            signals[sym] = entry
    signals["openai"]      = _openai_market_signal
    signals["fear_greed"]  = _fear_greed_cache
    return jsonify(signals)


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
        if "dca_enabled"    in data: bot_state["dca_enabled"]    = bool(data["dca_enabled"])
        if "volatile_amount" in data: bot_state["volatile_amount"] = float(data["volatile_amount"])
        if "reinvest_pct"   in data: bot_state["reinvest_pct"]   = int(data["reinvest_pct"])
    save_state()  # persistir config en PostgreSQL
    return jsonify({"ok": True, "config": {
        "pairs":          bot_state["pairs"],
        "profit_target":  bot_state["profit_target"],
        "drop_to_buy":    bot_state["drop_to_buy"],
        "trade_amount":   bot_state["trade_amount"],
        "interval":       bot_state["interval"],
        "max_positions":  bot_state["max_positions"],
        "stop_loss":      bot_state["stop_loss"],
        "dca_enabled":    bot_state["dca_enabled"],
    }})


# ═══════════════════════════════════════════════════════════════════════════════
# MÓDULO DE FUTUROS — Estrategia Long/Short con ML
# ═══════════════════════════════════════════════════════════════════════════════

futures_state = {
    "enabled":        False,
    "positions":      {},      # {symbol: {side, entry, qty, leverage, sl, tp}}
    "pairs":          ["ETHUSDT", "BTCUSDT"],
    "leverage":       2,
    "capital":        50.0,    # USDT por operación
    "stop_loss":      1.5,     # %
    "take_profit":    2.0,     # %
    "min_score":      60,      # score mínimo para operar
    "stats": {
        "trades": 0, "wins": 0, "losses": 0,
        "total_pnl": 0.0, "daily_pnl": 0.0,
    },
    "log": [],
}
futures_thread = None
futures_lock   = threading.Lock()


def futures_log(msg, level="info"):
    entry = {"time": time.strftime("%H:%M:%S"), "msg": msg, "level": level}
    with futures_lock:
        futures_state["log"].append(entry)
        if len(futures_state["log"]) > 100:
            futures_state["log"] = futures_state["log"][-100:]
    logger.info(f"[FUTURES] {msg}")


def get_futures_client():
    """Retorna cliente de Binance Futures."""
    from binance.client import Client
    return Client(API_KEY, SECRET_KEY)


def get_futures_score(symbol, direction="long"):
    """
    Calcula el score ML para abrir una posición de futuros.
    direction: 'long' o 'short'
    Retorna (score, reasons, vetoes)
    """
    score   = 0
    reasons = []
    vetoes  = []
    mult    = 1 if direction == "long" else -1

    # ── 1. RSI Taapi (±30 pts) ────────────────────────────────────────────────
    taapi = get_taapi_signal(symbol)
    if taapi and taapi.get("ok"):
        rsi = taapi.get("rsi", 50)
        if rsi < 25:
            pts = 30 * mult * -1  # RSI bajo = señal de long, no short
            score += 30 if direction == "long" else -30
            reasons.append(f"RSI:{rsi}(sobreventa extrema)")
        elif rsi < 35:
            score += 20 if direction == "long" else -20
            reasons.append(f"RSI:{rsi}(sobreventa)")
        elif rsi > 75:
            score += -20 if direction == "long" else 20
            reasons.append(f"RSI:{rsi}(sobrecompra extrema)")
        elif rsi > 65:
            score += -10 if direction == "long" else 10
            reasons.append(f"RSI:{rsi}(sobrecompra)")
        # MACD
        macd = taapi.get("macd_hist", 0)
        if macd > 0:
            score += 15 if direction == "long" else -15
            reasons.append("MACD:alcista")
        else:
            score += -15 if direction == "long" else 15
            reasons.append("MACD:bajista")
        # Bollinger
        bb_pct = taapi.get("bb_pct", 0.5)
        if bb_pct < 0.2:
            score += 15 if direction == "long" else -15
            reasons.append("BB:precio_bajo(soporte)")
        elif bb_pct > 0.8:
            score += -15 if direction == "long" else 15
            reasons.append("BB:precio_alto(resistencia)")

    # ── 2. Fear & Greed (±20 pts) ─────────────────────────────────────────────
    fg = get_fear_greed()
    fg_val = fg.get("value", 50)
    if fg.get("extreme_fear"):
        score += 20 if direction == "long" else -20
        reasons.append(f"F&G:{fg_val}(miedo_extremo)")
    elif fg.get("fear"):
        score += 10 if direction == "long" else -10
        reasons.append(f"F&G:{fg_val}(miedo)")
    elif fg.get("extreme_greed"):
        score += -20 if direction == "long" else 20
        reasons.append(f"F&G:{fg_val}(euforia_extrema)")
    elif fg.get("greed"):
        score += -10 if direction == "long" else 10
        reasons.append(f"F&G:{fg_val}(codicia)")

    # ── 3. Santiment (±10 pts) ────────────────────────────────────────────────
    sent = get_santiment_sentiment(symbol)
    if sent:
        if sent["positive"]:
            score += 10 if direction == "long" else -10
            reasons.append("Sentimiento:positivo")
        elif sent["negative"]:
            score += -10 if direction == "long" else 10
            reasons.append("Sentimiento:negativo")

    # ── 4. CryptoQuant (±15 pts, solo BTC/ETH) ───────────────────────────────
    cq = get_cryptoquant_signal(symbol)
    if cq:
        if cq["bullish"]:
            score += 15 if direction == "long" else -15
            reasons.append("CryptoQuant:bullish")
        elif cq["bearish"]:
            score += -15 if direction == "long" else 15
            reasons.append("CryptoQuant:bearish")

    # ── 5. OpenAI (±10 pts) ───────────────────────────────────────────────────
    ai = get_openai_signal()
    if ai.get("signal") == "buy":
        score += 10 if direction == "long" else -10
        reasons.append(f"OpenAI:buy({ai.get('confidence',0)}%)")
    elif ai.get("signal") == "sell":
        score += -10 if direction == "long" else 10
        reasons.append(f"OpenAI:sell({ai.get('confidence',0)}%)")

    # ── Vetoes ────────────────────────────────────────────────────────────────
    # No operar si ya hay posición abierta en el mismo símbolo
    if symbol in futures_state["positions"]:
        vetoes.append(f"Posición ya abierta en {symbol}")

    # No operar en ambas direcciones simultáneamente
    existing = futures_state["positions"].get(symbol, {})
    if existing.get("side") == ("BUY" if direction == "short" else "SELL"):
        vetoes.append("Dirección opuesta a posición existente")

    return score, reasons, vetoes


def futures_cycle():
    """Ciclo principal de trading de futuros."""
    try:
        client = get_futures_client()

        # Pre-cargar señales ML para los pares de futuros
        for sym in futures_state["pairs"]:
            threading.Thread(target=get_taapi_signal, args=(sym,), daemon=True).start()
            threading.Thread(target=get_santiment_sentiment, args=(sym,), daemon=True).start()
        threading.Thread(target=get_fear_greed, daemon=True).start()
        threading.Thread(target=update_openai_signal, daemon=True).start()
        time.sleep(3)  # dar tiempo a las APIs

        for symbol in futures_state["pairs"]:
            try:
                # Obtener precio actual
                ticker = client.futures_symbol_ticker(symbol=symbol)
                price  = float(ticker["price"])
                hour   = time.gmtime().tm_hour

                # Solo operar en horario activo 13-21 UTC
                if not (13 <= hour <= 21):
                    continue

                # ── Gestión de posición existente ────────────────────────────
                pos = futures_state["positions"].get(symbol)
                if pos:
                    entry    = pos["entry"]
                    side     = pos["side"]  # BUY=long, SELL=short
                    qty      = pos["qty"]
                    sl_pct   = futures_state["stop_loss"] / 100
                    tp_pct   = futures_state["take_profit"] / 100
                    leverage = pos.get("leverage", 2)

                    if side == "BUY":
                        pnl_pct = (price - entry) / entry
                    else:
                        pnl_pct = (entry - price) / entry

                    pnl_usdt = pnl_pct * entry * qty * leverage

                    # Take Profit
                    if pnl_pct >= tp_pct:
                        try:
                            close_side = "SELL" if side == "BUY" else "BUY"
                            client.futures_create_order(
                                symbol=symbol, side=close_side,
                                type="MARKET", quantity=qty,
                                reduceOnly=True
                            )
                            with futures_lock:
                                del futures_state["positions"][symbol]
                                futures_state["stats"]["total_pnl"] += pnl_usdt
                                futures_state["stats"]["daily_pnl"]  += pnl_usdt
                                futures_state["stats"]["trades"]     += 1
                                futures_state["stats"]["wins"]       += 1
                            futures_log(f"✅ TAKE PROFIT {symbol} {'LONG' if side=='BUY' else 'SHORT'} @ ${price:.2f} | +${pnl_usdt:.2f} ({pnl_pct*100:.2f}%)", "sell")
                        except Exception as e:
                            futures_log(f"✗ Error cerrando {symbol}: {e}", "error")

                    # Stop Loss
                    elif pnl_pct <= -sl_pct:
                        try:
                            close_side = "SELL" if side == "BUY" else "BUY"
                            client.futures_create_order(
                                symbol=symbol, side=close_side,
                                type="MARKET", quantity=qty,
                                reduceOnly=True
                            )
                            with futures_lock:
                                del futures_state["positions"][symbol]
                                futures_state["stats"]["total_pnl"] += pnl_usdt
                                futures_state["stats"]["daily_pnl"]  += pnl_usdt
                                futures_state["stats"]["trades"]     += 1
                                futures_state["stats"]["losses"]     += 1
                            futures_log(f"🛑 STOP LOSS {symbol} {'LONG' if side=='BUY' else 'SHORT'} @ ${price:.2f} | ${pnl_usdt:.2f} ({pnl_pct*100:.2f}%)", "error")
                        except Exception as e:
                            futures_log(f"✗ Error stop loss {symbol}: {e}", "error")
                    else:
                        futures_log(f"◷ {'LONG' if side=='BUY' else 'SHORT'} {symbol} @ ${price:.2f} | P&L:{pnl_pct*100:.2f}% (${pnl_usdt:.2f})", "info")
                    continue

                # ── Evaluación de nueva posición ──────────────────────────────
                min_score = futures_state["min_score"]

                # Score para LONG
                long_score, long_reasons, long_vetoes = get_futures_score(symbol, "long")
                # Score para SHORT
                short_score, short_reasons, short_vetoes = get_futures_score(symbol, "short")

                direction = None
                score     = 0
                reasons   = []

                if long_score >= min_score and not long_vetoes and long_score > short_score:
                    direction = "long"
                    score     = long_score
                    reasons   = long_reasons
                elif short_score >= min_score and not short_vetoes and short_score > long_score:
                    direction = "short"
                    score     = short_score
                    reasons   = short_reasons

                if not direction:
                    futures_log(f"⏸ {symbol} | LONG:{long_score} SHORT:{short_score} | sin señal clara", "info")
                    continue

                # ── Ejecutar orden ────────────────────────────────────────────
                capital   = futures_state["capital"]
                leverage  = futures_state["leverage"]

                # Configurar apalancamiento
                try:
                    client.futures_change_leverage(symbol=symbol, leverage=leverage)
                except:
                    pass

                # Calcular cantidad
                notional = capital * leverage
                qty      = round(notional / price, 3)
                side     = "BUY" if direction == "long" else "SELL"

                futures_log(f"🎯 SEÑAL {direction.upper()} {symbol} | score:{score} | {' · '.join(reasons[:3])}", "buy" if direction == "long" else "sell")

                order = client.futures_create_order(
                    symbol=symbol, side=side,
                    type="MARKET", quantity=qty
                )
                fill_price = float(order.get("avgPrice", price))

                with futures_lock:
                    futures_state["positions"][symbol] = {
                        "side":     side,
                        "entry":    fill_price,
                        "qty":      qty,
                        "leverage": leverage,
                        "time":     time.time(),
                        "score":    score,
                    }

                futures_log(f"▲ ABIERTO {direction.upper()} {symbol} @ ${fill_price:.2f} | qty:{qty} | 2x | SL:{futures_state['stop_loss']}% TP:{futures_state['take_profit']}%", "buy" if direction == "long" else "sell")

            except Exception as e:
                futures_log(f"✗ Error futures {symbol}: {e}", "error")

    except Exception as e:
        futures_log(f"✗ Error general futures: {e}", "error")


def futures_loop():
    futures_log("🚀 Bot de futuros iniciado.", "info")
    while futures_state["enabled"]:
        try:
            futures_cycle()
        except Exception as e:
            futures_log(f"✗ Error loop futures: {e}", "error")
        time.sleep(45)
    futures_log("⏹ Bot de futuros detenido.", "info")


# ─── Endpoints de futuros ─────────────────────────────────────────────────────
@app.route("/futures/start", methods=["POST"])
def futures_start():
    global futures_thread
    data = request.get_json(silent=True) or {}
    if futures_state["enabled"]:
        return jsonify({"ok": False, "msg": "Futuros ya corriendo"})
    if "capital"      in data: futures_state["capital"]      = float(data["capital"])
    if "leverage"     in data: futures_state["leverage"]     = int(data["leverage"])
    if "stop_loss"    in data: futures_state["stop_loss"]    = float(data["stop_loss"])
    if "take_profit"  in data: futures_state["take_profit"]  = float(data["take_profit"])
    if "min_score"    in data: futures_state["min_score"]    = int(data["min_score"])
    if "pairs"        in data: futures_state["pairs"]        = data["pairs"]
    futures_state["enabled"] = True
    futures_thread = threading.Thread(target=futures_loop, daemon=True)
    futures_thread.start()
    return jsonify({"ok": True, "msg": "Bot de futuros iniciado"})


@app.route("/futures/stop", methods=["POST"])
def futures_stop():
    futures_state["enabled"] = False
    return jsonify({"ok": True, "msg": "Bot de futuros deteniendo..."})


@app.route("/futures/status")
def futures_status():
    return jsonify({
        "enabled":    futures_state["enabled"],
        "positions":  futures_state["positions"],
        "pairs":      futures_state["pairs"],
        "capital":    futures_state["capital"],
        "leverage":   futures_state["leverage"],
        "stop_loss":  futures_state["stop_loss"],
        "take_profit": futures_state["take_profit"],
        "min_score":  futures_state["min_score"],
        "stats":      futures_state["stats"],
        "log":        futures_state["log"][-50:],
    })


@app.route("/futures/close", methods=["POST"])
def futures_close():
    """Cierra una posición de futuros manualmente."""
    data   = request.get_json(silent=True) or {}
    symbol = data.get("symbol")
    if not symbol or symbol not in futures_state["positions"]:
        return jsonify({"ok": False, "msg": "Posición no encontrada"})
    try:
        client = get_futures_client()
        pos    = futures_state["positions"][symbol]
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price  = float(ticker["price"])
        close_side = "SELL" if pos["side"] == "BUY" else "BUY"
        client.futures_create_order(
            symbol=symbol, side=close_side,
            type="MARKET", quantity=pos["qty"],
            reduceOnly=True
        )
        with futures_lock:
            del futures_state["positions"][symbol]
        futures_log(f"🔒 Posición {symbol} cerrada manualmente @ ${price:.2f}", "info")
        return jsonify({"ok": True, "msg": f"{symbol} cerrada"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


# ─── Auto-arranque al cargar el módulo (compatible con gunicorn) ──────────────
def preload_apis():
    """Precarga APIs con espacio entre llamadas para no bloquear."""
    time.sleep(60)  # esperar 1 minuto antes de primera precarga
    while True:
        try:
            logger.info("🔄 Precargando APIs en background...")
            # Solo Fear & Greed y OpenAI — no bloquean
            get_fear_greed()
            time.sleep(5)
            update_openai_signal()
        except Exception as e:
            logger.error(f"Error precargando APIs: {e}")
        time.sleep(1800)  # cada 30 minutos


def _auto_start():
    global bot_thread, _bot_started
    if _bot_started:
        return
    if not _is_primary_worker():
        logger.info("Worker secundario — bot no arrancado.")
        return
    _bot_started = True
    init_db()
    load_state()
    bot_state["running"] = True
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    logger.info("🚀 Bot spot arrancado automáticamente.")

    # Precargar APIs en background
    threading.Thread(target=preload_apis, daemon=True).start()

    # Auto-arranque de futuros si estaba activo
    if futures_state.get("auto_start", True):
        global futures_thread
        futures_state["enabled"] = True
        futures_thread = threading.Thread(target=futures_loop, daemon=True)
        futures_thread.start()
        logger.info("🚀 Bot de futuros arrancado automáticamente.")

_auto_start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Arrancar bot automáticamente al iniciar
    bot_state["running"] = True
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    logger.info("🚀 Servidor iniciado — bot arrancado automáticamente.")
    app.run(host="0.0.0.0", port=port, debug=False)
