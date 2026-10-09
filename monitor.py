import os
import time
import requests
import pandas as pd
import yfinance as yf
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta

# ============================================================
# MNQ MARKET WATCH
# MNQ MOMENTUM 5M
# LONG + SHORT
# 5-minute CONFIRMED candles
# ============================================================

TZ = ZoneInfo("America/Chicago")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError(
        "Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID environment variable."
    )

# ============================================================
# STRATEGY SETTINGS
# Matches TradingView MNQ MOMENTUM 5M baseline
# ============================================================

SYMBOL = "MNQ=F"
INTERVAL = "5m"
PERIOD = "5d"

ROC_LEN = 12
ATR_LEN = 14

SL_MULT = 4.0
TP_MULT = 0.75

MAX_HOLD_BARS = 24

ENTRY_START_HOUR = 8
ENTRY_START_MINUTE = 30

ENTRY_END_HOUR = 10
ENTRY_END_MINUTE = 0

EOD_START_HOUR = 14
EOD_START_MINUTE = 45

EOD_END_HOUR = 15
EOD_END_MINUTE = 0

POLL_SECONDS = 20

# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    try:
        response = requests.post(
            url,
            data=payload,
            timeout=15
        )

        response.raise_for_status()

        print("TELEGRAM:", message)

    except Exception as e:
        print("Telegram error:", e)


# ============================================================
# INDICATORS
# ============================================================

def true_range(df):

    previous_close = df["Close"].shift(1)

    ranges = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - previous_close).abs(),
            (df["Low"] - previous_close).abs()
        ],
        axis=1
    )

    return ranges.max(axis=1)


def rma(series, length):

    values = series.astype(float).copy()

    result = pd.Series(
        index=values.index,
        dtype=float
    )

    if len(values) < length:
        return result

    first = values.iloc[:length].mean()

    result.iloc[length - 1] = first

    alpha = 1.0 / length

    for i in range(length, len(values)):
        result.iloc[i] = (
            result.iloc[i - 1]
            + alpha * (values.iloc[i] - result.iloc[i - 1])
        )

    return result


def calculate_indicators(df):

    df = df.copy()

    df["TR"] = true_range(df)

    df["ATR"] = rma(
        df["TR"],
        ATR_LEN
    )

    df["ROC"] = (
        df["Close"]
        - df["Close"].shift(ROC_LEN)
    )

    return df


# ============================================================
# MARKET DATA
# ============================================================

def download_data():

    df = yf.download(
        SYMBOL,
        period=PERIOD,
        interval=INTERVAL,
        auto_adjust=False,
        progress=False,
        prepost=False
    )

    if df.empty:
        return df

    # yfinance can return MultiIndex columns
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")

    df.index = df.index.tz_convert(TZ)

    df = df.sort_index()

    return df


# ============================================================
# CONFIRMED BAR CHECK
# ============================================================

def confirmed_data(df):

    if df.empty:
        return df

    now = datetime.now(TZ)

    confirmed_rows = []

    for timestamp in df.index:

        bar_close = timestamp + timedelta(minutes=5)

        if bar_close <= now:
            confirmed_rows.append(timestamp)

    if not confirmed_rows:
        return df.iloc[0:0]

    return df.loc[confirmed_rows]


# ============================================================
# SESSION HELPERS
# ============================================================

def minutes_from_midnight(timestamp):
    return timestamp.hour * 60 + timestamp.minute


def in_entry_session(timestamp):

    current = minutes_from_midnight(timestamp)

    start = (
        ENTRY_START_HOUR * 60
        + ENTRY_START_MINUTE
    )

    end = (
        ENTRY_END_HOUR * 60
        + ENTRY_END_MINUTE
    )

    # Pine input.session("0830-1000")
    # includes bars beginning at 08:30 through 09:55
    return start <= current < end


def in_eod_session(timestamp):

    current = minutes_from_midnight(timestamp)

    start = (
        EOD_START_HOUR * 60
        + EOD_START_MINUTE
    )

    end = (
        EOD_END_HOUR * 60
        + EOD_END_MINUTE
    )

    return start <= current < end


# ============================================================
# STATE
# ============================================================

position = 0

entry_bar_number = None
entry_price = None

stop_price = None
target_price = None

last_processed_bar = None


# ============================================================
# PROCESS NEW BAR
# ============================================================

