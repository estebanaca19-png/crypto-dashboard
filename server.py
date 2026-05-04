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
        from flask import request
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
        return jsonify([{"tiempo":v[0],"open":float(v[1]),"high":float(v[2]),"low":float(v[3]),"close":float(v[4]),"volumen":float(v[5])} for v in velas])
    except Exception as e:
        return jsonify({"error": str(e)}), 500