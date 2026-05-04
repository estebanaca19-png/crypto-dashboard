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
        velas = client.get_klines(
            symbol=symbol+"USDT",
            interval=Client.KLINE_INTERVAL_4HOUR,
            limit=24
        )
        data = []
        for v in velas:
            data.append({
                "tiempo": v[0],
                "open": float(v[1]),
                "high": float(v[2]),
                "low": float(v[3]),
                "close": float(v[4]),
                "volumen": float(v[5])
            })
        return jsonify(data)
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

if __name__ == "__main__":
    app.run(debug=True)