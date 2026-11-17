# wbk-data-analysis: Kurzfassung für Claude

- Enthält den **IO-Link Monitor** (`IO-Link Monitor/`): Live-Anzeige, Kalibrierung und CSV-Export für IO-Link-Sensoren am ifm AL1102. Vollständige Doku und Technik: `IO-Link Monitor/README.md`.
- Nächster Schritt: induktiver Sensor mit ~200 Hz; über die IoT-Core-API sind nur ~16,7 Hz möglich (Wege siehe README).
- Läuft der Monitor, beantwortet der Master keine zweite Verbindung. Kennlinie gibt es doppelt (`kal_funktion()` Python, `kalFunktion()` JS): beide anpassen.
- Deutsch, knapp. Befehle selbst ausführen; `sudo` im Terminal-Tab starten, Niklas tippt das Passwort.

## Übergabe in Anytype
Projektstand im Anytype-Space „Claude Gedächtnis“: Übergabe „IO-Link Monitor: Stand, Hardware und Technik“ (Projekt „IO-Link Monitor (WBK)“). Am Ende größerer Aufgaben die Übergabe aktualisieren (`/uebergabe`), bei Änderungen am Stand auch die README.
