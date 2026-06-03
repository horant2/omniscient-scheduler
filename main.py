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

INCEPTION_VALUE = 100000
TICKERS = ["SOXL", "TECL", "TQQQ", "FAS", "ERX", "UUP", "TMF"]
SAFE = "BIL"
CONFIDENCE_THRESHOLD = 0.10
TARGET_VOL = 0.80

TICKER_NAMES = {
    "SOXL": "SOXL (3x Semiconductors)",
    "TECL": "TECL (3x Tech)",
    "TQQQ": "TQQQ (3x Nasdaq)",
    "FAS":  "FAS (3x Financials)",
    "ERX":  "ERX (2x Energy)",
    "UUP":  "UUP (US Dollar)",
    "TMF":  "TMF (3x Long Bonds)",
    "BIL":  "BIL (Cash)"
}

current_holding = SAFE
daily_initialized = False
daily_start_value = None
eod_summary_sent = False
price_cache = {}
price_cache_time = {}
PRICE_CACHE_SECONDS = 60

def get_live_price(ticker):
    now = time.time()
    if ticker in price_cache and (now - price_cache_time.get(ticker, 0)) < PRICE_CACHE_SECONDS:
        return price_cache[ticker]
    try:
        data = yf.download(ticker.replace("/", "-"), period="2d", interval="1m", progress=False)
        if data.empty:
            data = yf.download(ticker.replace("/", "-"), period="5d", interval="5m", progress=False)
        if not data.empty:
            price = float(data["Close"].squeeze().dropna().iloc[-1])
            price_cache[ticker] = price
            price_cache_time[ticker] = now
            return price
    except Exception as e:
        print(f"Price fetch error {ticker}: {e}")
    return None

def send_signal(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in [message[i:i+4000] for i in range(0, len(message), 4000)]:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk})
        time.sleep(1)

def alpaca_request(method, endpoint, data=None):
    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type": "application/json"
    }
    url = f"{ALPACA_BASE_URL}{endpoint}"
    if method == "GET":
        return requests.get(url, headers=headers).json()
    elif method == "POST":
        return requests.post(url, headers=headers, json=data).json()
    elif method == "DELETE":
        return requests.delete(url, headers=headers).json()

def get_account():
    return alpaca_request("GET", "/v2/account")

def get_positions():
    return alpaca_request("GET", "/v2/positions")

def cancel_all_orders():
    try:
        alpaca_request("DELETE", "/v2/orders")
    except:
        pass

def liquidate_all():
    positions = get_positions()
    if not isinstance(positions, list):
        return
    for pos in positions:
        try:
            alpaca_request("DELETE", f"/v2/positions/{pos['symbol']}")
        except:
            pass
    time.sleep(2)

def submit_order(symbol, side, notional=None):
    order = {"symbol": symbol, "side": side, "type": "market", "time_in_force": "day"}
    if notional:
        order["notional"] = str(round(notional, 2))
    return alpaca_request("POST", "/v2/orders", order)

def is_market_hours():
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_t <= now <= close_t

def get_current_position():
    try:
        positions = get_positions()
        if not isinstance(positions, list) or len(positions) == 0:
            return SAFE, 0, 0
        pos = positions[0]
        return (pos["symbol"],
                float(pos.get("unrealized_pl", 0)),
                float(pos.get("unrealized_plpc", 0)) * 100)
    except:
        return SAFE, 0, 0

def keepalive():
    try:
        get_account()
    except:
        pass

