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

st.set_page_config(page_title="Swing Agent Dashboard", layout="wide", page_icon="📈")

# -- design system -------------------------------------------------------
# Streamlit's own markdown renderer treats a $...$ pair as inline LaTeX
# (KaTeX), which silently mangles any prose containing dollar amounts (the
# Trade Journal narratives are full of them) -- _escape_dollars() below
# guards every place user-facing prose gets rendered via st.markdown.
_STATUS_COLORS = {
    "APPROVE": ("#0ca30c", "rgba(12,163,12,0.12)"),
    "PASS": ("#0ca30c", "rgba(12,163,12,0.12)"),
    "BULLISH": ("#0ca30c", "rgba(12,163,12,0.12)"),
    "REJECT": ("#d03b3b", "rgba(208,59,59,0.12)"),
    "BEARISH": ("#d03b3b", "rgba(208,59,59,0.12)"),
    "ERROR": ("#d03b3b", "rgba(208,59,59,0.12)"),
    "CAUTIOUS": ("#e6b41e", "rgba(230,180,30,0.12)"),
}


def _escape_dollars(text: str) -> str:
    # A literal "\$" doesn't reliably suppress Streamlit's markdown-it+KaTeX
    # $...$ math-mode matching (and can leave a visible backslash) -- the
    # HTML entity form is decoded by the browser AFTER markdown parsing, so
    # markdown-it's LaTeX regex never sees a raw "$" to match against.
    return text.replace("$", "&#36;")


def _badge(label: str) -> str:
    fg, bg = _STATUS_COLORS.get(label, ("#9a9a9a", "rgba(154,154,154,0.12)"))
    return (
        f'<span style="display:inline-block;padding:2px 10px;border-radius:10px;'
        f'font-size:0.8rem;font-weight:600;letter-spacing:0.02em;'
        f'color:{fg};background:{bg};border:1px solid {fg}55;">{label}</span>'
    )


