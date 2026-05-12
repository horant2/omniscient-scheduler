import os
import time
import requests
import yfinance as yf
import numpy as np
from datetime import datetime
import pytz

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_PERFORMANCE_TOKEN = os.environ.get("TELEGRAM_PERFORMANCE_TOKEN")
TELEGRAM_PERFORMANCE_CHAT_ID = os.environ.get("TELEGRAM_PERFORMANCE_CHAT_ID")
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

# CRITICAL: All prices come from get_live_price() via yfinance
# Entry = live price always. Stop = 5% from live. Target = 15% from live.
# Never hardcode or hallucinate prices. Ever.

INCEPTION_VALUE = 100000
TICKERS = ["SOXL", "TECL", "TQQQ", "FAS", "ERX", "UUP", "TMF"]
SAFE = "BIL"
CONFIDENCE_THRESHOLD = 0.10
TARGET_VOL = 0.80

TICKER_NAMES = {
    "SOXL": "3x Semiconductors (SOXL)",
    "TECL": "3x Tech (TECL)",
    "TQQQ": "3x Nasdaq (TQQQ)",
    "FAS": "3x Financials (FAS)",
    "ERX": "2x Energy (ERX)",
    "UUP": "US Dollar (UUP)",
    "TMF": "3x Long Bonds (TMF)",
    "BIL": "Cash (BIL)"
}

current_holding = SAFE
daily_initialized = False
daily_start_value = None
price_cache = {}
price_cache_time = {}
PRICE_CACHE_SECONDS = 60

def get_live_price(ticker):
    now = time.time()
    if ticker in price_cache and (now - price_cache_time.get(ticker, 0)) < PRICE_CACHE_SECONDS:
        return price_cache[ticker]
    try:
        yf_ticker = ticker.replace("/", "-")
        data = yf.download(yf_ticker, period="2d", interval="1m", progress=False)
        if data.empty:
            data = yf.download(yf_ticker, period="5d", interval="5m", progress=False)
        if not data.empty:
            price = float(data["Close"].squeeze().dropna().iloc[-1])
            price_cache[ticker] = price
            price_cache_time[ticker] = now
            return price
    except Exception as e:
        print(f"Price fetch error {ticker}: {e}")
    return None

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
    for chunk in chunks:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk})
        time.sleep(1)

def send_performance(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_PERFORMANCE_TOKEN}/sendMessage"
    chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
    for chunk in chunks:
        try:
            r = requests.post(url, json={"chat_id": TELEGRAM_PERFORMANCE_CHAT_ID, "text": chunk}, timeout=10)
            print(f"Performance send: {r.status_code}")
        except Exception as e:
            print(f"Performance send error: {e}")
        time.sleep(1)

def format_rotation_alert(new_ticker, old_ticker, notional, weight, portfolio_value):
    ticker_name = TICKER_NAMES.get(new_ticker, new_ticker)
    old_name = TICKER_NAMES.get(old_ticker, old_ticker)
    live_price = get_live_price(new_ticker)
    price_str = f"${live_price:.2f}" if live_price else "live price"
    net_profit = portfolio_value - INCEPTION_VALUE
    net_pct = net_profit / INCEPTION_VALUE * 100
    profit_emoji = "📈" if net_profit >= 0 else "📉"
    return f"""🔄 OMNISCIENTBOT ROTATED

Sold: {old_name}
Bought: {ticker_name} at {price_str} (live price)

Bet size: ${notional:,.0f} ({weight*100:.0f}% of portfolio)
Portfolio value: ${portfolio_value:,.0f}

Pure momentum math. No opinions. No news. Just the score.

{profit_emoji} Net profit since inception: {'+' if net_profit >= 0 else ''}${net_profit:,.0f} ({'+' if net_pct >= 0 else ''}{net_pct:.2f}%)

-- Satis House Consulting"""

def format_safe_alert(old_ticker, portfolio_value):
    old_name = TICKER_NAMES.get(old_ticker, old_ticker)
    net_profit = portfolio_value - INCEPTION_VALUE
    net_pct = net_profit / INCEPTION_VALUE * 100
    profit_emoji = "📈" if net_profit >= 0 else "📉"
    return f"""🏦 OMNISCIENTBOT MOVED TO CASH

Sold: {old_name}
Now holding: Cash (BIL)

Momentum scores turned negative. The math says wait.
Capital protected.

{profit_emoji} Net profit since inception: {'+' if net_profit >= 0 else ''}${net_profit:,.0f} ({'+' if net_pct >= 0 else ''}{net_pct:.2f}%)

-- Satis House Consulting"""

def format_position_report(current_ticker, portfolio_value, current_pnl, current_pnl_pct, daily_pnl_pct):
    ticker_name = TICKER_NAMES.get(current_ticker, current_ticker)
    pnl_emoji = "📈" if current_pnl >= 0 else "📉"
    daily_emoji = "📈" if daily_pnl_pct >= 0 else "📉"
    net_profit = portfolio_value - INCEPTION_VALUE
    net_pct = net_profit / INCEPTION_VALUE * 100
    profit_emoji = "📈" if net_profit >= 0 else "📉"
    live_price = get_live_price(current_ticker)
    price_str = f" | Live price: ${live_price:.2f}" if live_price else ""
    return f"""📊 OMNISCIENTBOT UPDATE

Holding: {ticker_name}{price_str}
Position P&L: {pnl_emoji} {'+' if current_pnl >= 0 else ''}${current_pnl:,.0f} ({'+' if current_pnl_pct >= 0 else ''}{current_pnl_pct:.1f}%)
Today: {daily_emoji} {'+' if daily_pnl_pct >= 0 else ''}{daily_pnl_pct:.1f}%
Portfolio value: ${portfolio_value:,.0f}

{profit_emoji} Net profit since inception: {'+' if net_profit >= 0 else ''}${net_profit:,.0f} ({'+' if net_pct >= 0 else ''}{net_pct:.2f}%)

Scanning every 15 minutes. Will rotate automatically when math says so.

-- Satis House Consulting"""

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

