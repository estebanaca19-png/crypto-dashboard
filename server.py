from flask import Flask, jsonify, request
from flask_cors import CORS
from binance.client import Client
import os
import time

app = Flask(__name__)
CORS(app)

API_KEY = os.environ.get("API_KEY")
SECRET_KEY = os.environ.get("SECRET_KEY")

# CACHE
cache = {}
CACHE_TTL = 30  # segundos

def get_cache(key):
    if key in cache:
        data, timestamp = cache[key]
        if time.time() - timestamp < CACHE_TTL:
            return data
    return None

def set_cache(key, data):
    cache[key] = (data, time.time())

def get_client():
    return Client(API_KEY, SECRET_KEY, tld="com")

@app.route("/precios")
def precios():
    cached = get_cache("precios")
    if cached:
        return jsonify(cached)
    client = get_client()
    tickers = client.get_ticker()
    data = []
    for t in tickers:
        if t["symbol"].endswith("USDT"):
            data.append({
                "symbol": t["symbol"],
                "name": t["symbol"].replace("USDT", ""),
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
    limite = int(request.args.get("limit", 24))
    cache_key = f"historial_{symbol}_{intervalo}_{limite}"
    cached = get_cache(cache_key)
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
            symbol=symbol+"USDT",
            interval=intervalos.get(intervalo, Client.KLINE_INTERVAL_4HOUR),
            limit=limite
        )
        data = [{"tiempo":v[0],"open":float(v[1]),"high":float(v[2]),"low":float(v[3]),"close":float(v[4]),"volumen":float(v[5])} for v in velas]
        set_cache(cache_key, data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/resumen/<symbol>")
def resumen(symbol):
    cache_key = f"resumen_{symbol}"
    cached = get_cache(cache_key)
    if cached:
        return jsonify(cached)
    try:
        client = get_client()
        velas = client.get_klines(symbol=symbol+"USDT", interval=Client.KLINE_INTERVAL_4HOUR, limit=24)
        ticker = client.get_ticker(symbol=symbol+"USDT")
        cierres = [float(v[4]) for v in velas]
        volumenes = [float(v[5]) for v in velas]
        precio = float(ticker["lastPrice"])
        cambio24h = float(ticker["priceChangePercent"])
        max24h = float(ticker["highPrice"])
        min24h = float(ticker["lowPrice"])
        vol24h = float(ticker["quoteVolume"])
        ganancias, perdidas = 0, 0
        for i in range(1, len(cierres)):
            diff = cierres[i] - cierres[i-1]
            if diff > 0: ganancias += diff
            else: perdidas += abs(diff)
        rsi = 100 if perdidas == 0 else 100-(100/(1+(ganancias/len(cierres))/(perdidas/len(cierres))))
        mitad = len(cierres)//2
        tendencia = "alcista" if sum(cierres[mitad:])/len(cierres[mitad:]) > sum(cierres[:mitad])/mitad else "bajista"
        cambio4h = ((cierres[-1]-cierres[-2])/cierres[-2])*100
        vol_prom = sum(volumenes)/len(volumenes)
        vol_estado = "alto" if volumenes[-1]>vol_prom*1.2 else "bajo" if volumenes[-1]<vol_prom*0.8 else "normal"
        if rsi<35 and tendencia=="alcista": señal="COMPRAR"
        elif rsi>65 and tendencia=="bajista": señal="VENDER"
        elif rsi<40: señal="COMPRAR"
        elif rsi>60: señal="VENDER"
        else: señal="MANTENER"
        data = {
            "symbol": symbol, "precio": precio,
            "cambio24h": round(cambio24h,2), "cambio4h": round(cambio4h,2),
            "max24h": max24h, "min24h": min24h, "vol24h": round(vol24h,0),
            "rsi": round(rsi,1), "tendencia": tendencia,
            "volumen_estado": vol_estado, "señal": señal,
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
            if float(b["free"])>0 or float(b["locked"])>0:
                data.append({"asset":b["asset"],"free":float(b["free"]),"locked":float(b["locked"])})
        set_cache("balance", data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/mi-ip")
def mi_ip():
    import requests
    r = requests.get("https://api.ipify.org?format=json")
    return jsonify(r.json())

if __name__ == "__main__":
    app.run(debug=True)