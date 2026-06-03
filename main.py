import os
import time
import requests
import yfinance as yf
import numpy as np
from datetime import datetime
import pytz

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")
ALPACA_API_KEY     = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY  = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL    = "https://paper-api.alpaca.markets"

# ── NAITIK GUPTA'S EXACT PARAMETERS ──────────────────────────
# Source: quantconnect.com/strategies/46/TheOmniscientParadox
# Author: Naitik Gupta, Desenyon Trade Club
# Version: V2.0.1, submitted Jan 31 2026
# Schedule: rebalance once daily, 5 minutes before market close

INCEPTION_VALUE       = 100000
TICKERS               = ["SOXL", "TECL", "TQQQ", "FAS", "ERX", "UUP", "TMF"]
SAFE                  = "BIL"
CONFIDENCE_THRESHOLD  = 0.10
TARGET_VOL            = 0.80
LOOKBACK_VOL          = 20          # Naitik uses 20, not 30
ROC_FAST              = 9           # Naitik uses 9, not 10
ROC_MED               = 21          # Naitik uses 21, not 22
ROC_SLOW              = 63          # Naitik uses 63, not 64
VOL_PERIOD            = 21          # std(21, DAILY)
RSI_PERIOD            = 14
SMA_PERIOD            = 50
SPY_SMA_PERIOD        = 200

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

current_holding    = SAFE
daily_rebalanced   = False
eod_summary_sent   = False
daily_open_price   = None
price_cache        = {}
price_cache_time   = {}
PRICE_CACHE_SECS   = 60

# ── HELPERS ───────────────────────────────────────────────────

def get_live_price(ticker):
    now = time.time()
    if ticker in price_cache and (now - price_cache_time.get(ticker, 0)) < PRICE_CACHE_SECS:
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
        print(f"Price error {ticker}: {e}")
    return None

def send_signal(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in [message[i:i+4000] for i in range(0, len(message), 4000)]:
        try:
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk}, timeout=10)
        except Exception as e:
            print(f"Telegram error: {e}")
        time.sleep(1)

def alpaca_request(method, endpoint, data=None):
    headers = {
        "APCA-API-KEY-ID":    ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type":       "application/json"
    }
    url = f"{ALPACA_BASE_URL}{endpoint}"
    if method == "GET":
        return requests.get(url, headers=headers, timeout=15).json()
    elif method == "POST":
        return requests.post(url, headers=headers, json=data, timeout=15).json()
    elif method == "DELETE":
        return requests.delete(url, headers=headers, timeout=15).json()

def get_account():      return alpaca_request("GET", "/v2/account")
def get_positions():    return alpaca_request("GET", "/v2/positions")

def cancel_all_orders():
    try: alpaca_request("DELETE", "/v2/orders")
    except: pass

def liquidate_all():
    positions = get_positions()
    if not isinstance(positions, list): return
    for pos in positions:
        try: alpaca_request("DELETE", f"/v2/positions/{pos['symbol']}")
        except: pass
    time.sleep(2)

def submit_order(symbol, side, notional=None):
    order = {"symbol": symbol, "side": side, "type": "market", "time_in_force": "day"}
    if notional:
        order["notional"] = str(round(notional, 2))
    return alpaca_request("POST", "/v2/orders", order)

def is_market_hours():
    et  = pytz.timezone("America/New_York")
    now = datetime.now(et)
    if now.weekday() >= 5: return False
    return (now.replace(hour=9, minute=30, second=0, microsecond=0) <= now <=
            now.replace(hour=16, minute=0,  second=0, microsecond=0))

def get_current_position():
    try:
        positions = get_positions()
        if not isinstance(positions, list) or not positions:
            return SAFE, 0.0, 0.0
        pos = positions[0]
        return (pos["symbol"],
                float(pos.get("unrealized_pl", 0)),
                float(pos.get("unrealized_plpc", 0)) * 100)
    except:
        return SAFE, 0.0, 0.0

def keepalive():
    try: get_account()
    except: pass

