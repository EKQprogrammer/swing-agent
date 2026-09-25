from __future__ import annotations

import json
from pathlib import Path

import daily_scan


def _canned_approve(conn, ticker: str) -> dict:
    return {
        "ticker": ticker,
        "as_of_date": "2024-06-01",
        "verdict": "APPROVE",
        "reject_layer": None,
        "technical": {"setup": "PULLBACK"},
        "risk": {"entry": 100.0, "stop": 95.0, "shares": 20},
    }


def _canned_reject(conn, ticker: str) -> dict:
    return {
        "ticker": ticker,
        "as_of_date": "2024-06-01",
        "verdict": "REJECT",
        "reject_layer": "technical",
        "reasoning": "No setup.",
    }


def test_scan_writes_json_snapshot_and_reports_approved(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daily_scan, "fetch_and_store_prices", lambda conn, ticker, period=None: 0)
    monkeypatch.setattr(daily_scan, "fetch_and_store_macro_series", lambda conn, series_id: 0)

    calls = {"AAA": _canned_approve, "BBB": _canned_reject}
    monkeypatch.setattr(
        daily_scan, "get_verdict", lambda conn, ticker: calls[ticker](conn, ticker)
    )

    import sys

    monkeypatch.setattr(
        sys, "argv", ["daily_scan.py", "--tickers", "AAA", "BBB", "--db", str(tmp_path / "t.db")]
    )
    daily_scan.main()

    out = capsys.readouterr().out
    assert "1/2 approved" in out
    assert "AAA: PULLBACK" in out

    scan_files = list((tmp_path / "data" / "scans").glob("scan_*.json"))
    assert len(scan_files) == 1
    saved = json.loads(scan_files[0].read_text(encoding="utf-8"))
    assert len(saved) == 2
    assert {r["ticker"] for r in saved} == {"AAA", "BBB"}


def test_skip_fetch_avoids_calling_fetch_functions(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    def _boom(*args, **kwargs):
        raise AssertionError("fetch function should not be called with --skip-fetch")

    monkeypatch.setattr(daily_scan, "fetch_and_store_prices", _boom)
    monkeypatch.setattr(daily_scan, "fetch_and_store_macro_series", _boom)
    monkeypatch.setattr(daily_scan, "get_verdict", lambda conn, ticker: _canned_reject(conn, ticker))

    import sys

    monkeypatch.setattr(
        sys, "argv",
        ["daily_scan.py", "--tickers", "AAA", "--skip-fetch", "--db", str(tmp_path / "t.db")],
    )
    daily_scan.main()  # would raise via _boom if --skip-fetch were ignored

    out = capsys.readouterr().out
    assert "0/1 approved" in out
