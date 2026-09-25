"""Streamlit dashboard for the swing trading agent.

Shows the current macro regime, a live-refreshable scan of a watchlist, and
(if present) the most recent backtest run's metrics.

Usage:
    streamlit run scripts/dashboard.py            (local)
    -- or deployed on Streamlit Community Cloud --

There is no background scheduler on Streamlit Community Cloud's free tier,
so this page fetches nothing automatically: data only refreshes when the
"Refresh scan" button is clicked (or via scripts/daily_scan.py locally /
on a schedule, whose data/scans/scan_<date>.json output this page also
reads if present).

FMP_API_KEY resolution: read from the environment as usual for local runs
(.env via python-dotenv, see swing_agent.config.load_config); if unset,
falls back to Streamlit's secrets manager (st.secrets["FMP_API_KEY"]) so a
Streamlit Community Cloud deployment can supply it via Settings -> Secrets
without an .env file (which never leaves your machine / is gitignored).
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
from swing_agent.config import load_config
from swing_agent.data.macro import fetch_and_store_macro_series
from swing_agent.data.prices import fetch_and_store_prices
from swing_agent.storage.db import get_connection

if not os.environ.get("FMP_API_KEY"):
    try:
        secret_key = st.secrets.get("FMP_API_KEY")
    except Exception:
        secret_key = None
    if secret_key:
        os.environ["FMP_API_KEY"] = secret_key

st.set_page_config(page_title="Swing Agent Dashboard", layout="wide")
st.title("Swing Trading Agent")

cfg = load_config()
conn = get_connection(cfg.data.db_path)

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
    with st.spinner("Refreshing macro + watchlist data and re-evaluating..."):
        for ticker in cfg.data.tickers:
            fetch_and_store_prices(conn, ticker, period=f"{cfg.data.price_history_years}y")
        fetch_and_store_macro_series(conn, "DGS10")
        for ticker in watchlist:
            if ticker not in cfg.data.tickers:
                fetch_and_store_prices(conn, ticker, period=f"{cfg.data.price_history_years}y")

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

st.header("Macro Regime")
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

st.header("Watchlist Scan")
scans_dir = Path("data/scans")
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
    st.dataframe(pd.DataFrame(rows), width="stretch")

conn.close()

st.header("Backtest")
st.caption("Run `python scripts/run_backtest.py --start ... --end ... --json > data/scans/backtest_latest.json` to populate this section.")
backtest_path = scans_dir / "backtest_latest.json"
if backtest_path.exists():
    payload = json.loads(backtest_path.read_text(encoding="utf-8"))
    metrics = payload["metrics"]
    cols = st.columns(4)
    cols[0].metric("Trades", metrics["num_trades"])
    cols[1].metric("Win Rate", f"{metrics['win_rate']:.1f}%" if metrics["win_rate"] is not None else "N/A")
    cols[2].metric("Avg R", f"{metrics['avg_r']:.2f}" if metrics["avg_r"] is not None else "N/A")
    cols[3].metric("Max DD", f"{metrics['max_drawdown_pct']:.1f}%" if metrics["max_drawdown_pct"] is not None else "N/A")
    st.dataframe(pd.DataFrame(payload["trades"]), width="stretch")
else:
    st.info("No backtest results found yet.")
