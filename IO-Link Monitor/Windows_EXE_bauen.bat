@echo off
rem Baut IO-Link-Monitor.exe (laeuft danach ohne installiertes Python, auch auf anderen Windows-PCs).
rem Braucht einmalig Python 3 und Internet (installiert PyInstaller per pip).
setlocal
cd /d "%~dp0"
title IO-Link-Monitor.exe bauen
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
  echo Python 3 wurde nicht gefunden - bitte von https://www.python.org/downloads/ installieren
  echo und dabei "Add python.exe to PATH" ankreuzen.
  pause
  exit /b 1
)
echo PyInstaller installieren/aktualisieren ...
%PY% -m pip install --user --upgrade pyinstaller || goto fehler
echo.
echo Baue IO-Link-Monitor.exe ...
%PY% -m PyInstaller --onefile --console --clean --noconfirm --name IO-Link-Monitor ^
  --distpath . --workpath build\pyinstaller --specpath build iolink_monitor.py || goto fehler
rmdir /s /q build 2>nul
echo.
echo Fertig: %~dp0IO-Link-Monitor.exe
echo Die .exe kann zusammen mit kalibrierung_temperatur.json / kalibrierung_induktiv.json in einen beliebigen Ordner kopiert werden.
pause
exit /b 0
:fehler
echo.
echo Bauen fehlgeschlagen - Meldungen oben pruefen.
pause
exit /b 1
