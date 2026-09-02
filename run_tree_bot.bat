@echo off
REM ---------------------------------------------------------------------------
REM Haifa tree-watch - scheduled launcher.
REM
REM Wired to the scheduled task "Haifa_Tree_Bot" since the September 2026
REM cutover. The legacy D:\storage\desktop\run_tree_bot.bat and firsd.py are no
REM longer used.
REM
REM Differences from the old Desktop copy, on purpose:
REM   * no "pause" - under Task Scheduler there is nobody to press a key
REM   * output is redirected to a log file
REM   * a non-zero exit code is propagated, so a failed run shows as failed
REM   * data\state.jsonl is backed up before every run
REM ---------------------------------------------------------------------------

setlocal

set "PROJECT=D:\adiof\Documents\trees_bot"
set "PYTHON=%PROJECT%\venv\Scripts\python.exe"
set "STATE=%PROJECT%\data\state.jsonl"
set "BACKUPS=%PROJECT%\data\backups"
set "KEEP=30"

REM The explicit cd is deliberate. The scheduled task's "start in" field points
REM at an old, abandoned copy of this bot; without this line the wrong folder
REM would be used. Do not remove it.
d:
cd /d "%PROJECT%" || exit /b 1

REM Must come before anything writes into logs\, including the error paths below.
if not exist "%PROJECT%\logs" mkdir "%PROJECT%\logs"

if not exist "%PYTHON%" (
    echo [%DATE% %TIME%] python not found at "%PYTHON%" >> "%PROJECT%\logs\run.log"
    exit /b 1
)

REM ---------------------------------------------------------------------------
REM Back up the state file BEFORE the run.
REM
REM state.jsonl is the only irreplaceable file here: if it is lost or truncated,
REM the next run treats every licence ever sent as new and floods the public
REM channel with months of old alerts. A copy costs nothing.
REM
REM PowerShell rather than pure batch because %DATE% formatting depends on the
REM machine's locale, and pruning old files in batch is fragile. Failure to back
REM up is logged but never stops the run - a missed backup is recoverable, a
REM missed alert is not.
REM ---------------------------------------------------------------------------
if exist "%STATE%" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$ErrorActionPreference='Stop';" ^
      "try {" ^
      "  New-Item -ItemType Directory -Force -Path '%BACKUPS%' | Out-Null;" ^
      "  $stamp = Get-Date -Format 'yyyy-MM-dd_HHmmss';" ^
      "  $dest  = Join-Path '%BACKUPS%' ('state.jsonl.' + $stamp + '.bak');" ^
      "  Copy-Item -LiteralPath '%STATE%' -Destination $dest -Force;" ^
      "  $src = (Get-Item -LiteralPath '%STATE%').Length;" ^
      "  $dst = (Get-Item -LiteralPath $dest).Length;" ^
      "  if ($src -ne $dst) { throw ('size mismatch: ' + $src + ' vs ' + $dst) };" ^
      "  Get-ChildItem -LiteralPath '%BACKUPS%' -Filter 'state.jsonl.*.bak' |" ^
      "    Sort-Object LastWriteTime -Descending | Select-Object -Skip %KEEP% |" ^
      "    Remove-Item -Force;" ^
      "  Write-Output ('state backup ok: ' + (Split-Path $dest -Leaf) + ' (' + $src + ' bytes)')" ^
      "} catch {" ^
      "  Write-Output ('STATE BACKUP FAILED: ' + $_.Exception.Message)" ^
      "}" >> "%PROJECT%\logs\run.log" 2>&1
) else (
    echo [%DATE% %TIME%] no state file to back up - first run, or migration pending >> "%PROJECT%\logs\run.log"
)

set "PYTHONPATH=%PROJECT%\src"
set "PYTHONIOENCODING=utf-8"

echo [%DATE% %TIME%] starting tree-watch >> "%PROJECT%\logs\run.log"
"%PYTHON%" -m tree_watch.main %* >> "%PROJECT%\logs\run.log" 2>&1
set "RC=%ERRORLEVEL%"
echo [%DATE% %TIME%] finished with exit code %RC% >> "%PROJECT%\logs\run.log"

endlocal & exit /b %RC%
