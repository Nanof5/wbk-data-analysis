#!/usr/bin/env bash
# Startet den IO-Link Monitor (Linux) und oeffnet das Dashboard im Browser.
# Startet auch ohne angeschlossenen Master; Verbindung, Master-IP und PC-IP siehe Reiter "Verbindung".
# Beenden ueber den "Beenden"-Knopf im Dashboard (oder Strg+C, falls im Terminal gestartet).

WEB=8080

cd "$(dirname "$(readlink -f "$0")")" || exit 1

# Laeuft er schon? Dann nur das Dashboard oeffnen.
if curl -s -m 2 "http://localhost:$WEB/api/daten?seit=9999999999" >/dev/null 2>&1; then
  xdg-open "http://localhost:$WEB" >/dev/null 2>&1 &
  exit 0
fi

exec python3 iolink_monitor.py --web "$WEB" "$@"
