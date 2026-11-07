@echo off
REM Scheduled entry point: check the FK mailbox for new sales reports,
REM import any found, and refresh the dashboard + report.
cd /d "%~dp0"
py pipeline\email_ingest.py >> output\email_ingest.log 2>&1
