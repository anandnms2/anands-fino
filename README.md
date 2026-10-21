# Anands Fino

AI research and signal desk for forex and metals, executing through the cTrader Open API (MT5 under Wine is an optional alternative).
FreeLLMAPI does the volume work (tagging news). **Claude gives the final YES or NO on every trade.**
Hard risk limits live in code and Claude cannot override them.

## Flow (one cycle per candle, default M3 = every 3 minutes; set `TIMEFRAME_MIN`)
data → technicals → research (FRED, CFTC, calendar, news) → weighted score (7 factors, at least 60% of them must agree, `MIN_AGREE_PCT`) → risk gate → **Claude YES/NO** → gate re-check → execute / ask approval → journal.

## Deploy on Coolify (cTrader, the default)
The broker link is the **cTrader Open API**: no MetaTrader, no Wine. Stack = app + Postgres.

1. **Open a cTrader account** at your broker (a *demo* first). Note the cTrader ID (email login) you use for it.
2. **Register an app** at <https://openapi.ctrader.com> (sign in with that cTrader ID, "Applications" → add). Set the **redirect URI** to `https://<your app domain>/ctrader/callback`. Copy the **Client ID** and **Secret**.
3. In Coolify, Anands Fino app: Build Pack **Docker Compose**, location `/docker-compose.yaml`, domain `app` → port **8000**.
4. Environment variables: `CTRADER_CLIENT_ID`, `CTRADER_CLIENT_SECRET`, `ANTHROPIC_API_KEY`, `FREELLMAPI_BASE_URL` (ends in `/v1`), `FREELLMAPI_KEY`, `FRED_API_KEY`. Optional: `PAPER_MODE=true` to start with the Paper venue on. Coolify generates `SERVICE_PASSWORD_ADMIN` (the dashboard login) and the DB password.
5. Deploy, open the dashboard, press **Connect cTrader**, sign in with the cTrader ID and grant *trading* access. Tokens are stored in the database and refreshed automatically (access tokens last about 30 days).
6. Open `/api/diagnostics?venue=demo` (also `paper`, `live`): it should show your demo account, each symbol with its digits and lot sizes, and candle counts. Broker symbol names that differ (for example `XAUUSD.r`) go in `SYMBOL_MAP` as JSON.

`CTRADER_DEMO_ACCOUNT_ID` / `CTRADER_LIVE_ACCOUNT_ID` (trader login) are only needed when the cTrader ID has several accounts of the same type. Demo and Live use separate connections (`demo.` and `live.ctraderapi.com`); the same cTrader ID and token serve both.

### Optional: MT5 under Wine
`docker-compose.mt5.yaml` is the older stack with MetaTrader 5 in Wine (set `BROKER=mt5`). The current MT5 build can crash under Wine with a false "debugger found" error, so it is not the default.

## Three venues: Paper, Demo, Live
One analysis and **one Claude YES/NO per symbol** feed every venue that is switched on. Each venue then runs its own risk gate, account, positions, daily-loss base, loss streak and pause, so they can run side by side.

| Venue | Account | Needs |
|---|---|---|
| **Paper** | Simulated balance (`PAPER_BALANCE`) on real prices from a free feed | nothing; works while cTrader approval is pending |
| **Demo** | cTrader demo account with demo funds | cTrader app approved, `Connect cTrader` done |
| **Live** | cTrader live account, real money | the above plus `ALLOW_LIVE=true` |

Each venue card on the dashboard has two switches: **Trade** (on/off) and **Auto-approve**. With auto-approve on, a cleared trade is sent to that venue straight away. With it off, the trade waits in "Waiting for your approval" (`APPROVAL_TTL_MIN`) until you click Approve. Live is locked until you press **Unlock** on the Live card (or start with `ALLOW_LIVE=true`; the dashboard switch wins afterwards, and **Lock Live** switches Live off again), switching Live on or auto-approving it asks for confirmation, and a venue refuses an account of the wrong type (Live refuses a demo account, Demo and Paper refuse a real one).
With every venue off the app still analyses and records signals (and posts to `NOTIFY_WEBHOOK_URL`), it just places nothing.

A venue pauses by itself after `MAX_LOSS_STREAK` losses in a row or a 2% daily loss; the red **Halt trading** button stops all venues. **Resume** clears both.

**Paper prices.** `PAPER_FEED=yahoo` (no key, unofficial) or `twelvedata` (`TWELVEDATA_API_KEY`; check that the free plan covers XAU/XAG). Fills are simulated with a typical spread and no slippage, and a stop wins when one candle touches both stop and target, so paper results are optimistic. Paper levels are shifted onto the paper feed's own price (a venue quoting more than 0.5 ATR away is skipped). `Reset` on the Paper card restores the starting balance.

**Initial switches** come from the environment (`PAPER_MODE`, `MODE`: `demo_auto` switches Demo on; `live_approval` / `live_auto` switch Live on with manual / auto approval when `ALLOW_LIVE=true`); after that the dashboard switches win and are stored in the database.

