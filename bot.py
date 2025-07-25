
from flask import Flask, request, jsonify
import oandapyV20
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.pricing as pricing
import oandapyV20.endpoints.trades as trades
import json, threading, time, datetime

app = Flask(__name__)

# === OANDA CREDENTIALS ===
OANDA_API_KEY = "ddb165f2150034e54aeccd70541a667f-6450934cf8f0594e1db0cbe85779bada"
OANDA_ACCOUNT_ID = "101-004-35892024-001"
OANDA_URL = "https://api-fxpractice.oanda.com/v3"

client = oandapyV20.API(access_token=OANDA_API_KEY)

# === LOAD ZONES ===
with open("zones.json", "r") as f:
    zones_data = json.load(f)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print("Received:", data)

    symbol = data.get("ticker")
    entry_type = data.get("type")
    entry_price = float(data.get("price"))
    donMid = float(data.get("donMid"))

    pair_data = zones_data.get(symbol, {})
    pip_size = pair_data.get("pip_size", 0.0001)  # default to 0.0001 if missing
    zones = pair_data.get("zones", [])
    buffer = pair_data.get("buffer", 0)

    # Calculate SL and TP using pip_size
    if entry_type == "long":
        sl = donMid - 10 * pip_size   # 10 pips below donMid
        rr_dist = entry_price - sl
        tp = entry_price + 2 * rr_dist  # TP at 1:2 RR
    elif entry_type == "short":
        sl = donMid + 10 * pip_size   # 10 pips above donMid
        rr_dist = sl - entry_price
        tp = entry_price - 2 * rr_dist  # TP at 1:2 RR
    else:
        return jsonify({"status": "error", "msg": "Invalid entry type"}), 400

    # Check if blocked by S/R zones
    blocked = False #Change to False when live
    if entry_type == "long":
        for zone in zones:
            if entry_price < zone and (zone - entry_price) <= buffer:
                blocked = True
                break
    elif entry_type == "short":
        for zone in zones:
            if entry_price > zone and (entry_price - zone) <= buffer:
                blocked = True
                break

    if blocked:
        print("Trade blocked by S/R zone.")
        return jsonify({"status": "blocked", "reason": "Too close to S/R zone", "data":{"SL:":sl,"tp":tp}}), 200

    order = {
        "order": {
            "instrument": symbol,
            "units": "1000" if entry_type == "long" else "-1000",
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": f"{sl:.5f}"
            },
            "takeProfitOnFill": {
                "price": f"{tp:.5f}"
            }
        }
    }

    try:
        r = orders.OrderCreate(accountID=OANDA_ACCOUNT_ID, data=order)
        client.request(r)
        print("Trade executed.")

        trade_data = {
            "instrument": symbol,
            "entry_price": entry_price,
            "sl": sl,
            "tp": tp,
            "type": entry_type,
            "rr_ratio": 2,
            "breakeven_moved": False,
            "trail_moved": False,
            "time": datetime.datetime.utcnow().isoformat()
        }

        with open("open_trades.json", "w") as f:
            json.dump(trade_data, f)

        return jsonify({"status": "success", "order": order}), 200

    except Exception as e:
        print("Error placing order:", str(e))
        return jsonify({"status": "error", "msg": str(e)}), 500


def modify_stop_loss(instrument, new_sl_price):
    r = trades.OpenTrades(accountID=OANDA_ACCOUNT_ID)
    trades_data = client.request(r)["trades"]

    for tr in trades_data:
        if tr["instrument"] == instrument:
            trade_id = tr["id"]
            data = {
                "stopLoss": {
                    "price": f"{new_sl_price:.5f}"
                }
            }
            mod = trades.TradeCRCDO(accountID=OANDA_ACCOUNT_ID, tradeID=trade_id, data=data)
            try:
                client.request(mod)
                print(f"SL updated to {new_sl_price}")
            except Exception as e:
                print("SL update failed:", str(e))

def monitor_trade():
    while True:
        try:
            with open("open_trades.json", "r") as f:
                trade = json.load(f)
        except FileNotFoundError:
            time.sleep(10)
            continue

        symbol = trade["instrument"]
        entry = trade["entry_price"]
        sl = trade["sl"]
        rr = trade["rr_ratio"]
        trade_type = trade["type"]

        params = {"instruments": symbol}
        r = pricing.PricingInfo(accountID=OANDA_ACCOUNT_ID, params=params)
        price_data = client.request(r)
        bids = price_data['prices'][0]['bids']
        asks = price_data['prices'][0]['asks']
        current_price = float(asks[0]['price']) if trade_type == "long" else float(bids[0]['price'])

        r1 = abs(entry - sl)
        r1_price = entry + r1 if trade_type == "long" else entry - r1
        r1_5_price = entry + r1 * 1.5 if trade_type == "long" else entry - r1 * 1.5

        if not trade["breakeven_moved"] and ((trade_type == "long" and current_price >= r1_price) or
                                             (trade_type == "short" and current_price <= r1_price)):
            print("Moving SL to Breakeven")
            new_sl = entry
            modify_stop_loss(symbol, new_sl)
            trade["breakeven_moved"] = True

        if not trade["trail_moved"] and ((trade_type == "long" and current_price >= r1_5_price) or
                                         (trade_type == "short" and current_price <= r1_5_price)):
            print("Moving SL to 1R")
            new_sl = r1_price
            modify_stop_loss(symbol, new_sl)
            trade["trail_moved"] = True

        with open("open_trades.json", "w") as f:
            json.dump(trade, f)

        time.sleep(15)

if __name__ == "__main__":
    threading.Thread(target=monitor_trade, daemon=True).start()
    app.run(port=5000)
