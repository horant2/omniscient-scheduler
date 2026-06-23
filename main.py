import os
import time
import requests
import yfinance as yf
import numpy as np
from datetime import datetime
import pytz

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")

# ── NAITIK GUPTA (quantconnect.com/strategies/46/TheOmniscientParadox) ───
TICKERS              = ["SOXL","TECL","TQQQ","FAS","ERX","UUP","TMF"]
SAFE                 = "BIL"
CONFIDENCE_THRESHOLD = 0.10
ROC_FAST             = 9
ROC_MED              = 21
ROC_SLOW             = 63
VOL_PERIOD           = 21
RSI_PERIOD           = 14
SMA_PERIOD           = 50
SPY_SMA_PERIOD       = 200

TICKER_NAMES = {
    "SOXL": "SOXL (3x Semiconductors)",
    "TECL": "TECL (3x Tech)",
    "TQQQ": "TQQQ (3x Nasdaq)",
    "FAS":  "FAS (3x Financials)",
    "ERX":  "ERX (2x Energy)",
    "UUP":  "UUP (US Dollar)",
    "TMF":  "TMF (3x Long Bonds)",
    "BIL":  "Cash (BIL)"
}

daily_signal_sent = False

def send_signal(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in [message[i:i+4000] for i in range(0, len(message), 4000)]:
        try:
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk}, timeout=10)
        except Exception as e:
            print(f"Telegram error: {e}")
        time.sleep(1)

def is_market_hours():
    et  = pytz.timezone("America/New_York")
    now = datetime.now(et)
    if now.weekday() >= 5: return False
    return (now.replace(hour=9,  minute=30, second=0, microsecond=0) <= now <=
            now.replace(hour=16, minute=0,  second=0, microsecond=0))

def smart_sleep(seconds):
    elapsed = 0
    while elapsed < seconds:
        time.sleep(min(480, seconds - elapsed))
        elapsed += min(480, seconds - elapsed)

def calc_rsi_wilders(series, period=14):
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

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
            if len(close) < ROC_SLOW + 5: continue
            fast  = float((close.iloc[-1] - close.iloc[-ROC_FAST-1]) / close.iloc[-ROC_FAST-1])
            med   = float((close.iloc[-1] - close.iloc[-ROC_MED-1])  / close.iloc[-ROC_MED-1])
            slow  = float((close.iloc[-1] - close.iloc[-ROC_SLOW-1]) / close.iloc[-ROC_SLOW-1])
            vol   = float(close.pct_change().iloc[-VOL_PERIOD:].std())
            if vol == 0 or np.isnan(vol): vol = 1.0
            rsi   = float(calc_rsi_wilders(close, RSI_PERIOD).iloc[-1])
            sma50 = float(close.rolling(SMA_PERIOD).mean().iloc[-1])
            price = float(close.iloc[-1])
            wmom  = (fast * 0.5) + (med * 0.3) + (slow * 0.2)
            radj  = wmom / vol
            trend = 1.0 if price > sma50 else 0.5
            pen   = 0.9 if (rsi > 85 or rsi < 30) else 1.0
            scores[ticker] = radj * trend * pen
        except Exception as e:
            print(f"Score error {ticker}: {e}")

    if not scores:
        return SAFE, 0, spy_trend, scores

    best_ticker = max(scores, key=scores.get)
    best_score  = scores[best_ticker]

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

def run_cycle():
    global daily_signal_sent

    et     = pytz.timezone("America/New_York")
    now_et = datetime.now(et)

    if now_et.hour >= 17:
        daily_signal_sent = False

    if not is_market_hours():
        print(f"Market closed. {now_et.strftime('%H:%M')} ET")
        return

    # 3:00 PM ET = 2:00 PM Central = one hour before close
    is_signal_window = (now_et.hour == 15 and 0 <= now_et.minute <= 7)

    if is_signal_window and not daily_signal_sent:
        print("Running daily signal...")
        daily_signal_sent = True

        best_ticker, best_score, spy_trend, scores = score_assets()

        # Today's return of the top asset
        daily_pct = 0.0
        price_str = ""
        if best_ticker != SAFE:
            try:
                fi             = yf.Ticker(best_ticker).fast_info
                last_price     = float(fi.last_price)
                previous_close = float(fi.previous_close)
                if previous_close > 0:
                    daily_pct = (last_price - previous_close) / previous_close * 100
                price_str = f" at ${last_price:.2f}"
            except Exception as e:
                print(f"Price error: {e}")

        name  = TICKER_NAMES.get(best_ticker, best_ticker)
        emoji = "📈" if daily_pct >= 0 else "📉"
        trend_str = "BULL" if spy_trend else "BEAR"

        msg = (
            f"{emoji} HOLD {name}\n"
            f"Today: {daily_pct:+.2f}%\n"
            f"Market: {trend_str}"
        ) if best_ticker != SAFE else (
            f"🏦 Move to Cash (BIL)\n"
            f"Momentum negative. Stay out."
        )

        send_signal(msg)
        print(f"Signal sent: {best_ticker}")

    print(f"Cycle. {now_et.strftime('%H:%M')} ET")

send_signal("✅ OmniscientBot live. Daily signal at 2:00 PM Central / 3:00 PM Eastern.")

while True:
    try:
        run_cycle()
    except Exception as e:
        print(f"Error: {e}")
    smart_sleep(900)
