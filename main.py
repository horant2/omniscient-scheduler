import os
import time
import requests
import yfinance as yf
import numpy as np
from datetime import datetime
import pytz

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

TICKERS = ["SOXL", "TECL", "TQQQ", "FAS", "ERX", "UUP", "TMF"]
SAFE = "BIL"
CONFIDENCE_THRESHOLD = 0.10
TARGET_VOL = 0.80
LOOKBACK_VOL = 20

current_holding = SAFE
daily_initialized = False

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
    for chunk in chunks:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk})
        time.sleep(1)

def alpaca_request(method, endpoint, data=None, params=None):
    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type": "application/json"
    }
    url = f"{ALPACA_BASE_URL}{endpoint}"
    if method == "GET":
        return requests.get(url, headers=headers, params=params).json()
    elif method == "POST":
        return requests.post(url, headers=headers, json=data).json()
    elif method == "DELETE":
        return requests.delete(url, headers=headers).json()

def get_account():
    return alpaca_request("GET", "/v2/account")

def get_positions():
    return alpaca_request("GET", "/v2/positions")

def get_open_orders():
    return alpaca_request("GET", "/v2/orders", params={"status": "open"})

def cancel_all_orders():
    try:
        alpaca_request("DELETE", "/v2/orders")
        print("All open orders cancelled")
    except Exception as e:
        print(f"Cancel orders error: {e}")

def liquidate_position(symbol):
    try:
        alpaca_request("DELETE", f"/v2/positions/{symbol}")
        print(f"Liquidated {symbol}")
    except Exception as e:
        print(f"Liquidate error for {symbol}: {e}")

def liquidate_all():
    positions = get_positions()
    if not isinstance(positions, list):
        return
    for pos in positions:
        symbol = pos["symbol"]
        liquidate_position(symbol)
    time.sleep(2)

def submit_order(symbol, side, notional=None, qty=None):
    order = {
        "symbol": symbol,
        "side": side,
        "type": "market",
        "time_in_force": "day"
    }
    if notional:
        order["notional"] = str(round(notional, 2))
    elif qty:
        order["qty"] = str(qty)
    return alpaca_request("POST", "/v2/orders", order)

def is_market_hours():
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close

def get_current_holding():
    try:
        positions = get_positions()
        if not isinstance(positions, list) or len(positions) == 0:
            return SAFE, 0
        pos = positions[0]
        symbol = pos["symbol"]
        market_value = float(pos.get("market_value", 0))
        return symbol, market_value
    except:
        return SAFE, 0

def keepalive():
    try:
        get_account()
        print(f"Keepalive {datetime.now(pytz.utc).strftime('%H:%M:%S')}")
    except:
        pass

def smart_sleep(total_seconds):
    interval = 480
    elapsed = 0
    while elapsed < total_seconds:
        sleep_chunk = min(interval, total_seconds - elapsed)
        time.sleep(sleep_chunk)
        elapsed += sleep_chunk
        if elapsed < total_seconds:
            keepalive()

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def score_assets():
    scores = {}
    prices = {}

    try:
        spy_data = yf.download("SPY", period="220d", interval="1d", progress=False)
        spy_close = spy_data["Close"].squeeze()
        spy_sma200 = spy_close.rolling(200).mean().iloc[-1]
        spy_current = spy_close.iloc[-1]
        spy_trend = bool(spy_current > spy_sma200)
    except:
        spy_trend = True

    for ticker in TICKERS:
        try:
            df = yf.download(ticker, period="100d", interval="1d", progress=False)
            close = df["Close"].squeeze()
            if len(close) < 65:
                continue

            roc_fast = float((close.iloc[-1] - close.iloc[-10]) / close.iloc[-10])
            roc_med = float((close.iloc[-1] - close.iloc[-22]) / close.iloc[-22])
            roc_slow = float((close.iloc[-1] - close.iloc[-64]) / close.iloc[-64])
            vol = float(close.pct_change().rolling(21).std().iloc[-1])
            rsi = float(calc_rsi(close).iloc[-1])
            sma50 = float(close.rolling(50).mean().iloc[-1])
            price = float(close.iloc[-1])

            if vol == 0 or np.isnan(vol):
                vol = 0.01

            weighted_mom = (roc_fast * 0.5) + (roc_med * 0.3) + (roc_slow * 0.2)
            risk_adj_mom = weighted_mom / vol
            trend_score = 1.0 if price > sma50 else 0.5
            rsi_penalty = 0.9 if (rsi > 85 or rsi < 30) else 1.0
            final_score = risk_adj_mom * trend_score * rsi_penalty

            scores[ticker] = final_score
            prices[ticker] = price
        except Exception as e:
            print(f"Score error {ticker}: {e}")

    if not scores:
        return SAFE, 0, spy_trend, scores, prices

    sorted_assets = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_ticker = sorted_assets[0][0]
    best_score = sorted_assets[0][1]

    if not spy_trend:
        uup_score = scores.get("UUP", -999)
        if uup_score > 0 and uup_score > best_score:
            best_ticker = "UUP"
            best_score = uup_score
        elif best_score < 0:
            best_ticker = SAFE
            best_score = 0

    if best_score <= 0:
        best_ticker = SAFE

    return best_ticker, best_score, spy_trend, scores, prices

