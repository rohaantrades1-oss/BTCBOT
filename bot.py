#!/usr/bin/env python3
"""
NORDVEX.AI - Telegram signal bot (BTCUSDT 5m)  v1.1

Port of the NORDVEX.AI Pine indicator logic:
  Volume Profile (POC / VAH / VAL) + volume flow (delta from 1m candles)
  + EMA trend filter + volume filter  ->  BUY / SELL with SL, TP1, TP2.
Alerts also show Volume Flow, HTF Order Blocks (1H/4H) and a confluence grade.

- Signals fire only on CLOSED 5m candles (no repaint).
- Trade management (TP1 / TP2 / SL / BE) runs on closed 1m candles.
- Messages that fail to send are kept in an outbox and retried (nothing is lost).
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
MAX_SL_PER_DAY = _i("MAX_SL_PER_DAY", 0)      # 0 = off (every signal is sent)
COST_PCT = _f("COST_PCT", 0.05)               # round-trip fees+slippage, % of price (used in R stats)

OB_IMPULSE = _f("OB_IMPULSE", 1.2)
OB_TFS = [int(x) for x in os.getenv("OB_TFS", "60,240").split(",") if x.strip()]
HEARTBEAT_HOURS = _f("HEARTBEAT_HOURS", 6)    # 0 = off
SHOW_SKIPPED = _b("SHOW_SKIPPED", True)       # tell me when a signal is NOT taken and why

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
        "last_flow": None,
        "last_hb": 0,
        "outbox": [],
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

def _try_send(text):
    for _ in range(2):
        try:
            res = tg("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="HTML",
                     disable_web_page_preview=True)
            if res.get("ok"):
                return True
            log.warning("telegram error: %s", res)
        except Exception as e:
            log.warning("telegram exception: %s", e)
        time.sleep(1)
    return False

def flush_outbox():
    """Send queued messages in order. A failed message stays queued and is retried next tick."""
    while st["outbox"]:
        ts, text = st["outbox"][0]
        age_min = (int(time.time() * 1000) - ts) / 60000.0
        body = text if age_min < 2 else "⏱ Delayed %d min\n%s" % (age_min, text)
        if not _try_send(body):
            return False
        st["outbox"].pop(0)
    return True

def send(text):
    st["outbox"].append([int(time.time() * 1000), text])
    del st["outbox"][:-50]
    save_state()
    ok = flush_outbox()
    save_state()
    return ok

def fmt(x):
    return "{:,.2f}".format(x)

def pkt_time(ms):
    return datetime.fromtimestamp(ms / 1000.0, tz=PKT).strftime("%d %b %H:%M")

def tf_label(m):
    if m == 60:
        return "1H"
    if m % 60 == 0:
        return "%dH" % (m // 60)
    return "%dm" % m


# ───────────────────────── DATA ─────────────────────────
HEADERS = {"User-Agent": "nordvex-bot/1.1"}

def _interval(minutes):
    if EXCHANGE == "mexc":
        return {1: "1m", 5: "5m", 15: "15m", 30: "30m", 60: "60m", 240: "4h", 1440: "1d"}[minutes]
    if EXCHANGE == "binance":
        return {1: "1m", 5: "5m", 15: "15m", 30: "30m", 60: "1h", 240: "4h", 1440: "1d"}[minutes]
    return str(minutes)

def fetch_klines(minutes, limit):
    """Returns CLOSED candles ascending: (open_time_ms, o, h, l, c, v)."""
    if EXCHANGE in ("mexc", "binance"):
        url = "https://api.mexc.com/api/v3/klines" if EXCHANGE == "mexc" \
            else "https://data-api.binance.vision/api/v3/klines"
        r = requests.get(url, params={"symbol": SYMBOL, "interval": _interval(minutes), "limit": limit},
                         headers=HEADERS, timeout=15)
        r.raise_for_status()
        out = [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]))
               for x in r.json()]
    elif EXCHANGE == "bybit":
        r = requests.get("https://api.bybit.com/v5/market/kline",
                         params={"category": "linear", "symbol": SYMBOL, "interval": _interval(minutes), "limit": limit},
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
        "vol_x": (v / vol_avg) if vol_avg > 0 else 0.0,
        "bar_delta": d5[i],
    }


# ───────────────────────── ORDER BLOCKS (HTF) ─────────────────────────
_ob_cache = {"t": 0.0, "obs": []}

def find_obs(ch, minutes):
    """Same rule as the Pine indicator, on CLOSED higher-timeframe candles.
    Bull OB: last down candle before an up-impulse that closes above its high.
    Bear OB: last up candle before a down-impulse that closes below its low."""
    atr = atr_list(ch)
    lab = tf_label(minutes)
    found = []
    for j in range(16, len(ch)):
        a = atr[j - 1]
        if a is None:
            continue
        o1, c1 = ch[j - 1][1], ch[j - 1][4]
        t2, o2, h2, l2, c2 = ch[j - 2][0], ch[j - 2][1], ch[j - 2][2], ch[j - 2][3], ch[j - 2][4]
        if c1 > h2 and (c1 - o1) > a * OB_IMPULSE and c2 < o2:
            found.append({"dir": 1, "top": h2, "bot": l2, "t": t2, "j": j, "tf": lab})
        if c1 < l2 and (o1 - c1) > a * OB_IMPULSE and c2 > o2:
            found.append({"dir": -1, "top": h2, "bot": l2, "t": t2, "j": j, "tf": lab})
    alive = []
    for ob in found:
        mitigated = False
        for k in range(ob["j"], len(ch)):
            cl = ch[k][4]
            if (ob["dir"] == 1 and cl < ob["bot"]) or (ob["dir"] == -1 and cl > ob["top"]):
                mitigated = True
                break
        if not mitigated:
            alive.append(ob)
    return alive

def get_obs():
    now = time.time()
    if now - _ob_cache["t"] < 120:
        return _ob_cache["obs"]
    obs = []
    ok = False
    for m in OB_TFS:
        try:
            ch = fetch_klines(m, 300 if m <= 60 else 200)
            obs += find_obs(ch, m)
            ok = True
        except Exception as e:
            log.warning("OB fetch failed (%sm): %s", m, e)
    if ok or not _ob_cache["obs"]:
        _ob_cache["obs"] = obs
    _ob_cache["t"] = now
    return _ob_cache["obs"]

def ob_lines(price, d=None):
    obs = [ob for ob in get_obs()
           if (ob["dir"] == 1 and price >= ob["bot"]) or (ob["dir"] == -1 and price <= ob["top"])]
    inside = [ob for ob in obs if ob["bot"] <= price <= ob["top"]]
    sup = sorted([ob for ob in obs if ob["dir"] == 1 and ob["top"] < price], key=lambda o: price - o["top"])
    res = sorted([ob for ob in obs if ob["dir"] == -1 and ob["bot"] > price], key=lambda o: o["bot"] - price)
    lines = []
    for ob in inside[:2]:
        lines.append("Inside %s OB %s: %s – %s" % ("Bull 🟢" if ob["dir"] == 1 else "Bear 🔴", ob["tf"],
                                                  fmt(ob["bot"]), fmt(ob["top"])))
    if sup:
        ob = sup[0]
        lines.append("↓ Support: Bull OB %s %s – %s (%+.2f%%)" %
                     (ob["tf"], fmt(ob["bot"]), fmt(ob["top"]), (ob["top"] - price) / price * 100.0))
    if res:
        ob = res[0]
        lines.append("↑ Resistance: Bear OB %s %s – %s (%+.2f%%)" %
                     (ob["tf"], fmt(ob["bot"]), fmt(ob["top"]), (ob["bot"] - price) / price * 100.0))
    if not lines:
        lines.append("No active OB nearby")
    same = d is not None and any(ob["dir"] == d for ob in inside)
    return lines, same


# ───────────────────────── TRADES ─────────────────────────
def flow_label(flow):
    if flow > FLOW_THR:
        return "BUY FLOW"
    if flow < -FLOW_THR:
        return "SELL FLOW"
    return "NEUTRAL"

def signal_msg(sig, bar_t):
    d = sig["dir"]
    side = "BUY 🟢" if d == 1 else "SELL 🔴"
    pct = sig["risk"] / sig["entry"] * 100.0
    obl, inside_same = ob_lines(sig["entry"], d)
    flow_strong = abs(sig["flow"]) >= 2 * FLOW_THR
    vol_spike = sig["vol_x"] >= 1.5
    score = int(flow_strong) + int(vol_spike) + int(inside_same)
    grade = {3: "A+", 2: "A", 1: "B", 0: "C"}[score]
    sig["grade"] = grade
    be_txt = "\nTP1 hit hone par SL ko entry par le aao." if USE_BE else ""
    tp1_txt = " (%d%%)" % TP1_PCT if TP1_PCT > 0 else ""
    tick = lambda ok: "✔" if ok else "✘"
    return (
        "<b>NORDVEX.AI — %s  [%s]</b>\n%s · %dm · %s PKT\n\n"
        "Entry: <code>%s</code>\n"
        "🛑 SL: <code>%s</code> (%.2f%%)\n"
        "🎯 BOOK TP1 HERE%s: <code>%s</code>\n"
        "🎯 BOOK TP2 HERE: <code>%s</code>\n\n"
        "📊 <b>Volume Flow:</b> %s %+.2f%s · Vol %.1fx avg · Δ %+.1f\n"
        "Profile: POC %s · VAH %s · VAL %s\n"
        "🧱 <b>Order Blocks:</b>\n%s\n"
        "Confluence: Flow %s · Volume %s · OB %s → %d/3%s\n\n"
        "⚠️ Max 1%% risk per trade. Not financial advice."
    ) % (side, grade, SYMBOL, TF_MIN, pkt_time(bar_t + TF_MS),
         fmt(sig["entry"]), fmt(sig["sl"]), pct, tp1_txt, fmt(sig["tp1"]), fmt(sig["tp2"]),
         flow_label(sig["flow"]), sig["flow"], " (strong)" if flow_strong else "",
         sig["vol_x"], sig["bar_delta"],
         fmt(sig["poc"]), fmt(sig["vah"]), fmt(sig["val"]),
         "\n".join(obl),
         tick(flow_strong), tick(vol_spike), tick(inside_same), score, be_txt)

def open_trade(sig, bar_t):
    msg = signal_msg(sig, bar_t)          # also sets sig["grade"]
    tr = dict(sig)
    tr.update({"tp1_hit": False, "real_r": 0.0, "bar_t": bar_t, "start_ms": bar_t + TF_MS})
    st["active"] = tr
    st["last_mgmt"] = bar_t + TF_MS - 1
    save_state()
    send(msg)

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
    save_state()
    send("%s\nResult: <b>%+.2fR</b>\n%s" % (head, r, stats_line()))

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
            extra = ("\nSL ab entry par: <code>%s</code>" % fmt(tr["entry"])) if USE_BE else ""
            if USE_BE:
                tr["sl"] = tr["entry"]
            save_state()
            send("✅ <b>TP1 HIT</b> — %s %s\nBook %d%% here: <code>%s</code>%s" %
                 (SYMBOL, side, TP1_PCT, fmt(tr["tp1"]), extra))

def process_new_bars(c5, d5, ema_t, atr):
    if not c5:
        return
    if st["last_bar"] == 0:
        st["last_bar"] = c5[-1][0]
        save_state()
        send("🤖 <b>NORDVEX.AI bot started</b>\n%s · %dm · %s\nHar signal closed 5m candle par aayega (volume flow + order blocks ke saath)." %
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
        try:
            sig = compute_signal(c5, d5, ema_t, atr, i)
            if not sig:
                continue
            side = "BUY" if sig["dir"] == 1 else "SELL"
            reason = None
            if st["active"]:
                reason = "trade already active"
            elif MAX_SL_PER_DAY > 0 and st["sl_today"] >= MAX_SL_PER_DAY:
                reason = "daily SL limit (%d) reached" % MAX_SL_PER_DAY
            elif t // TF_MS - st["last_close_bar"] < CD_BARS:
                reason = "cooldown after last trade"
            if reason:
                if SHOW_SKIPPED:
                    send("⏭ Signal skipped (%s): %s %s @ <code>%s</code>" % (reason, SYMBOL, side, fmt(sig["entry"])))
                continue
            open_trade(sig, t)
        except Exception:
            log.exception("signal error")
            send("⚠️ Bot error while checking the %s PKT candle (logged). Bot is still running." % pkt_time(t + TF_MS))
    save_state()


# ───────────────────────── COMMANDS ─────────────────────────
def status_text():
    px = st.get("last_price")
    lines = ["<b>NORDVEX.AI</b> · %s %dm" % (SYMBOL, TF_MIN)]
    if px:
        lines.append("Price: <code>%s</code>" % fmt(px))
    fl = st.get("last_flow")
    if fl is not None:
        lines.append("Volume Flow: %s %+.2f" % (flow_label(fl), fl))
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
    if px:
        obl, _ = ob_lines(px)
        lines.append("🧱 Order Blocks:")
        lines.extend(obl)
    if MAX_SL_PER_DAY > 0:
        lines.append("SL today: %d / %d" % (st["sl_today"], MAX_SL_PER_DAY))
    return "\n".join(lines)

def ob_text():
    px = st.get("last_price")
    obs = get_obs()
    if not px:
        return "Price not loaded yet."
    obs = [ob for ob in obs if (ob["dir"] == 1 and px >= ob["bot"]) or (ob["dir"] == -1 and px <= ob["top"])]
    obs.sort(key=lambda o: min(abs(px - o["top"]), abs(px - o["bot"])))
    if not obs:
        return "No active order blocks."
    lines = ["<b>Active Order Blocks</b> (price %s)" % fmt(px)]
    for ob in obs[:8]:
        lines.append("%s %s: %s – %s" % ("🟢 Bull" if ob["dir"] == 1 else "🔴 Bear", ob["tf"],
                                        fmt(ob["bot"]), fmt(ob["top"])))
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
        elif text.startswith("/ob"):
            send(ob_text())
        elif text.startswith("/start") or text.startswith("/help"):
            send("Commands:\n/status - price, volume flow, active trade, nearby OBs\n/ob - all active order blocks\n/stats - performance in R")
    save_state()


# ───────────────────────── MAIN LOOP ─────────────────────────
def roll_day():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st["day"] != today:
        st["day"] = today
        st["sl_today"] = 0
        st["paused_notified"] = False

def heartbeat():
    if HEARTBEAT_HOURS <= 0:
        return
    now = int(time.time() * 1000)
    if st["last_hb"] == 0:
        st["last_hb"] = now
        return
    if now - st["last_hb"] >= HEARTBEAT_HOURS * 3600000:
        st["last_hb"] = now
        tr = st["active"]
        state_txt = ("%s trade active" % ("BUY" if tr["dir"] == 1 else "SELL")) if tr else "waiting for signal"
        send("💚 Bot alive · %s %s · %s" % (SYMBOL, fmt(st["last_price"]) if st["last_price"] else "-", state_txt))

def tick():
    flush_outbox()
    c5 = fetch_klines(TF_MIN, 400)
    c1 = fetch_klines(1, 500)
    if not c5 or not c1:
        return
    roll_day()
    st["last_price"] = c1[-1][4]
    ema_t = ema_list([k[4] for k in c5], TREND_LEN)
    atr = atr_list(c5)
    d5 = build_deltas(c5, c1)
    n = len(c5)
    sv = sum(c5[k][5] for k in range(n - FLOW_LEN, n))
    st["last_flow"] = (sum(d5[k] for k in range(n - FLOW_LEN, n)) / sv) if sv > 0 else 0.0
    manage_trade(c1)
    process_new_bars(c5, d5, ema_t, atr)
    handle_commands()
    heartbeat()
    save_state()

def main():
    global st
    if not TOKEN or not CHAT_ID:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.")
        sys.exit(1)
    st = load_state()
    log.info("NORDVEX bot v1.1 running: %s %dm on %s", SYMBOL, TF_MIN, EXCHANGE)
    while True:
        try:
            tick()
        except Exception:
            log.exception("loop error")
            time.sleep(15)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
