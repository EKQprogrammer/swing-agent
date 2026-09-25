"""Streamlit dashboard for the swing trading agent.

Shows the current macro regime, a live-refreshable scan of a watchlist, and
(if present) the most recent backtest run's metrics, equity curve, and
per-trade narratives.

Usage:
    streamlit run scripts/dashboard.py            (local)
    -- or deployed on Streamlit Community Cloud --

There is no background scheduler on Streamlit Community Cloud's free tier,
so this page fetches nothing automatically: data only refreshes when the
"Refresh scan" button is clicked (or via scripts/daily_scan.py locally /
on a schedule, whose data/scans/scan_<date>.json output this page also
reads if present).

EODHD_API_KEY / FMP_API_KEY resolution: read from the environment as usual
for local runs (.env via python-dotenv, see swing_agent.config.load_config);
if unset, falls back to Streamlit's secrets manager (st.secrets[...]) so a
Streamlit Community Cloud deployment can supply them via Settings -> Secrets
without an .env file (which never leaves your machine / is gitignored).

Theming lives in .streamlit/config.toml (dark, trading-terminal palette).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from swing_agent.agents.macro_regime import MacroRegimeError, get_macro_regime
from swing_agent.agents.orchestrator import get_verdict
from swing_agent.backtest.narrative import build_trade_narratives
from swing_agent.config import load_config
from swing_agent.data.macro import fetch_and_store_macro_series
from swing_agent.data.prices import fetch_and_store_prices
from swing_agent.storage.db import get_connection

for _key_name in ("EODHD_API_KEY", "FMP_API_KEY"):
    if not os.environ.get(_key_name):
        try:
            _secret_value = st.secrets.get(_key_name)
        except Exception:
            _secret_value = None
        if _secret_value:
            os.environ[_key_name] = _secret_value

st.set_page_config(page_title="Swing Agent Dashboard", layout="wide")
st.title("Swing Trading Agent")

cfg = load_config()
conn = get_connection(cfg.data.db_path)
scans_dir = Path("data/scans")

with st.sidebar:
    st.header("Watchlist")
    default_watchlist = ", ".join(cfg.fundamental.universe)
    tickers_input = st.text_area("Tickers to scan (comma-separated)", value=default_watchlist, height=100)
    watchlist = [t.strip().upper() for t in tickers_input.split(",") if t.strip()]
    st.caption(
        f"{len(watchlist)} ticker(s). Each ticker whose fundamentals cache has "
        "expired (7-day TTL) costs ~4 FMP API calls on refresh -- keep the "
        "list short if you're watching your free-tier quota."
    )
    refresh_clicked = st.button("Refresh scan", type="primary")

if refresh_clicked:
    # Every external call below is isolated in its own try/except: this runs
    # on a cloud network path (unlike local dev), where a single slow/failed
    # request (FRED, yfinance) must not take down the whole page. DGS10 is
    # informational-only in get_macro_regime, so skipping it on failure is safe.
    fetch_warnings = []
    with st.spinner("Refreshing macro + watchlist data and re-evaluating..."):
        for ticker in cfg.data.tickers:
            try:
                fetch_and_store_prices(conn, ticker, period=f"{cfg.data.price_history_years}y")
            except Exception as exc:
                fetch_warnings.append(f"Price fetch failed for {ticker}: {exc}")

        try:
            fetch_and_store_macro_series(conn, "DGS10")
        except Exception as exc:
            fetch_warnings.append(f"10Y yield (DGS10) fetch failed, continuing without it: {exc}")

        for ticker in watchlist:
            if ticker not in cfg.data.tickers:
                try:
                    fetch_and_store_prices(conn, ticker, period=f"{cfg.data.price_history_years}y")
                except Exception as exc:
                    fetch_warnings.append(f"Price fetch failed for {ticker}: {exc}")

        results = []
        for ticker in watchlist:
            try:
                results.append(get_verdict(conn, ticker))
            except Exception as exc:
                results.append({
                    "ticker": ticker, "verdict": "ERROR", "as_of_date": None,
                    "reject_layer": None, "reasoning": str(exc),
                })
        st.session_state["scan_results"] = results
        st.session_state["scan_watchlist"] = watchlist
        st.session_state["fetch_warnings"] = fetch_warnings

if st.session_state.get("fetch_warnings"):
    with st.expander(f"{len(st.session_state['fetch_warnings'])} fetch warning(s) from the last refresh"):
        for w in st.session_state["fetch_warnings"]:
            st.warning(w)

tab_scan, tab_backtest, tab_journal = st.tabs(["Macro & Scan", "Backtest", "Trade Journal"])

with tab_scan:
    st.subheader("Macro Regime")
    with st.container(border=True):
        try:
            macro = get_macro_regime(conn)
            cols = st.columns(4)
            cols[0].metric("Regime", macro["regime"])
            cols[1].metric("Size Modifier", f"{macro['position_size_modifier']}x")
            cols[2].metric("SPY / 200MA", f"{macro['spy_close']:.2f} / {macro['spy_200ma']:.2f}")
            cols[3].metric("VIX", f"{macro['vix']:.2f}")
            st.caption(macro["reasoning"])
        except MacroRegimeError:
            st.info("No macro data yet -- click **Refresh scan** in the sidebar to fetch it.")

    st.subheader("Watchlist Scan")
    scan_results = st.session_state.get("scan_results")
    if scan_results is None:
        scan_files = sorted(scans_dir.glob("scan_*.json")) if scans_dir.exists() else []
        if scan_files:
            scan_results = json.loads(scan_files[-1].read_text(encoding="utf-8"))
            st.caption(f"Source: {scan_files[-1].name} (last scripts/daily_scan.py run)")

    if scan_results is None:
        st.info("No scan results yet. Click **Refresh scan** in the sidebar, or run `python scripts/daily_scan.py` locally.")
    else:
        rows = []
        for r in scan_results:
            row = {"ticker": r["ticker"], "verdict": r["verdict"]}
            if r["verdict"] == "APPROVE":
                risk = r["risk"]
                row.update({
                    "setup": r["technical"]["setup"], "entry": risk["entry"],
                    "stop": risk["stop"], "shares": risk["shares"],
                })
            else:
                row["reject_layer"] = r.get("reject_layer")
                row["reasoning"] = r.get("reasoning")
            rows.append(row)
        scan_df = pd.DataFrame(rows)
        styled = scan_df.style.map(
            lambda v: "color: #0ca30c" if v == "APPROVE" else ("color: #d03b3b" if v == "REJECT" else ""),
            subset=["verdict"],
        )
        st.dataframe(styled, width="stretch")

conn.close()

backtest_path = scans_dir / "backtest_latest.json"

with tab_backtest:
    st.caption(
        "Populate this tab with: "
        "`python scripts/run_backtest.py --start ... --end ... --narratives --json > data/scans/backtest_latest.json`"
    )
    if backtest_path.exists():
        payload = json.loads(backtest_path.read_text(encoding="utf-8"))
        metrics = payload["metrics"]
        cols = st.columns(5)
        cols[0].metric("Trades", metrics["num_trades"])
        cols[1].metric("Win Rate", f"{metrics['win_rate']:.1f}%" if metrics["win_rate"] is not None else "N/A")
        cols[2].metric("Avg R", f"{metrics['avg_r']:.2f}" if metrics["avg_r"] is not None else "N/A")
        cols[3].metric("Max DD", f"{metrics['max_drawdown_pct']:.1f}%" if metrics["max_drawdown_pct"] is not None else "N/A")
        cols[4].metric("Final Equity", f"${metrics['final_equity']:,.0f}" if metrics.get("final_equity") is not None else "N/A")

        equity_curve = payload.get("equity_curve")
        if equity_curve:
            st.subheader("Equity Curve")
            curve_df = pd.DataFrame(equity_curve).set_index("date")
            st.line_chart(curve_df["equity"], color="#3987e5")

        st.subheader("Trades")
        trades_df = pd.DataFrame(payload["trades"])
        if not trades_df.empty and "r_multiple" in trades_df.columns:
            display_cols = [c for c in trades_df.columns if c not in ("fills", "entry_indicators", "narrative")]
            styled_trades = trades_df[display_cols].style.map(
                lambda v: "color: #0ca30c" if isinstance(v, (int, float)) and v > 0
                else ("color: #d03b3b" if isinstance(v, (int, float)) and v < 0 else ""),
                subset=["r_multiple"],
            )
            st.dataframe(styled_trades, width="stretch")
    else:
        st.info("No backtest results found yet.")

with tab_journal:
    st.caption("Plain-English win/loss story for each backtest trade -- why it entered, what happened, why it won or lost.")
    if backtest_path.exists():
        payload = json.loads(backtest_path.read_text(encoding="utf-8"))
        trades = payload["trades"]
        if trades and "narrative" not in trades[0]:
            trades = build_trade_narratives(trades)

        col1, col2 = st.columns(2)
        tickers_in_trades = sorted({t["ticker"] for t in trades})
        ticker_filter = col1.multiselect("Ticker", tickers_in_trades)
        result_filter = col2.selectbox("Result", ["All", "Wins", "Losses"])

        filtered = trades
        if ticker_filter:
            filtered = [t for t in filtered if t["ticker"] in ticker_filter]
        if result_filter == "Wins":
            filtered = [t for t in filtered if t["r_multiple"] > 0.05]
        elif result_filter == "Losses":
            filtered = [t for t in filtered if t["r_multiple"] < -0.05]

        st.caption(f"{len(filtered)} of {len(trades)} trades")
        for t in filtered:
            icon = "🟢" if t["r_multiple"] > 0.05 else ("🔴" if t["r_multiple"] < -0.05 else "⚪")
            with st.container(border=True):
                st.markdown(f"{icon} **{t['ticker']}** -- {t.get('narrative', '(no narrative)')}")
    else:
        st.info("No backtest results found yet. Run with `--narratives` to populate this tab.")