def process_bar(df):

    global position
    global entry_bar_number
    global entry_price
    global stop_price
    global target_price
    global last_processed_bar

    if len(df) < 220:
        return

    current_bar = len(df) - 1

    row = df.iloc[current_bar]
    previous = df.iloc[current_bar - 1]

    timestamp = df.index[current_bar]

    if last_processed_bar == timestamp:
        return

    last_processed_bar = timestamp

    close = float(row["Close"])
    high = float(row["High"])
    low = float(row["Low"])

    atr = float(row["ATR"])
    roc = float(row["ROC"])
    previous_roc = float(previous["ROC"])

    if pd.isna(atr) or pd.isna(roc) or pd.isna(previous_roc):
        return

    print(
        timestamp,
        "Close:",
        round(close, 2),
        "ROC:",
        round(roc, 2),
        "ATR:",
        round(atr, 2),
        "Position:",
        position
    )

    # ========================================================
    # MANAGE OPEN LONG
    # ========================================================

    if position == 1:

        # Conservative same-bar assumption:
        # stop is checked before target if both were touched.

        if low <= stop_price:

            send_telegram(
                "🔴 MNQ MOM | EXIT LONG - STOP\n"
                f"Price: {stop_price:.2f}\n"
                f"Time: {timestamp.strftime('%Y-%m-%d %H:%M')} CT"
            )

            position = 0
            entry_bar_number = None
            entry_price = None
            stop_price = None
            target_price = None

            return

        if high >= target_price:

            send_telegram(
                "🟢 MNQ MOM | EXIT LONG - TARGET\n"
                f"Price: {target_price:.2f}\n"
                f"Time: {timestamp.strftime('%Y-%m-%d %H:%M')} CT"
            )

            position = 0
            entry_bar_number = None
            entry_price = None
            stop_price = None
            target_price = None

            return

    # ========================================================
    # MANAGE OPEN SHORT
    # ========================================================

    elif position == -1:

        if high >= stop_price:

            send_telegram(
                "🔴 MNQ MOM | EXIT SHORT - STOP\n"
                f"Price: {stop_price:.2f}\n"
                f"Time: {timestamp.strftime('%Y-%m-%d %H:%M')} CT"
            )

            position = 0
            entry_bar_number = None
            entry_price = None
            stop_price = None
            target_price = None

            return

        if low <= target_price:

            send_telegram(
                "🟢 MNQ MOM | EXIT SHORT - TARGET\n"
                f"Price: {target_price:.2f}\n"
                f"Time: {timestamp.strftime('%Y-%m-%d %H:%M')} CT"
            )

            position = 0
            entry_bar_number = None
            entry_price = None
            stop_price = None
            target_price = None

            return

    # ========================================================
    # TIME EXIT
    # ========================================================

    if (
        position != 0
        and entry_bar_number is not None
        and current_bar >= entry_bar_number + MAX_HOLD_BARS
    ):

        side = "LONG" if position == 1 else "SHORT"

        send_telegram(
            f"🟡 MNQ MOM | TIME EXIT {side}\n"
            f"Price: {close:.2f}\n"
            f"Time: {timestamp.strftime('%Y-%m-%d %H:%M')} CT"
        )

        position = 0
        entry_bar_number = None
        entry_price = None
        stop_price = None
        target_price = None

        return

    # ========================================================
    # END-OF-DAY EXIT
    # ========================================================

    if position != 0 and in_eod_session(timestamp):

        side = "LONG" if position == 1 else "SHORT"

        send_telegram(
            f"🟠 MNQ MOM | EOD FLAT {side}\n"
            f"Price: {close:.2f}\n"
            f"Time: {timestamp.strftime('%Y-%m-%d %H:%M')} CT"
        )

        position = 0
        entry_bar_number = None
        entry_price = None
        stop_price = None
        target_price = None

        return

    # ========================================================
    # ENTRY CONDITIONS
    # ========================================================

    can_trade = (
        position == 0
        and in_entry_session(timestamp)
        and current_bar >= 210
    )

    if not can_trade:
        return

    long_signal = (
        roc > 0
        and previous_roc <= 0
    )

    short_signal = (
        roc < 0
        and previous_roc >= 0
    )

    # ========================================================
    # LONG
    # ========================================================

    if long_signal:

        position = 1

        entry_bar_number = current_bar
        entry_price = close

        stop_price = (
            close
            - atr * SL_MULT
        )

        target_price = (
            close
            + atr * TP_MULT
        )

        send_telegram(
            "🟢 MNQ MOM | BUY LONG\n"
            f"Entry: {entry_price:.2f}\n"
            f"Target: {target_price:.2f}\n"
            f"Stop: {stop_price:.2f}\n"
            f"Time: {timestamp.strftime('%Y-%m-%d %H:%M')} CT"
        )

        return

    # ========================================================
    # SHORT
    # ========================================================

    if short_signal:

        position = -1

        entry_bar_number = current_bar
        entry_price = close

        stop_price = (
            close
            + atr * SL_MULT
        )

        target_price = (
            close
            - atr * TP_MULT
        )

        send_telegram(
            "🔴 MNQ MOM | SELL SHORT\n"
            f"Entry: {entry_price:.2f}\n"
            f"Target: {target_price:.2f}\n"
            f"Stop: {stop_price:.2f}\n"
            f"Time: {timestamp.strftime('%Y-%m-%d %H:%M')} CT"
        )


# ============================================================
# STARTUP
# ============================================================

send_telegram(
    "🟢 MNQ MARKET WATCH ONLINE\n\n"
    "Strategy: MNQ MOMENTUM 5M\n"
    "LONG + SHORT\n"
    "Timeframe: 5 MIN\n"
    "Entry window: 08:30-10:00 CT\n"
    "Momentum: 12 bars\n"
    "ATR: 14\n"
    "SL: 4.0 ATR\n"
    "TP: 0.75 ATR\n"
    "Max hold: 24 bars"
)

print("MNQ MOMENTUM 5M MONITOR ONLINE")


# ============================================================
# MAIN LOOP
# ============================================================

while True:

    try:

        data = download_data()

        if not data.empty:

            data = confirmed_data(data)

            if not data.empty:

                data = calculate_indicators(data)

                process_bar(data)

    except Exception as e:

        print("Monitor error:", e)

    time.sleep(POLL_SECONDS)