def smart_sleep(seconds):
    elapsed = 0
    while elapsed < seconds:
        chunk = min(480, seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk
        if elapsed < seconds: keepalive()

# ── NAITIK'S RSI (Wilders smoothing) ─────────────────────────

def calc_rsi_wilders(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    # Wilders smoothing = EWM with alpha = 1/period
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ── NAITIK'S SCORING ENGINE (exact match) ────────────────────

def score_assets():
    scores = {}
    try:
        spy_close = yf.download("SPY", period="220d", interval="1d", progress=False)["Close"].squeeze()
        spy_trend = bool(spy_close.iloc[-1] > spy_close.rolling(SPY_SMA_PERIOD).mean().iloc[-1])
    except:
        spy_trend = True

    for ticker in TICKERS:
        try:
            close = yf.download(ticker, period="120d", interval="1d", progress=False)["Close"].squeeze()
            if len(close) < ROC_SLOW + 5:
                continue

            # Exact Naitik lookback periods: 9, 21, 63
            fast  = float((close.iloc[-1] - close.iloc[-ROC_FAST-1]) / close.iloc[-ROC_FAST-1])
            med   = float((close.iloc[-1] - close.iloc[-ROC_MED-1])  / close.iloc[-ROC_MED-1])
            slow  = float((close.iloc[-1] - close.iloc[-ROC_SLOW-1]) / close.iloc[-ROC_SLOW-1])

            # Vol: std of daily returns over 21 days (Naitik uses self.std with 21 periods)
            vol   = float(close.pct_change().iloc[-VOL_PERIOD:].std())
            if vol == 0 or np.isnan(vol): vol = 1.0

            rsi   = float(calc_rsi_wilders(close, RSI_PERIOD).iloc[-1])
            sma50 = float(close.rolling(SMA_PERIOD).mean().iloc[-1])
            price = float(close.iloc[-1])

            weighted_mom = (fast * 0.5) + (med * 0.3) + (slow * 0.2)
            risk_adj_mom = weighted_mom / vol
            trend_score  = 1.0 if price > sma50 else 0.5
            rsi_penalty  = 0.9 if (rsi > 85 or rsi < 30) else 1.0
            scores[ticker] = risk_adj_mom * trend_score * rsi_penalty

        except Exception as e:
            print(f"Score error {ticker}: {e}")

    if not scores:
        return SAFE, 0, spy_trend, scores

    best_ticker = max(scores, key=scores.get)
    best_score  = scores[best_ticker]

    # Naitik's bear defense
    if not spy_trend and best_ticker != SAFE:
        uup_score = scores.get("UUP", -999)
        if uup_score > 0 and uup_score > scores.get(best_ticker, -999):
            best_ticker = "UUP"
            best_score  = uup_score
        elif scores.get(best_ticker, -999) < 0:
            best_ticker = SAFE
            best_score  = 0

    if best_score <= 0:
        best_ticker = SAFE

    return best_ticker, best_score, spy_trend, scores

# ── NAITIK'S VOL TARGETING (exact match) ─────────────────────
# lookback_vol = 20, target_vol = 0.80, annualized with sqrt(252)

def calc_target_weight(ticker):
    try:
        close    = yf.download(ticker, period="40d", interval="1d", progress=False)["Close"].squeeze()
        rets     = close.pct_change().dropna().iloc[-LOOKBACK_VOL:]
        curr_vol = float(np.std(rets) * np.sqrt(252))
        if curr_vol > 0:
            return min(1.0, TARGET_VOL / curr_vol)
    except:
        pass
    return 1.0

# ── EXECUTION ─────────────────────────────────────────────────

def execute_rotation(new_ticker, old_ticker, account_value):
    try:
        cancel_all_orders()
        time.sleep(1)
        liquidate_all()
        time.sleep(3)

        if new_ticker == SAFE:
            send_signal(
                f"🏦 Moving to cash\n"
                f"Sold: {TICKER_NAMES.get(old_ticker, old_ticker)}\n"
                f"Momentum deteriorated."
            )
            return

        live_price = get_live_price(new_ticker)
        if live_price is None:
            print(f"No live price for {new_ticker} -- aborting")
            return

        weight     = calc_target_weight(new_ticker)
        notional   = account_value * weight
        remainder  = account_value * (1.0 - weight)

        submit_order(new_ticker, "buy", notional=notional)

        # Naitik: if remainder > 10%, allocate to BIL
        if (1.0 - weight) > 0.1:
            bil_notional = account_value * (1.0 - weight)
            try:
                submit_order(SAFE, "buy", notional=bil_notional)
            except Exception as e:
                print(f"BIL order error: {e}")

        price_str  = f"${live_price:.2f}"
        old_name   = TICKER_NAMES.get(old_ticker, old_ticker)
        new_name   = TICKER_NAMES.get(new_ticker, new_ticker)

        send_signal(
            f"🔴 SELL {old_name}\n"
            f"🟢 BUY {new_name} at {price_str}\n\n"
            f"Momentum rotated. Make the switch."
        )

    except Exception as e:
        print(f"Rotation failed: {e}")

# ── MAIN CYCLE ────────────────────────────────────────────────

def run_cycle():
    global current_holding, daily_rebalanced, eod_summary_sent, daily_open_price

    et     = pytz.timezone("America/New_York")
    now_et = datetime.now(et)

    # Reset daily flags after close
    if now_et.hour >= 17:
        daily_rebalanced = False
        eod_summary_sent = False

    if not is_market_hours():
        symbol, _, _ = get_current_position()
        current_holding = symbol
        print(f"Market closed. Holding: {current_holding}")
        return

    # Reset at open
    if now_et.hour == 9 and now_et.minute < 45:
        daily_open_price = None

    symbol, pnl, pnl_pct = get_current_position()
    current_holding = symbol

    try:
        account       = get_account()
        portfolio_val = float(account.get("portfolio_value", INCEPTION_VALUE))
    except:
        portfolio_val = INCEPTION_VALUE

    # Capture today's open price for daily % calculation
    live_price = get_live_price(current_holding) if current_holding and current_holding != SAFE else None
    if daily_open_price is None and live_price and now_et.hour == 9 and now_et.minute >= 30:
        try:
            today_data = yf.download(current_holding, period="1d", interval="1m", progress=False)
            if not today_data.empty:
                daily_open_price = float(today_data["Open"].iloc[0])
        except:
            daily_open_price = live_price

    daily_pct = 0.0
    if daily_open_price and live_price and daily_open_price > 0:
        daily_pct = (live_price - daily_open_price) / daily_open_price * 100

    # ── NAITIK'S SCHEDULE: rebalance 5 minutes before close ──
    # Original: self.time_rules.before_market_close(symbol, 5)
    # = 3:55 PM ET

    is_rebalance_window = (now_et.hour == 15 and now_et.minute >= 52)

    if is_rebalance_window and not daily_rebalanced:
        print("Running Naitik's daily rebalance (5 min before close)...")
        best_ticker, best_score, spy_trend, scores = score_assets()
        daily_rebalanced = True

        current_score = scores.get(current_holding, -999)
        should_rotate = False

        if current_holding == SAFE:
            if best_score > 0.02:
                should_rotate = True
        elif current_holding is None:
            if best_score > 0:
                should_rotate = True
        else:
            if best_score > current_score * (1 + CONFIDENCE_THRESHOLD):
                should_rotate = True
            elif current_score < -0.02:
                best_ticker   = SAFE
                should_rotate = True

        name      = TICKER_NAMES.get(current_holding, current_holding)
        target    = TICKER_NAMES.get(best_ticker, best_ticker)
        eod_emoji = "📈" if daily_pct >= 0 else "📉"

        if should_rotate:
            execute_rotation(best_ticker, current_holding, portfolio_val)
            current_holding = best_ticker
            eod_msg = (
                f"{eod_emoji} {name}\n"
                f"Today: {daily_pct:+.2f}%\n\n"
                f"🔄 Rotated to {target} at close."
            )
        else:
            eod_msg = (
                f"{eod_emoji} {name}\n"
                f"Today: {daily_pct:+.2f}%\n\n"
                f"Tomorrow: HOLD"
            )

        send_signal(eod_msg)
        eod_summary_sent = True
        print(f"Rebalanced. Holding: {current_holding}")

    print(f"Cycle done. Holding: {current_holding} | {now_et.strftime('%H:%M')} ET")

# ── STARTUP ───────────────────────────────────────────────────

send_signal(
    f"✅ OmniscientBot running (Naitik Gupta v2.0.1)\n"
    f"Rebalances daily at 3:55 PM ET.\n"
    f"Daily update and rotation alerts at close.\n\n"
    f"Note: position shows as Cash when market is closed. "
    f"Your holding does not change overnight."
)

while True:
    try:
        run_cycle()
    except Exception as e:
        print(f"Error: {e}")
    smart_sleep(900)