## Candle size per venue
Each venue card has a **Candles** selector (1, 3, 5, 15, 30 min or 1 hour). The scheduler wakes every minute and runs a venue only when a candle of its own size has just closed, so Paper can trade M1 while Demo trades M5 and Live trades H1. Venues on the same size share one analysis and one Claude answer. `TIMEFRAME_MIN` is only the starting value.

**Short charts and spreads.** On M1/M3 the ATR is often smaller than the spread, which used to leave a stop thinner than the spread (blocked) and a huge lot. Three rules now handle it: the stop is never closer than 1.05 ÷ `MAX_SPREAD_TO_STOP` spreads (10.5 at the default 10%; `MIN_STOP_SPREADS` overrides), and a setup whose stop would then be wider than `MAX_STOP_ATR` (3.5) ATRs is skipped because its target would be out of reach; the trade is skipped when one average candle moves less than `MIN_ATR_TO_SPREAD` (2) spreads (nothing to earn after costs); and the existing `MAX_SPREAD_TO_STOP` (10%) still applies. Paper spreads default to typical raw-account values (EURUSD 0.7 pip, gold 0.25); set `PAPER_SPREADS` to what your cTrader demo actually quotes. The Demo venue always uses the real cTrader bid/ask.

## Contract specs and spreads
Click **Contracts** in the dashboard header. Demo and Live show what your cTrader account reports for every traded symbol: digits, units per lot, lot minimum and step, the currency P/L is paid in, the live spread (in price and in points) and the cost of one lot's spread. Lot size, stop distance and risk are always computed from those broker numbers, so metals, yen pairs and any non-standard contract are sized correctly. Paper is a simulation, so its rows are editable (spread, units per lot, digits, lot min/step) and saved in the database; **Default** restores the built-in value. Match the Paper numbers to what the Demo tab shows for a fair test.

## Symbols
The **Symbols** card on the dashboard overrides the `SYMBOLS` variable (Coolify keeps the value from the first deploy). Default 13: XAUUSD, XAGUSD, EURUSD, GBPUSD, USDJPY, USDCHF, USDCAD, AUDUSD, NZDUSD, EURJPY, GBPJPY, EURGBP, AUDJPY (`SYMBOLS`, comma separated; any FX pair or metal your broker lists works, use `SYMBOL_MAP` for odd broker names).
- Each cycle ranks the setups by score and works through the strongest first, so the open-trade slots go to the best ones.
- Pairs whose profit is not in USD (JPY, CHF, CAD, GBP) are sized by converting the stop distance into the account currency with the live rate; if the rate cannot be priced the trade is refused.
- Correlation groups (one open trade per group and direction): the metals; the four dollar-short pairs (EURUSD, GBPUSD, AUDUSD, NZDUSD); the three dollar-long pairs (USDJPY, USDCHF, USDCAD); the four yen-short pairs (USDJPY and the JPY crosses).
- The macro (dollar, real yield) factor does not apply to cross pairs, and the CFTC factor covers the pairs with a futures contract, so crosses are scored on fewer factors; the 60% agree rule works on whatever is available.
- More symbols means more price requests per cycle and a busier 3-minute loop. The free Twelve Data plan cannot feed 13 symbols; use Yahoo or cTrader prices.
- The CFTC names for the added currencies (yen, franc, Canadian, Australian and NZ dollar) are not yet checked against a live download.

## Schedule
Trading cycle every `TIMEFRAME_MIN` minutes (default 3, on candle close, 15 s after the boundary) · open trades checked every 30 s · news every 5 min · calendar hourly · macro 6 h · COT 12 h. Trend confirmation uses H1 on M3 charts (H4 on M30/H1). After Claude says NO to a symbol and direction it is not asked again for `CLAUDE_COOLDOWN_MIN` (15) minutes, which keeps the Claude call count down. Old no-trade rows (3 days) and journal lines (14 days) are pruned automatically.

**M3 caveats.** Spread and commission eat far more of a 3-minute stop than an hourly one, so the spread check will block many setups and live results will be worse than paper. The free Twelve Data plan cannot refresh four symbols every 3 minutes (800 calls a day), so it refreshes about every 8 minutes; Yahoo (1-minute data, resampled) has no such cap.

## Risk gate (code, not model)
0.5% risk per trade, 2% daily loss cap, max 3 open trades, min R:R 1.5, spread ≤ 10% of stop, Claude confidence ≥ 0.6, halt after 5 straight losses, ±15 min news blackout, one open trade per symbol, no same-direction trades in correlated pairs, lots rounded down to the broker step.

## Honest status
- Logic, gate, scoring and API are covered by tests against a mock broker (`pytest`).
- **Not yet verified against a real cTrader server:** the cTrader client was written from Spotware's published protocol and tested only against a fake server that mimics it (login, symbols, prices, candles, orders, stops, closes, deals). The CFTC report parsing is also untested. Use `/api/diagnostics` first and stay on demo until you have watched several days of fills.
- cTrader market orders cannot carry an absolute stop, so the app sets the stop and target on the open position right after the fill, and **closes the position again if that fails**.
- This is not financial advice; no strategy is guaranteed profitable.

## Local dev
```
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload
pytest
```