def calc_target_weight(ticker):
    try:
        df = yf.download(ticker, period="30d", interval="1d", progress=False)
        close = df["Close"].squeeze()
        rets = close.pct_change().dropna()
        curr_vol = float(np.std(rets) * np.sqrt(252))
        if curr_vol > 0:
            weight = TARGET_VOL / curr_vol
        else:
            weight = 1.0
        return min(1.0, weight)
    except:
        return 1.0

def execute_rotation(new_ticker, weight, account_value):
    try:
        cancel_all_orders()
        time.sleep(1)
        liquidate_all()
        time.sleep(3)

        if new_ticker != SAFE:
            notional = account_value * weight
            result = submit_order(new_ticker, "buy", notional=notional)
            order_id = result.get("id", "unknown")
            return f"ROTATED TO: {new_ticker}\nNotional: ${notional:,.0f} ({weight*100:.0f}% of portfolio)\nOrder ID: {order_id}"
        else:
            return f"MOVED TO SAFE: {SAFE}\nFully in cash equivalent."
    except Exception as e:
        return f"Rotation failed: {e}"

def run_cycle():
    global current_holding, daily_initialized

    if not is_market_hours():
        print(f"Market closed. Sleeping. Current holding: {current_holding}")
        brief = f"OMNISCIENTBOT\nTime: {datetime.now(pytz.utc).strftime('%Y-%m-%d %H:%M UTC')}\nMarket closed. Holding: {current_holding}\nNo action."
        send_telegram(brief)
        return

    et = pytz.timezone("America/New_York")
    now_et = datetime.now(et)
    if now_et.hour == 9 and now_et.minute < 45 and not daily_initialized:
        cancel_all_orders()
        daily_initialized = True
        print("Daily initialization -- stale orders cancelled")

    if now_et.hour == 16:
        daily_initialized = False

    actual_holding, holding_value = get_current_holding()
    current_holding = actual_holding

    best_ticker, best_score, spy_trend, scores, prices = score_assets()

    try:
        account = get_account()
        portfolio_value = float(account.get("portfolio_value", 100000))
    except:
        portfolio_value = 100000

    should_rotate = False

    if current_holding == SAFE:
        if best_score > 0.02:
            should_rotate = True
    elif current_holding == best_ticker:
        should_rotate = False
    else:
        current_score = scores.get(current_holding, -999)
        if best_score > current_score * (1 + CONFIDENCE_THRESHOLD):
            should_rotate = True
        elif current_score < -0.02:
            best_ticker = SAFE
            should_rotate = True

    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    score_lines = "\n".join([f"  {t}: {s:.3f}" for t, s in sorted_scores])

    brief = f"OMNISCIENTBOT CYCLE\n"
    brief += f"Time: {datetime.now(pytz.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
    brief += f"SPY Trend: {'BULL' if spy_trend else 'BEAR'}\n"
    brief += f"Portfolio: ${portfolio_value:,.0f}\n\n"
    brief += f"SCORES:\n{score_lines}\n\n"
    brief += f"CURRENT HOLDING: {current_holding}\n"
    brief += f"WINNER: {best_ticker} (score: {best_score:.3f})\n"

    if should_rotate:
        weight = calc_target_weight(best_ticker) if best_ticker != SAFE else 1.0
        result = execute_rotation(best_ticker, weight, portfolio_value)
        current_holding = best_ticker
        brief += f"\nROTATION EXECUTED\n{result}"
        send_telegram(brief)
        print(f"Rotated to {best_ticker}")
    else:
        brief += f"\nACTION: HOLD {current_holding}"
        send_telegram(brief)
        print(f"Holding {current_holding}")

while True:
    try:
        run_cycle()
    except Exception as e:
        msg = f"OmniscientBot error: {e}"
        send_telegram(msg)
        print(msg)
    smart_sleep(900)
