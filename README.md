# NORDVEX.AI Telegram Bot (BTCUSDT 5m)

Same logic as the Pine indicator (Volume Profile + volume flow + EMA trend), sends BUY/SELL, SL, BOOK TP1, BOOK TP2 and hit updates to Telegram. No TradingView plan needed.

## Setup
1. Telegram: @BotFather -> /newbot -> copy token. Use a NEW bot (do not reuse @Mr_Most_Profitable_Bot, two programs polling one token conflict).
2. Send any message to the new bot, then open https://api.telegram.org/bot<TOKEN>/getUpdates and copy chat.id (or use @userinfobot).
3. Railway: new project -> upload these 4 files (GitHub repo) -> Variables:
   - TELEGRAM_BOT_TOKEN = your token
   - TELEGRAM_CHAT_ID = your chat id
   - (optional) EXCHANGE = mexc | bybit | binance   (default mexc; try bybit if mexc errors)
   - (optional) STATE_PATH = /data/state.json  + attach a Railway volume at /data so an open trade/stats survive redeploys
4. Deploy. You get a "bot started" message. Commands: /status, /stats

## Optional variables (defaults)
SYMBOL=BTCUSDT, MAX_SL_PER_DAY=2 (0=off), TP1_CLOSE_PCT=50, RR1=1, RR2=2, MOVE_SL_TO_BE=1,
COOLDOWN_BARS=3, COST_PCT=0.05 (fees+slippage in R stats), USE_TREND=1, FLOW_THR=0.08

## Notes
- Signals only on CLOSED 5m candles. TP/SL tracking uses closed 1m candles (up to ~1 min delay).
- Stats in /stats are forward-only (since bot start) - judge it after 30+ trades, not before.
- Small differences vs TradingView are normal (different exchange feed, 1m tracking).
- Not financial advice. Risk max 1% per trade.
