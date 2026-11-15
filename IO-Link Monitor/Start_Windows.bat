@echo off
rem Startet den IO-Link Monitor (Temperatur + Induktiv) unter Windows per Doppelklick
rem und oeffnet das Dashboard im Browser. Beenden ueber den "Beenden"-Knopf im Dashboard
rem oder dieses Fenster schliessen. Startet auch ohne Master (siehe Reiter "Verbindung").
rem Optionen werden durchgereicht, z. B.:
rem   Start_Windows.bat --induktiv-port 2 --intervall 0.06
setlocal
cd /d "%~dp0"
title IO-Link Monitor
set "WEB=8080"

rem --- Laeuft er schon? Dann nur das Dashboard oeffnen.
curl -s -m 2 -o nul "http://localhost:%WEB%/api/daten?seit=9999999999" >nul 2>&1
if not errorlevel 1 (
  start "" "http://localhost:%WEB%"
  exit /b 0
)

rem --- Python suchen (py-Launcher bevorzugt; "python" kann der Microsoft-Store-Platzhalter sein)
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
  echo Python 3 wurde nicht gefunden.
  echo Bitte von https://www.python.org/downloads/ installieren
  echo und dabei "Add python.exe to PATH" ankreuzen. Danach diese Datei erneut starten.
  echo Alternative ohne Python: IO-Link-Monitor.exe ^(siehe README^).
  pause
  exit /b 1
)

%PY% iolink_monitor.py --web %WEB% %*
if errorlevel 1 pause
