# Swing Trading Agent

## Project Overview

A Python-based swing trading agent that screens stocks using a three-layer filter (macro → fundamental → technical), outputs actionable trade plans (entry, stop, targets, position size), and can backtest the strategy on historical data.

- Time frame: swing trades held 3 days to 3 weeks
- Target win rate: 50-60%
- Risk per trade: 1% of account equity
- Budget: $0 (free APIs only)

## Strategy Specification

A ticker must pass ALL THREE layers in order. If any layer fails, the verdict is REJECT.

### Layer 1 — Macro Regime
Detects whether the market environment supports long swing trades.

- BULLISH: SPY close > SPY 200-day SMA AND VIX < 25 → position-size modifier 1.0x
- CAUTIOUS: SPY close > SPY 200-day SMA AND VIX 25-35 → modifier 0.5x
- BEARISH: SPY close < SPY 200-day SMA OR VIX > 35 → no new longs (0.0x)

### Layer 2 — Fundamental Quality
Only trade strong, high-quality companies that institutions accumulate. All thresholds must pass:

- ROIC > 10%
- Free Cash Flow positive
- FCF Margin > 5%
- Revenue growth YoY > 5%
- Earnings growth YoY > 10%
- Relative Strength vs universe > 70 (prefer > 80)
- Price > $10
- Average daily volume > 500,000 shares

Data source: Financial Modeling Prep free tier (FMP_API_KEY in .env). Cache results in SQLite `fundamentals` table with 7-day TTL.

### Layer 3 — Technical Trigger
Three-indicator stack: 50 EMA (trend), RSI(14) (timing), ATR(14) (risk).

Three valid setups:
1. PULLBACK — In uptrend, price pulls back to 20 EMA or 50 EMA on declining volume, then resumes with close above prior day's high on above-average volume.
2. BREAKOUT — 3-8 week consolidation (5-15% wide), volume dries up, then breaks out on 1.5×+ average volume.
3. FAILED BREAKDOWN (half size) — Price breaks below support on panic volume, recovers and closes back above support within 1-2 days.

### Position Sizing
Position Size = (Account Equity × 0.01) / (Entry − Stop)

Rules:
- Max 1% risk per trade
- Max 5-6 concurrent positions
- Max 25-30% of capital in one sector
- Reduce size 50% after 3 consecutive losses
- Half size on failed-breakdown setups

### Exit Rules
1. Initial stop: setup low or 2× ATR (whichever tighter) → 1R loss
2. Move to breakeven when trade hits 1R profit
3. Take 50% off at 2R
4. Take 25% off at 3R
5. Trail final 25% at 10-day EMA or 2× ATR
6. Time stop: exit if no 1R move in 5 trading days
7. Psychological stop: exit if you can't focus

## Tech Stack

- Python 3.10+
- yfinance (prices), FRED (macro rates), Financial Modeling Prep (fundamentals)
- SQLite at `data/swing.db`
- YAML config (`config.yaml`), secrets in `.env`
- pytest for tests
- requests for HTTP
- No paid services, no cloud, no paid LLM APIs

## Project Structure
swing-agent/
├── CLAUDE.md
├── config.yaml
├── .env (gitignored)
├── .env.example
├── requirements.txt
├── run.py # CLI entry point
├── src/swing_agent/
│ ├── __init__.py
│ ├── config.py
│ ├── logging_setup.py
│ ├── data/
│ │ ├── prices.py
│ │ ├── macro.py
│ │ └── fundamentals.py
│ ├── storage/
│ │ ├── db.py
│ │ └── schema.sql
│ ├── agents/
│ │ ├── macro_regime.py
│ │ ├── fundamental.py
│ │ ├── technical.py
│ │ ├── risk_manager.py
│ │ └── orchestrator.py
│ └── backtest/
│ ├── engine.py
│ └── metrics.py
├── scripts/
│ ├── fetch_prices.py
│ ├── fetch_macro.py
│ ├── fetch_fundamentals.py
│ ├── run_backtest.py
│ └── daily_scan.py
├── tests/
└── data/
└── swing.db

## Coding Conventions

- **Point-in-time data discipline**: never use data filed after the "as of" date. For prices, slice with `.loc[:date]`. For fundamentals, filter by `filed_date`.
- **Idempotent DB writes**: use `CREATE TABLE IF NOT EXISTS`, `INSERT OR REPLACE`, and guard against empty DataFrames.
- **Config-driven thresholds**: RSI levels, ATR multipliers, ROIC cutoff, account size, etc. all live in `config.yaml`, not hardcoded.
- **Clear logging**: every IO operation logs via `get_logger(__name__)`.
- **No look-ahead in backtests**: use only data available as of each simulated date.
- **Tests must not require network**: use synthetic data or in-memory SQLite. Real API calls go behind a `@pytest.mark.live` marker.
- **Type hints** on all public functions. Python 3.10 union syntax (`float | None`).
- **Review your own code before considering a phase complete**: check unused imports, missing error handling, empty-frame edge cases, and SQL injection risks.

## Phase Plan

Work one phase at a time. At the end of each phase, run the tests, run a smoke-test command, and commit to git with a message like `Phase N: <summary>`. Then pause and report status. Do not roll into the next phase without being told to proceed.

- **Phase 1 — Data Foundation** (already done): prices + macro into SQLite.
- **Phase 2 — Macro Regime Agent**: detect BULLISH / CAUTIOUS / BEARISH, output modifier.
- **Phase 3 — Fundamental Filter**: screen quality using FMP. Cache in `fundamentals` table.
- **Phase 4 — Technical Agent**: compute EMA/RSI/ATR/RS, detect the 3 setups, return entry/stop/targets.
- **Phase 5 — Orchestrator + Risk Manager**: chain the three layers, produce final verdict dict.
- **Phase 6 — CLI (`run.py`)**: single-ticker verdict, `--scan` watchlist, `--json` output.
- **Phase 7 — Backtester**: walk-forward over 2020-2024, output win rate, average R, expectancy, max DD, Sharpe, equity curve.
- **Phase 8 — Automation**: `scripts/daily_scan.py`, Windows Task Scheduler `.bat`, optional Streamlit dashboard.

## Rules for Claude Code

1. **One phase at a time.** Build Phase N, test it, commit it, report status. Wait for me to say "proceed" before starting Phase N+1.
2. **Write real files.** Do not tell me to create files by hand. Use your Write/Edit tools.
3. **Run the tests yourself.** Run `pytest` after each phase. If something fails, fix it before reporting.
4. **Commit at phase boundaries.** `git add -A && git commit -m "Phase N: <description>"`.
5. **Report concisely at phase end.** Show:
   - Files created
   - Test result (`X passed`)
   - One smoke-test command + its output
   - Any blockers or design decisions I should review
   - "Ready to proceed to Phase N+1?"
6. **No clarifying questions unless truly blocked.** Make reasonable choices, document them in code comments, proceed.
7. **Do not invent paid APIs or cloud services.** Everything must run on the free stack above.
8. **Preserve existing code.** Phase 2 must not break Phase 1. Same for later phases. If a refactor is needed, flag it and ask.