def smart_sleep(total_seconds):
    elapsed = 0
    while elapsed < total_seconds:
        chunk = min(480, total_seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk
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
    try:
        spy_close = yf.download("SPY", period="220d", interval="1d", progress=False)["Close"].squeeze()
        spy_trend = bool(spy_close.iloc[-1] > spy_close.rolling(200).mean().iloc[-1])
    except:
        spy_trend = True

    for ticker in TICKERS:
        try:
            close = yf.download(ticker, period="100d", interval="1d", progress=False)["Close"].squeeze()
            if len(close) < 65:
                continue
            roc_fast = float((close.iloc[-1] - close.iloc[-10]) / close.iloc[-10])
            roc_med  = float((close.iloc[-1] - close.iloc[-22]) / close.iloc[-22])
            roc_slow = float((close.iloc[-1] - close.iloc[-64]) / close.iloc[-64])
            vol      = float(close.pct_change().rolling(21).std().iloc[-1])
            rsi      = float(calc_rsi(close).iloc[-1])
            sma50    = float(close.rolling(50).mean().iloc[-1])
            price    = float(close.iloc[-1])
            if vol == 0 or np.isnan(vol):
                vol = 0.01
            wmom   = roc_fast*0.5 + roc_med*0.3 + roc_slow*0.2
            radj   = wmom / vol
            trend  = 1.0 if price > sma50 else 0.5
            pen    = 0.9 if (rsi > 85 or rsi < 30) else 1.0
            scores[ticker] = radj * trend * pen
        except Exception as e:
            print(f"Score error {ticker}: {e}")

    if not scores:
        return SAFE, 0, spy_trend, scores

    best_ticker = max(scores, key=scores.get)
    best_score  = scores[best_ticker]

    if not spy_trend:
        uup_score = scores.get("UUP", -999)
        if uup_score > 0 and uup_score > best_score:
            best_ticker = "UUP"
            best_score  = uup_score
        elif best_score < 0:
            best_ticker = SAFE
            best_score  = 0

    if best_score <= 0:
        best_ticker = SAFE

    return best_ticker, best_score, spy_trend, scores

def calc_target_weight(ticker):
    try:
        close = yf.download(ticker, period="30d", interval="1d", progress=False)["Close"].squeeze()
        rets  = close.pct_change().dropna()
        vol   = float(np.std(rets) * np.sqrt(252))
        return min(1.0, TARGET_VOL / vol) if vol > 0 else 1.0
    except:
        return 1.0

def execute_rotation(new_ticker, old_ticker, weight, account_value):
    try:
        cancel_all_orders()
        time.sleep(1)
        liquidate_all()
        time.sleep(3)
        if new_ticker != SAFE:
            live_price = get_live_price(new_ticker)
            if live_price is None:
                return
            notional = account_value * weight
            submit_order(new_ticker, "buy", notional=notional)
    except Exception as e:
        print(f"Rotation failed: {e}")

daily_decision_made = False
daily_best_ticker   = None

def run_cycle():
    global current_holding, daily_initialized, daily_start_value
    global eod_summary_sent, cycle_count, daily_open_price
    global daily_decision_made, daily_best_ticker

    et     = pytz.timezone("America/New_York")
    now_et = datetime.now(et)

    if now_et.hour >= 17:
        eod_summary_sent   = False
        daily_decision_made = False

    if not is_market_hours():
        symbol, _, _ = get_current_position()
        current_holding = symbol
        print(f"Market closed. Holding: {current_holding}")
        return

    if now_et.hour == 9 and now_et.minute < 45 and not daily_initialized:
        cancel_all_orders()
        daily_initialized   = True
        daily_start_value   = None
        daily_open_price    = None
        eod_summary_sent    = False
        daily_decision_made = False
        print("Daily init -- open price reset")

    if now_et.hour == 16:
        daily_initialized = False

    symbol, pnl, pnl_pct = get_current_position()
    current_holding = symbol

    try:
        account       = get_account()
        portfolio_val = float(account.get("portfolio_value", 100000))
    except:
        portfolio_val = 100000

    if daily_start_value is None:
        daily_start_value = portfolio_val

    live_price = get_live_price(current_holding)

    # Get today's actual open price from yfinance for accurate daily % calculation
    if daily_open_price is None and live_price:
        try:
            today_data = yf.download(current_holding, period="1d", interval="1m", progress=False)
            if not today_data.empty:
                daily_open_price = float(today_data["Open"].iloc[0])
                print(f"Daily open price from yfinance: {current_holding} @ ${daily_open_price:.2f}")
        except:
            daily_open_price = live_price

    daily_pct = 0.0
    if daily_open_price and live_price and daily_open_price > 0:
        daily_pct = (live_price - daily_open_price) / daily_open_price * 100

    # ── ONE DECISION PER DAY ──────────────────────────────────
    # Score assets once at market open between 9:35 and 9:50 AM.
    # Hold that decision all day. This matches the daily backtest cadence.
    # The original strategy made one decision per day on daily closing prices.
    # Running scoring every 15 minutes on intraday prices causes whipsawing.

    if now_et.hour == 9 and 35 <= now_et.minute <= 50 and not daily_decision_made:
        print("Running daily scoring decision...")
        best_ticker, best_score, spy_trend, scores = score_assets()
        daily_best_ticker   = best_ticker
        daily_decision_made = True

        # Send morning briefing
        name   = TICKER_NAMES.get(current_holding, current_holding)
        target = TICKER_NAMES.get(best_ticker, best_ticker)
        emoji  = "📈" if pnl_pct >= 0 else "📉"

        if current_holding == best_ticker:
            action = f"HOLD {name}"
        else:
            action = f"ROTATE: Sell {name} / Buy {target}"

        msg = (
            f"☀️ MORNING SIGNAL\n\n"
            f"Holding: {name}\n"
            f"Action: {action}\n\n"
            f"Market: {'BULL' if spy_trend else 'BEAR'}"
        )
        send_signal(msg)

        # Execute rotation if needed
        if current_holding != best_ticker:
            weight = calc_target_weight(best_ticker) if best_ticker != SAFE else 1.0
            execute_rotation(best_ticker, current_holding, weight, portfolio_val)
            old_name  = TICKER_NAMES.get(current_holding, current_holding)
            new_name  = TICKER_NAMES.get(best_ticker, best_ticker)
            new_price = get_live_price(best_ticker)
            price_str = f" at ${new_price:.2f}" if new_price else ""
            send_signal(
                f"🔴 SELL {old_name}\n"
                f"🟢 BUY {new_name}{price_str}\n\n"
                f"Momentum rotated. Make the switch."
            )
            daily_open_price = None
            current_holding  = best_ticker
            print(f"Rotated to {best_ticker}")

    # End of day summary at 3:45 PM ET
    if now_et.hour == 15 and now_et.minute >= 45 and not eod_summary_sent:
        name  = TICKER_NAMES.get(current_holding, current_holding)
        emoji = "📈" if daily_pct >= 0 else "📉"
        msg   = (
            f"{emoji} {name}\n"
            f"Today: {daily_pct:+.2f}%"
        )
        send_signal(msg)
        eod_summary_sent = True
        print("EOD summary sent")

    cycle_count += 1
    print(f"Holding {current_holding} | Decision made today: {daily_decision_made}")

# Startup message
send_signal(
    f"✅ OmniscientBot running\n"
    f"Daily update at 3:45 PM ET.\n"
    f"Rotation alerts fire instantly.\n\n"
    f"Note: if market is closed, position shows as Cash until open. "
    f"Your holding does not change overnight."
)

while True:
    try:
        run_cycle()
    except Exception as e:
        print(f"Error: {e}")
    smart_sleep(900)
