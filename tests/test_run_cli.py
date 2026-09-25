from __future__ import annotations

import json
from pathlib import Path

import pytest

import run


def _canned_approve(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "as_of_date": "2024-06-01",
        "verdict": "APPROVE",
        "reject_layer": None,
        "macro": {"regime": "BULLISH"},
        "fundamental": {"verdict": "PASS"},
        "technical": {"setup": "PULLBACK"},
        "risk": {
            "entry": 100.0, "stop": 95.0, "shares": 20, "risk_dollars": 100.0,
            "targets": {"2R": 110.0, "3R": 115.0},
        },
    }


def _canned_reject(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "as_of_date": "2024-06-01",
        "verdict": "REJECT",
        "reject_layer": "fundamental",
        "reasoning": "Failed checks: roic.",
        "macro": {"regime": "BULLISH"},
        "fundamental": {"verdict": "REJECT"},
        "technical": None,
        "risk": None,
    }


def test_single_ticker_human_output(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(run, "get_verdict", lambda conn, ticker, as_of_date=None: _canned_approve(ticker))
    exit_code = run.main(["AAPL", "--db", str(tmp_path / "t.db")])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "AAPL - APPROVE" in out
    assert "Setup: PULLBACK" in out
    assert "Shares: 20" in out


def test_single_ticker_json_output(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(run, "get_verdict", lambda conn, ticker, as_of_date=None: _canned_approve(ticker))
    run.main(["AAPL", "--json", "--db", str(tmp_path / "t.db")])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert isinstance(parsed, dict)
    assert parsed["ticker"] == "AAPL"
    assert parsed["verdict"] == "APPROVE"


def test_scan_mode_processes_all_tickers(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(run, "get_verdict", lambda conn, ticker, as_of_date=None: _canned_reject(ticker))
    run.main(["--scan", "--tickers", "AAA", "BBB", "--db", str(tmp_path / "t.db")])
    out = capsys.readouterr().out
    assert "AAA - REJECT" in out
    assert "BBB - REJECT" in out


def test_scan_json_output_is_a_list(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(run, "get_verdict", lambda conn, ticker, as_of_date=None: _canned_reject(ticker))
    run.main(["--scan", "--tickers", "AAA", "BBB", "--json", "--db", str(tmp_path / "t.db")])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert len(parsed) == 2


def test_no_ticker_and_no_scan_errors(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run.main(["--db", str(tmp_path / "t.db")])


def test_exception_in_one_ticker_becomes_error_result_not_a_crash(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    def flaky_get_verdict(conn, ticker, as_of_date=None):
        if ticker == "BAD":
            raise RuntimeError("no price data")
        return _canned_reject(ticker)

    monkeypatch.setattr(run, "get_verdict", flaky_get_verdict)
    exit_code = run.main(["--scan", "--tickers", "BAD", "GOOD", "--json", "--db", str(tmp_path / "t.db")])
    assert exit_code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed[0]["verdict"] == "ERROR"
    assert parsed[1]["verdict"] == "REJECT"
