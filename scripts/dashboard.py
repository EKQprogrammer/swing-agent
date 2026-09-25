"""Optional Streamlit dashboard for the swing trading agent.

Shows the current macro regime, the most recent daily_scan.py results, and
(if present) the most recent backtest run's metrics/equity curve.

Usage:
    streamlit run scripts/dashboard.py

Read-only: makes no live FMP/yfinance calls itself -- run scripts/daily_scan.py
or scripts/run_backtest.py first to produce the data this page displays.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from swing_agent.agents.macro_regime import MacroRegimeError, get_macro_regime
from swing_agent.config import load_config
from swing_agent.storage.db import get_connection

st.set_page_config(page_title="Swing Agent Dashboard", layout="wide")
st.title("Swing Trading Agent")

cfg = load_config()
conn = get_connection(cfg.data.db_path)

st.header("Macro Regime")
try:
    macro = get_macro_regime(conn)
    cols = st.columns(4)
    cols[0].metric("Regime", macro["regime"])
    cols[1].metric("Size Modifier", f"{macro['position_size_modifier']}x")
    cols[2].metric("SPY / 200MA", f"{macro['spy_close']:.2f} / {macro['spy_200ma']:.2f}")
    cols[3].metric("VIX", f"{macro['vix']:.2f}")
    st.caption(macro["reasoning"])
except MacroRegimeError as exc:
    st.warning(f"Macro regime unavailable: {exc}")

st.header("Latest Daily Scan")
scans_dir = Path("data/scans")
scan_files = sorted(scans_dir.glob("scan_*.json")) if scans_dir.exists() else []
if not scan_files:
    st.info("No scan results yet. Run `python scripts/daily_scan.py` first.")
else:
    latest = scan_files[-1]
    st.caption(f"Source: {latest.name}")
    results = json.loads(latest.read_text(encoding="utf-8"))
    rows = []
    for r in results:
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
