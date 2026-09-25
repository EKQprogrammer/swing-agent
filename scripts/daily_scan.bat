@echo off
REM Runs the daily swing-agent watchlist scan. Intended to be triggered by
REM Windows Task Scheduler (e.g. a daily trigger for ~17:00, after market close).
REM
REM To register it:
REM   1. Open Task Scheduler -> Create Task...
REM   2. Trigger: Daily, your preferred time.
REM   3. Action: Start a program -> Program/script: this .bat file's full path.
REM   4. "Start in" can be left blank; this script cd's to the project root itself.
REM
REM Output is written to data\scans\scan_<date>.json each run; see
REM scripts\daily_scan.py for --tickers / --json / --skip-fetch options.

cd /d "%~dp0.."
".venv\Scripts\python.exe" "scripts\daily_scan.py" %*
