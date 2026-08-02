# Market Engine v1 — your personal finance analyst

Watches your tickers, pushes Robinhood-style alerts + a morning brief and evening wrap to your phone. Runs free on GitHub Actions.

## Setup (10 min)

1. **Push this folder to a new PRIVATE GitHub repo** (e.g. `market-engine`).
2. **Phone:** install the **ntfy** app (Play Store / App Store) → Subscribe to topic → pick something random like `ved-mkt-x7k2p9` (random = nobody else can guess your feed).
3. **Repo → Settings → Secrets and variables → Actions → New secret:**
   - `NTFY_TOPIC` = your topic from step 2
   - `ANTHROPIC_API_KEY` = optional, enables the Analyst Take voice (console.anthropic.com)
4. **Fill `watchlist.json`** — buy zones `[low, high]` and targets. `null` skips that rule.
5. **Test:** repo → Actions tab → `market-engine` → Run workflow → mode `brief`. Push should hit your phone in ~1 min.

## Local test (before pushing)

```
pip install -r requirements.txt
set NTFY_TOPIC=ved-mkt-x7k2p9     # PowerShell: $env:NTFY_TOPIC="ved-mkt-x7k2p9"
python engine.py --mode brief
```

## Schedule (weekdays)

| Time (CT) | Mode | What you get |
|---|---|---|
| 8:15 AM | brief | market pulse, every ticker vs your zones + 200W MA, earnings dates, headline each |
| every 30 min, market hours | scan | push ONLY when a rule fires: zone entry, target hit, ±4% move, 200W MA cross |
| 8:30 PM | wrap | what happened, why, and 📌 Watch — tomorrow's catalysts |

## Notes

- GitHub cron can run 5–15 min late. Normal.
- Cron times are set for CDT (UTC-5). After the November DST switch, shift them +1h in `market.yml`.
- Data is yfinance (free, unofficial Yahoo API) — fine for personal use, not for HFT.
- `state.json` is the engine's memory so the same alert never fires twice in one day. The workflow commits it back automatically.
- Alerts are crossing-based (price *crossed* your level vs yesterday's close), so you get pinged on the event, not spammed while it sits there.

## Roadmap

- **Weekend 2:** GitHub Pages dashboard (add-to-home-screen app feel), earnings calendar view
- **Later:** RSI + 50/200D crossovers, macro RSS (Fed/CPI), P&L tracking from positions
