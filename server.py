from flask import Flask, jsonify
from flask_cors import CORS
from binance.client import Client
import os

app = Flask(__name__)
CORS(app)

API_KEY = os.environ.get("API_KEY")
SECRET_KEY = os.environ.get("SECRET_KEY")

client = Client(API_KEY, SECRET_KEY, tld="com")

@app.route("/precios")
def precios():
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"]
    data = []
    for s in symbols:
        ticker = client.get_ticker(symbol=s)
        data.append({
            "symbol": s,
            "name": s.replace("USDT", ""),
            "price": float(ticker["lastPrice"]),
            "change": float(ticker["priceChangePercent"])
        })
    return jsonify(data)

@app.route("/balance")
def balance():
    cuenta = client.get_account()
    monedas = ["BTC", "ETH", "SOL", "DOGE"]
    data = []
    for b in cuenta["balances"]:
        if b["asset"] in monedas:
            data.append({
                "asset": b["asset"],
                "free": float(b["free"]),
                "locked": float(b["locked"])
            })
    return jsonify(data)

if __name__ == "__main__":
    app.run(debug=True)
