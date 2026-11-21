#!/usr/bin/env python3
"""
NORDVEX.AI - Telegram signal bot (BTCUSDT 5m)

Port of the NORDVEX.AI Pine indicator logic:
  Volume Profile (POC / VAH / VAL) + volume flow (delta from 1m candles)
  + EMA trend filter + volume filter  ->  BUY / SELL with SL, TP1, TP2.

- Signals fire only on CLOSED 5m candles (no repaint).
- Trade management (TP1 / TP2 / SL / BE) runs on closed 1m candles.
- State is saved to a JSON file so a restart does not lose the open trade.
- Data source: public exchange REST API (no API key needed).
"""
import os
import sys
import json
import math
import time
import logging
from datetime import datetime, timezone, timedelta

import requests

# ───────────────────────── CONFIG (env vars) ─────────────────────────
def _f(name, default):
    return float(os.getenv(name, str(default)))

def _i(name, default):
    return int(float(os.getenv(name, str(default))))

def _b(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SYMBOL = os.getenv("SYMBOL", "BTCUSDT").strip().upper()
EXCHANGE = os.getenv("EXCHANGE", "mexc").strip().lower()      # mexc | binance | bybit
POLL_SECONDS = _i("POLL_SECONDS", 10)
STATE_PATH = os.getenv("STATE_PATH", "state.json")

VP_LEN = _i("VP_LEN", 100)
VP_BINS = _i("VP_BINS", 24)
VA_PCT = _f("VA_PCT", 70)
BUB_LEN = _i("VOL_AVG_LEN", 50)
FLOW_LEN = _i("FLOW_LEN", 5)
FLOW_THR = _f("FLOW_THR", 0.08)
SIG_VOL = _f("MIN_VOL_MULT", 0.8)
USE_TREND = _b("USE_TREND", True)
TREND_LEN = _i("TREND_LEN", 100)
SL_LOOK = _i("SL_LOOK", 5)
SL_BUF = _f("SL_BUF_ATR", 0.2)
RR1 = _f("RR1", 1.0)
RR2 = _f("RR2", 2.0)
USE_BE = _b("MOVE_SL_TO_BE", True)
TP1_PCT = _f("TP1_CLOSE_PCT", 50)
CD_BARS = _i("COOLDOWN_BARS", 3)
MAX_SL_PER_DAY = _i("MAX_SL_PER_DAY", 2)      # 0 = off
COST_PCT = _f("COST_PCT", 0.05)               # round-trip fees+slippage, % of price (used in R stats)

TF_MIN = 5
TF_MS = TF_MIN * 60 * 1000
PKT = timezone(timedelta(hours=5))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nordvex")

st = None  # runtime state (dict)


# ───────────────────────── STATE ─────────────────────────
def default_state():
    return {
        "last_bar": 0,
        "last_mgmt": 0,
        "active": None,
        "last_close_bar": -1000,
        "offset": 0,
        "day": "",
        "sl_today": 0,
        "paused_notified": False,
        "last_price": None,
        "stats": {
            "closed": 0, "wins": 0, "tp2": 0, "be": 0, "sl": 0,
            "total_r": 0.0, "gross_w": 0.0, "gross_l": 0.0,
            "loss_streak": 0, "max_loss_streak": 0,
        },
    }

def load_state():
    s = default_state()
    try:
        with open(STATE_PATH, "r") as fh:
            saved = json.load(fh)
        for k, v in saved.items():
            if k == "stats":
                s["stats"].update(v)
            else:
                s[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("could not read state file: %s", e)
    return s

def save_state():
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(st, fh)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log.warning("could not save state: %s", e)


# ───────────────────────── TELEGRAM ─────────────────────────
def tg(method, **params):
    r = requests.post("https://api.telegram.org/bot%s/%s" % (TOKEN, method), json=params, timeout=20)
    return r.json()

def send(text):
    for _ in range(3):
        try:
            res = tg("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="HTML",
                     disable_web_page_preview=True)
            if res.get("ok"):
                return True
            log.warning("telegram error: %s", res)
        except Exception as e:
            log.warning("telegram exception: %s", e)
        time.sleep(2)
    return False

def fmt(x):
    return "{:,.2f}".format(x)

def pkt_time(ms):
    return datetime.fromtimestamp(ms / 1000.0, tz=PKT).strftime("%d %b %H:%M")


# ───────────────────────── DATA ─────────────────────────
HEADERS = {"User-Agent": "nordvex-bot/1.0"}

def fetch_klines(minutes, limit):
    """Returns CLOSED candles ascending: (open_time_ms, o, h, l, c, v)."""
    if EXCHANGE in ("mexc", "binance"):
        url = "https://api.mexc.com/api/v3/klines" if EXCHANGE == "mexc" \
            else "https://data-api.binance.vision/api/v3/klines"
        r = requests.get(url, params={"symbol": SYMBOL, "interval": "%dm" % minutes, "limit": limit},
                         headers=HEADERS, timeout=15)
        r.raise_for_status()
        out = [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]))
               for x in r.json()]
    elif EXCHANGE == "bybit":
        r = requests.get("https://api.bybit.com/v5/market/kline",
                         params={"category": "linear", "symbol": SYMBOL, "interval": str(minutes), "limit": limit},
                         headers=HEADERS, timeout=15)
        r.raise_for_status()
        rows = r.json()["result"]["list"]
        out = [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in rows]
    else:
        raise ValueError("EXCHANGE must be mexc, binance or bybit")
    out.sort(key=lambda k: k[0])
    now = int(time.time() * 1000)
    return [k for k in out if k[0] + minutes * 60000 <= now]