st.markdown(
    """
    <style>
    .swa-title { font-size:2.1rem; font-weight:800; margin-bottom:0; }
    .swa-subtitle { color:#9a9a9a; font-size:0.95rem; margin-top:2px; margin-bottom:1.2rem; }
    .swa-card { border:1px solid #2a2a2a; border-radius:10px; padding:14px 18px; margin-bottom:10px; background:#141414; }
    .swa-card-accent { border-left:3px solid var(--accent, #3987e5); }
    .swa-stat-label { color:#9a9a9a; font-size:0.78rem; text-transform:uppercase; letter-spacing:0.04em; }
    .swa-stat-value { font-size:1.6rem; font-weight:700; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="swa-title">📈 Swing Trading Agent</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="swa-subtitle">Three-layer macro / fundamental / technical screener, backtester, and trade journal</div>',
    unsafe_allow_html=True,
)

cfg = load_config()
conn = get_connection(cfg.data.db_path)
scans_dir = Path("data/scans")

with st.sidebar:
    st.header("Watchlist")
    default_watchlist = ", ".join(cfg.fundamental.universe)
    tickers_input = st.text_area("Tickers to scan (comma-separated)", value=default_watchlist, height=100)
    watchlist = [t.strip().upper() for t in tickers_input.split(",") if t.strip()]
    st.caption(
        f"{len(watchlist)} ticker(s). Stale fundamentals cost ~4 API calls "
        "per ticker on refresh -- keep the list short if watching quota."
    )
    refresh_clicked = st.button("Refresh scan", type="primary", use_container_width=True)

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
    with st.expander(f"⚠️ {len(st.session_state['fetch_warnings'])} fetch warning(s) from the last refresh"):
        for w in st.session_state["fetch_warnings"]:
            st.warning(w)

tab_scan, tab_backtest, tab_journal = st.tabs(["Macro & Scan", "Backtest", "Trade Journal"])

with tab_scan:
    st.subheader("Macro Regime")
    try:
        macro = get_macro_regime(conn)
        accent = _STATUS_COLORS.get(macro["regime"], ("#3987e5", ""))[0]
        st.markdown(
            f"""
            <div class="swa-card swa-card-accent" style="--accent:{accent};">
                <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">
                    <div>{_badge(macro['regime'])} <span style="margin-left:10px;color:#9a9a9a;">
                        size modifier <b style="color:#fff;">{macro['position_size_modifier']}x</b></span></div>
                </div>
                <div style="display:flex;gap:40px;margin-top:14px;flex-wrap:wrap;">
                    <div><div class="swa-stat-label">SPY / 200MA</div>
                        <div class="swa-stat-value">{macro['spy_close']:.2f} / {macro['spy_200ma']:.2f}</div></div>
                    <div><div class="swa-stat-label">VIX</div>
                        <div class="swa-stat-value">{macro['vix']:.2f}</div></div>
                </div>
                <div style="color:#9a9a9a;font-size:0.85rem;margin-top:12px;">{macro['reasoning']}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    except MacroRegimeError:
        st.info("No macro data yet -- click **Refresh scan** in the sidebar to fetch it.")

    st.subheader("Watchlist Scan")
    scan_results = st.session_state.get("scan_results")
    scan_source = None
    if scan_results is None:
        scan_files = sorted(scans_dir.glob("scan_*.json")) if scans_dir.exists() else []
        if scan_files:
            scan_results = json.loads(scan_files[-1].read_text(encoding="utf-8"))
            scan_source = scan_files[-1].name

    if scan_results is None:
        st.info("No scan results yet. Click **Refresh scan** in the sidebar, or run `python scripts/daily_scan.py` locally.")
    else:
        if scan_source:
            st.caption(f"Source: {scan_source} (last scripts/daily_scan.py run)")
        approved = [r for r in scan_results if r["verdict"] == "APPROVE"]
        st.caption(f"{len(approved)} of {len(scan_results)} approved")
        for r in scan_results:
            if r["verdict"] == "APPROVE":
                risk = r["risk"]
                detail = (
                    f"{r['technical']['setup']} &middot; entry <b>{risk['entry']:.2f}</b> "
                    f"&middot; stop <b>{risk['stop']:.2f}</b> &middot; {risk['shares']} shares"
                )
            else:
                detail = f"{r.get('reject_layer', 'n/a')} layer &middot; {r.get('reasoning', '')}"
            st.markdown(
                f"""
                <div class="swa-card" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">
                    <div><b style="font-size:1.05rem;">{r['ticker']}</b>
                        <span style="margin-left:10px;">{_badge(r['verdict'])}</span></div>
                    <div style="color:#b5b5b5;font-size:0.88rem;">{detail}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

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
            st.line_chart(curve_df["equity"], color="#3987e5", height=320)

        by_setup = payload.get("by_setup")
        if by_setup:
            st.subheader("By Setup")
            setup_cols = st.columns(len(by_setup))
            for col, (setup, m) in zip(setup_cols, by_setup.items()):
                wr = f"{m['win_rate']:.1f}%" if m.get("win_rate") is not None else "N/A"
                ar = f"{m['avg_r']:.2f}" if m.get("avg_r") is not None else "N/A"
                col.markdown(
                    f"""<div class="swa-card"><div class="swa-stat-label">{setup}</div>
                    <div class="swa-stat-value">{wr}</div>
                    <div style="color:#9a9a9a;font-size:0.85rem;">{m['num_trades']} trades &middot; {ar} avg R</div></div>""",
                    unsafe_allow_html=True,
                )

        st.subheader("Trades")
        trades_df = pd.DataFrame(payload["trades"])
        if not trades_df.empty and "r_multiple" in trades_df.columns:
            display_cols = [
                c for c in ["ticker", "setup", "entry_date", "exit_date", "entry_price", "shares", "pnl", "r_multiple"]
                if c in trades_df.columns
            ]
            styled_trades = trades_df[display_cols].style.map(
                lambda v: "color: #0ca30c" if isinstance(v, (int, float)) and v > 0
                else ("color: #d03b3b" if isinstance(v, (int, float)) and v < 0 else ""),
                subset=["r_multiple", "pnl"],
            )
            st.dataframe(styled_trades, width="stretch", hide_index=True)
            st.caption("Full entry reasoning and macro context for each trade: see the Trade Journal tab.")
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
        for t in filtered[:200]:
            if t["r_multiple"] > 0.05:
                label = "WIN"
            elif t["r_multiple"] < -0.05:
                label = "LOSS"
            else:
                label = "BREAKEVEN"
            narrative = _escape_dollars(t.get("narrative", "(no narrative)"))
            st.markdown(
                f"""
                <div class="swa-card">
                    <div style="margin-bottom:6px;"><b style="font-size:1.02rem;">{t['ticker']}</b>
                        <span style="margin-left:10px;">{_badge(label)}</span></div>
                    <div style="color:#c9c9c9;font-size:0.9rem;line-height:1.5;">{narrative}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        if len(filtered) > 200:
            st.caption(f"Showing first 200 of {len(filtered)} -- narrow with the filters above to see more.")
    else:
        st.info("No backtest results found yet. Run with `--narratives` to populate this tab.")