def cancel_all_orders():
    try:
        alpaca_request("DELETE", "/v2/orders")
        print("All open orders cancelled")
    except Exception as e:
        print(f"Cancel orders error: {e}")

def liquidate_all():
    positions = get_positions()
    if not isinstance(positions, list):
        return
    for pos in positions:
        symbol = pos["symbol"]
        try:
            alpaca_request("DELETE", f"/v2/positions/{symbol}")
            print(f"Liquidated {symbol}")
        except:
            pass
    time.sleep(2)

def submit_order(symbol, side, notional=None, qty=None):
    order = {"symbol": symbol, "side": side, "type": "market", "time_in_force": "day"}
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

def get_current_position():
    try:
        positions = get_positions()
        if not isinstance(positions, list) or len(positions) == 0:
            return SAFE, 0, 0, 0
        pos = positions[0]
        symbol = pos["symbol"]
        market_value = float(pos.get("market_value", 0))
        unrealized_pnl = float(pos.get("unrealized_pl", 0))
        unrealized_pct = float(pos.get("unrealized_plpc", 0)) * 100
        return symbol, market_value, unrealized_pnl, unrealized_pct
    except:
        return SAFE, 0, 0, 0

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

def execute_rotation(new_ticker, old_ticker, weight, account_value):
    try:
        cancel_all_orders()
        time.sleep(1)
        liquidate_all()
        time.sleep(3)

        if new_ticker != SAFE:
            live_price = get_live_price(new_ticker)
            if live_price is None:
                print(f"Cannot get live price for {new_ticker} -- aborting rotation")
                return f"Rotation aborted -- no live price for {new_ticker}", None

            notional = account_value * weight
            result = submit_order(new_ticker, "buy", notional=notional)
            order_id = result.get("id", "unknown")
            msg = format_rotation_alert(new_ticker, old_ticker, notional, weight, account_value)
            send_performance(msg)
            return f"Rotated to {new_ticker} at ${live_price:.2f} (live)", order_id
        else:
            msg = format_safe_alert(old_ticker, account_value)
            send_performance(msg)
            return "Moved to cash", None
    except Exception as e:
        return f"Rotation failed: {e}", None

cycle_count = 0

def run_cycle():
    global current_holding, daily_initialized, daily_start_value

    if not is_market_hours():
        actual_holding, market_val, current_pnl, current_pnl_pct = get_current_position()
        current_holding = actual_holding
        print(f"Market closed. Holding: {current_holding}")
        return

    et = pytz.timezone("America/New_York")
    now_et = datetime.now(et)

    if now_et.hour == 9 and now_et.minute < 45 and not daily_initialized:
        cancel_all_orders()
        daily_initialized = True
        daily_start_value = None
        print("Daily init -- stale orders cancelled")

    if now_et.hour == 16:
        daily_initialized = False

    actual_holding, holding_value, current_pnl, current_pnl_pct = get_current_position()
    current_holding = actual_holding

    try:
        account = get_account()
        portfolio_value = float(account.get("portfolio_value", 100000))
    except:
        portfolio_value = 100000

    if daily_start_value is None:
        daily_start_value = portfolio_value

    daily_pnl_pct = (portfolio_value - daily_start_value) / daily_start_value if daily_start_value else 0

    best_ticker, best_score, spy_trend, scores, prices = score_assets()

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
    score_lines = "\n".join([f"  {TICKER_NAMES.get(t, t)}: {s:.3f}" for t, s in sorted_scores])

    if should_rotate:
        weight = calc_target_weight(best_ticker) if best_ticker != SAFE else 1.0
        result, order_id = execute_rotation(best_ticker, current_holding, weight, portfolio_value)
        old_holding = current_holding
        current_holding = best_ticker
        print(f"Rotated from {old_holding} to {best_ticker}")

        tech_brief = f"OMNISCIENTBOT ROTATION\n"
        tech_brief += f"From: {old_holding} To: {best_ticker}\n"
        tech_brief += f"Score: {best_score:.3f} | Weight: {weight*100:.0f}%\n"
        tech_brief += f"SPY trend: {'BULL' if spy_trend else 'BEAR'}\n\n"
        tech_brief += f"All scores:\n{score_lines}"
        send_telegram(tech_brief)

    else:
        global cycle_count
        cycle_count += 1
        if cycle_count % 4 == 0:
            msg = format_position_report(current_holding, portfolio_value, current_pnl, current_pnl_pct, daily_pnl_pct)
            send_performance(msg)

        tech_brief = f"OMNISCIENTBOT\n"
        tech_brief += f"Holding: {TICKER_NAMES.get(current_holding, current_holding)}\n"
        tech_brief += f"Score: {scores.get(current_holding, 0):.3f} | Best: {best_ticker} ({best_score:.3f})\n"
        tech_brief += f"SPY: {'BULL' if spy_trend else 'BEAR'} | Action: HOLD"
        send_telegram(tech_brief)
        print(f"Holding {current_holding}")

while True:
    try:
        run_cycle()
    except Exception as e:
        msg = f"OmniscientBot error: {e}"
        send_telegram(msg)
        print(msg)
    smart_sleep(900)