# ───────────────────────── INDICATORS ─────────────────────────
def ema_list(vals, n):
    k = 2.0 / (n + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out

def atr_list(c, n=14):
    trs = []
    for i, k in enumerate(c):
        if i == 0:
            tr = k[2] - k[3]
        else:
            pc = c[i - 1][4]
            tr = max(k[2] - k[3], abs(k[2] - pc), abs(k[3] - pc))
        trs.append(tr)
    out = [None] * len(c)
    if len(c) < n:
        return out
    a = sum(trs[:n]) / n
    out[n - 1] = a
    for i in range(n, len(c)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out

def build_deltas(c5, c1):
    """Delta per 5m bar from 1m candles (up-minute volume minus down-minute volume)."""
    sums, cnt = {}, {}
    for t, o, h, l, c, v in c1:
        b = t - t % TF_MS
        sums[b] = sums.get(b, 0.0) + (v if c >= o else -v)
        cnt[b] = cnt.get(b, 0) + 1
    out = []
    for t, o, h, l, c, v in c5:
        if cnt.get(t, 0) >= 4:
            out.append(sums[t])
        else:
            out.append(v * (2 * c - h - l) / (h - l) if h != l else 0.0)
    return out

def volume_profile(c5, i):
    if i < VP_LEN - 1:
        return None
    w = c5[i - VP_LEN + 1:i + 1]
    p_hi = max(k[2] for k in w)
    p_lo = min(k[3] for k in w)
    step = (p_hi - p_lo) / VP_BINS
    if step <= 0:
        return None
    vp = [0.0] * VP_BINS
    for k in w:
        hl2 = (k[2] + k[3]) / 2.0
        b = int(math.floor((hl2 - p_lo) / step))
        b = max(0, min(VP_BINS - 1, b))
        vp[b] += k[5]
    mx = max(vp)
    p_idx = vp.index(mx)
    tot = sum(vp)
    acc = mx
    lo_i = hi_i = p_idx
    while acc < tot * VA_PCT / 100.0 and (lo_i > 0 or hi_i < VP_BINS - 1):
        dn = vp[lo_i - 1] if lo_i > 0 else -1.0
        up = vp[hi_i + 1] if hi_i < VP_BINS - 1 else -1.0
        if up >= dn:
            hi_i += 1
            acc += up
        else:
            lo_i -= 1
            acc += dn
    poc = p_lo + (p_idx + 0.5) * step
    vah = p_lo + (hi_i + 1) * step
    val = p_lo + lo_i * step
    return poc, vah, val

def compute_signal(c5, d5, ema_t, atr, i):
    need = max(VP_LEN - 1, BUB_LEN - 1, SL_LOOK - 1, FLOW_LEN, 14)
    if i < need or atr[i] is None:
        return None
    t, o, h, l, c, v = c5[i]
    vol_avg = sum(k[5] for k in c5[i - BUB_LEN + 1:i + 1]) / BUB_LEN
    sv = sum(c5[k][5] for k in range(i - FLOW_LEN + 1, i + 1))
    sd = sum(d5[k] for k in range(i - FLOW_LEN + 1, i + 1))
    flow = sd / sv if sv > 0 else 0.0
    prof = volume_profile(c5, i)
    if prof is None:
        return None
    poc, vah, val = prof
    pc = c5[i - 1][4]
    vol_ok = v > vol_avg * SIG_VOL
    buy = (((l <= val and c > val) or (pc < poc and c > poc)) and c > o and flow > FLOW_THR
           and vol_ok and (not USE_TREND or c > ema_t[i]))
    sell = (((h >= vah and c < vah) or (pc > poc and c < poc)) and c < o and flow < -FLOW_THR
            and vol_ok and (not USE_TREND or c < ema_t[i]))
    if not (buy or sell):
        return None
    d = 1 if buy else -1
    window = c5[i - SL_LOOK + 1:i + 1]
    raw_sl = (min(k[3] for k in window) - atr[i] * SL_BUF) if d == 1 \
        else (max(k[2] for k in window) + atr[i] * SL_BUF)
    risk = max(abs(c - raw_sl), atr[i] * 0.6)
    return {
        "dir": d, "entry": c,
        "sl": c - d * risk,
        "tp1": c + d * risk * RR1,
        "tp2": c + d * risk * RR2,
        "risk": risk, "flow": flow, "poc": poc, "vah": vah, "val": val,
    }


# ───────────────────────── TRADES ─────────────────────────
def signal_msg(sig, bar_t):
    side = "BUY 🟢" if sig["dir"] == 1 else "SELL 🔴"
    pct = sig["risk"] / sig["entry"] * 100.0
    be_txt = "\nTP1 hit hone par SL ko entry par le aao." if USE_BE else ""
    tp1_txt = " (%d%%)" % TP1_PCT if TP1_PCT > 0 else ""
    return (
        "<b>NORDVEX.AI — %s</b>\n%s · %dm · %s PKT\n\n"
        "Entry: <code>%s</code>\n"
        "🛑 SL: <code>%s</code> (%.2f%%)\n"
        "🎯 BOOK TP1 HERE%s: <code>%s</code>\n"
        "🎯 BOOK TP2 HERE: <code>%s</code>\n\n"
        "Flow %+.2f · POC %s · VAH %s · VAL %s%s\n\n"
        "⚠️ Max 1%% risk per trade. Not financial advice."
    ) % (side, SYMBOL, TF_MIN, pkt_time(bar_t + TF_MS),
         fmt(sig["entry"]), fmt(sig["sl"]), pct, tp1_txt, fmt(sig["tp1"]), fmt(sig["tp2"]),
         sig["flow"], fmt(sig["poc"]), fmt(sig["vah"]), fmt(sig["val"]), be_txt)

def open_trade(sig, bar_t):
    tr = dict(sig)
    tr.update({"tp1_hit": False, "real_r": 0.0, "bar_t": bar_t, "start_ms": bar_t + TF_MS})
    st["active"] = tr
    st["last_mgmt"] = bar_t + TF_MS - 1
    send(signal_msg(sig, bar_t))
    save_state()

def stats_line():
    s = st["stats"]
    wr = (s["wins"] * 100.0 / s["closed"]) if s["closed"] else 0.0
    return "Total %+.2fR · %d trades · Win %.0f%%" % (s["total_r"], s["closed"], wr)

def finish(tr, r, kind, t):
    s = st["stats"]
    s["closed"] += 1
    s["total_r"] += r
    if kind == "TP2":
        s["tp2"] += 1
    elif kind == "BE":
        s["be"] += 1
    else:
        s["sl"] += 1
        st["sl_today"] += 1
    if r > 0:
        s["wins"] += 1
        s["gross_w"] += r
        s["loss_streak"] = 0
    else:
        s["gross_l"] += -r
        s["loss_streak"] += 1
        s["max_loss_streak"] = max(s["max_loss_streak"], s["loss_streak"])
    st["active"] = None
    st["last_close_bar"] = t // TF_MS
    side = "BUY" if tr["dir"] == 1 else "SELL"
    if kind == "TP2":
        head = "🏆 <b>TP2 HIT</b> — %s %s" % (SYMBOL, side)
    elif kind == "BE":
        head = "⚪ <b>BE EXIT (after TP1)</b> — %s %s" % (SYMBOL, side)
    else:
        head = "❌ <b>SL HIT</b> — %s %s" % (SYMBOL, side)
    send("%s\nResult: <b>%+.2fR</b>\n%s" % (head, r, stats_line()))
    save_state()

def manage_trade(c1):
    tr = st["active"]
    if not tr:
        return
    p = TP1_PCT / 100.0
    cost_r = tr["entry"] * COST_PCT / 100.0 / tr["risk"]
    for t, o, h, l, c, v in c1:
        if t <= st["last_mgmt"]:
            continue
        st["last_mgmt"] = t
        d = tr["dir"]
        hit_sl = (l <= tr["sl"]) if d == 1 else (h >= tr["sl"])
        hit_t1 = (h >= tr["tp1"]) if d == 1 else (l <= tr["tp1"])
        hit_t2 = (h >= tr["tp2"]) if d == 1 else (l <= tr["tp2"])
        if hit_sl:
            if tr["tp1_hit"] and USE_BE:
                finish(tr, tr["real_r"] - cost_r, "BE", t)
            elif tr["tp1_hit"]:
                finish(tr, tr["real_r"] - (1.0 - p) - cost_r, "SL", t)
            else:
                finish(tr, -1.0 - cost_r, "SL", t)
            return
        if hit_t2:
            if not tr["tp1_hit"]:
                tr["real_r"] += p * RR1
                tr["tp1_hit"] = True
            finish(tr, tr["real_r"] + (1.0 - p) * RR2 - cost_r, "TP2", t)
            return
        if hit_t1 and not tr["tp1_hit"]:
            tr["tp1_hit"] = True
            tr["real_r"] += p * RR1
            side = "BUY" if d == 1 else "SELL"
            extra = "\nSL ab entry par: <code>%s</code>" % fmt(tr["entry"]) if USE_BE else ""
            if USE_BE:
                tr["sl"] = tr["entry"]
            send("✅ <b>TP1 HIT</b> — %s %s\nBook %d%% here: <code>%s</code>%s" %
                 (SYMBOL, side, TP1_PCT, fmt(tr["tp1"]), extra))
            save_state()

def process_new_bars(c5, d5, ema_t, atr):
    if not c5:
        return
    if st["last_bar"] == 0:
        st["last_bar"] = c5[-1][0]
        save_state()
        send("🤖 <b>NORDVEX.AI bot started</b>\n%s · %dm · %s\nNaye signals closed 5m candle par aayenge." %
             (SYMBOL, TF_MIN, EXCHANGE.upper()))
        return
    now = int(time.time() * 1000)
    for i, b in enumerate(c5):
        t = b[0]
        if t <= st["last_bar"]:
            continue
        st["last_bar"] = t
        if now - (t + TF_MS) > 2 * TF_MS:
            continue                                    # stale bar (bot was offline)
        if st["active"]:
            continue
        if MAX_SL_PER_DAY > 0 and st["sl_today"] >= MAX_SL_PER_DAY:
            if not st["paused_notified"]:
                st["paused_notified"] = True
                send("⏸ Daily SL limit (%d) hit. Aaj ke liye signals band." % MAX_SL_PER_DAY)
            continue
        if t // TF_MS - st["last_close_bar"] < CD_BARS:
            continue
        sig = compute_signal(c5, d5, ema_t, atr, i)
        if sig:
            open_trade(sig, t)
    save_state()


# ───────────────────────── COMMANDS ─────────────────────────
def status_text():
    px = st.get("last_price")
    lines = ["<b>NORDVEX.AI</b> · %s %dm" % (SYMBOL, TF_MIN)]
    if px:
        lines.append("Price: <code>%s</code>" % fmt(px))
    tr = st["active"]
    if tr:
        side = "BUY" if tr["dir"] == 1 else "SELL"
        lines.append("Active: <b>%s</b> @ <code>%s</code>" % (side, fmt(tr["entry"])))
        lines.append("SL <code>%s</code> · TP1 <code>%s</code>%s · TP2 <code>%s</code>" %
                     (fmt(tr["sl"]), fmt(tr["tp1"]), " ✔" if tr["tp1_hit"] else "", fmt(tr["tp2"])))
        if px:
            r_now = (px - tr["entry"]) * tr["dir"] / tr["risk"]
            lines.append("Open P/L: %+.2fR" % r_now)
    else:
        lines.append("Active trade: none (waiting)")
    if MAX_SL_PER_DAY > 0:
        lines.append("SL today: %d / %d" % (st["sl_today"], MAX_SL_PER_DAY))
    return "\n".join(lines)

def stats_text():
    s = st["stats"]
    pf = (s["gross_w"] / s["gross_l"]) if s["gross_l"] > 0 else None
    exp = (s["total_r"] / s["closed"]) if s["closed"] else 0.0
    return ("<b>NORDVEX.AI stats</b> (since bot start)\n"
            "Closed: %d · Win rate: %.0f%%\n"
            "Total: <b>%+.2fR</b> · Expectancy: %+.2fR/trade\n"
            "Profit factor: %s\n"
            "TP2 / BE / SL: %d / %d / %d\n"
            "Max loss streak: %d") % (
        s["closed"], (s["wins"] * 100.0 / s["closed"]) if s["closed"] else 0.0,
        s["total_r"], exp, ("%.2f" % pf) if pf else "-",
        s["tp2"], s["be"], s["sl"], s["max_loss_streak"])

def handle_commands():
    res = tg("getUpdates", offset=st["offset"], timeout=0)
    for u in res.get("result", []):
        st["offset"] = u["update_id"] + 1
        m = u.get("message") or u.get("channel_post") or {}
        if str(m.get("chat", {}).get("id")) != CHAT_ID:
            continue
        text = (m.get("text") or "").strip().lower()
        if text.startswith("/status"):
            send(status_text())
        elif text.startswith("/stats"):
            send(stats_text())
        elif text.startswith("/start") or text.startswith("/help"):
            send("Commands:\n/status - live price + active trade\n/stats - performance in R")
    save_state()


# ───────────────────────── MAIN LOOP ─────────────────────────
def roll_day():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st["day"] != today:
        st["day"] = today
        st["sl_today"] = 0
        st["paused_notified"] = False

def tick():
    c5 = fetch_klines(TF_MIN, 400)
    c1 = fetch_klines(1, 500)
    if not c5 or not c1:
        return
    roll_day()
    st["last_price"] = c1[-1][4]
    ema_t = ema_list([k[4] for k in c5], TREND_LEN)
    atr = atr_list(c5)
    d5 = build_deltas(c5, c1)
    manage_trade(c1)
    process_new_bars(c5, d5, ema_t, atr)
    handle_commands()
    save_state()

def main():
    global st
    if not TOKEN or not CHAT_ID:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.")
        sys.exit(1)
    st = load_state()
    log.info("NORDVEX bot running: %s %dm on %s", SYMBOL, TF_MIN, EXCHANGE)
    while True:
        try:
            tick()
        except Exception:
            log.exception("loop error")
            time.sleep(15)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
