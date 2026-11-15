#!/usr/bin/env bash
# Startet den IO-Link Monitor (macOS, per Doppelklick im Finder) und oeffnet das Dashboard.
# Startet auch ohne angeschlossenen Master; Verbindung und Master-IP siehe Reiter "Verbindung".
# Beenden ueber den "Beenden"-Knopf im Dashboard oder Strg+C im Terminalfenster.
# Der Mac braucht eine IP im Netz 169.254.x.x (meist automatisch, sonst manuell 169.254.33.10 / 255.255.0.0).

WEB=8080

cd "$(dirname "$0")" || exit 1

if curl -s -m 2 "http://localhost:$WEB/api/daten?seit=9999999999" >/dev/null 2>&1; then
  open "http://localhost:$WEB"
  exit 0
fi

exec python3 iolink_monitor.py --web "$WEB" "$@"
