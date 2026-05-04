from flask import Flask, jsonify, request
from flask_cors import CORS
from binance.client import Client
import os

app = Flask(__name__)
CORS(app)

API_KEY = os.environ.get("API_KEY")
SECRET_KEY = os.environ.get("SECRET_KEY")

@app.route("/precios")
def precios():
    client = Client(API_KEY, SECRET_KEY, tld="com")
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
    return jsonify(data)

@app.route("/historial/<symbol>")
def historial(symbol):
    try:
        client = Client(API_KEY, SECRET_KEY, tld="com")
        intervalo = request.args.get("interval", "4h")
        limite = int(request.args.get("limit", 24))
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
        return jsonify([{
            "tiempo": v[0], "open": float(v[1]),
            "high": float(v[2]), "low": float(v[3]),
            "close": float(v[4]), "volumen": float(v[5])
        } for v in velas])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/balance")
def balance():
    try:
        client = Client(API_KEY, SECRET_KEY, tld="com")
        cuenta = client.get_account()
        data = []
        for b in cuenta["balances"]:
            if float(b["free"]) > 0 or float(b["locked"]) > 0:
                data.append({
                    "asset": b["asset"],
                    "free": float(b["free"]),
                    "locked": float(b["locked"])
                })
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/resumen/<symbol>")
def resumen(symbol):
    try:
        client = Client(API_KEY, SECRET_KEY, tld="com")
        velas = client.get_klines(
            symbol=symbol+"USDT",
            interval=Client.KLINE_INTERVAL_4HOUR,
            limit=24
        )
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
        rsi = 100 if perdidas == 0 else 100 - (100 / (1 + (ganancias/len(cierres)) / (perdidas/len(cierres))))

        mitad = len(cierres) // 2
        prom_rec = sum(cierres[mitad:]) / len(cierres[mitad:])
        prom_ant = sum(cierres[:mitad]) / mitad
        tendencia = "alcista" if prom_rec > prom_ant else "bajista"

        cambio4h = ((cierres[-1] - cierres[-2]) / cierres[-2]) * 100
        vol_prom = sum(volumenes) / len(volumenes)
        vol_actual = volumenes[-1]
        vol_estado = "alto" if vol_actual > vol_prom * 1.2 else "bajo" if vol_actual < vol_prom * 0.8 else "normal"

        if rsi < 35 and tendencia == "alcista": señal = "COMPRAR"
        elif rsi > 65 and tendencia == "bajista": señal = "VENDER"
        elif rsi < 40: señal = "COMPRAR"
        elif rsi > 60: señal = "VENDER"
        else: señal = "MANTENER"

        return jsonify({
            "symbol": symbol,
            "precio": precio,
            "cambio24h": round(cambio24h, 2),
            "cambio4h": round(cambio4h, 2),
            "max24h": max24h,
            "min24h": min24h,
            "vol24h": round(vol24h, 0),
            "rsi": round(rsi, 1),
            "tendencia": tendencia,
            "volumen_estado": vol_estado,
            "señal": señal,
            "cierres": cierres[-8:]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)