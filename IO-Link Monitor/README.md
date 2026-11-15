# IO-Link Monitor

Live-Anzeige, Kalibrierung und CSV-Export für IO-Link-Sensoren an einem **ifm AL1102** IO-Link-Master (PROFINET, IoT-Core-API).
Ansichten im Dashboard: **Temperatur** (autosen AT001), **Induktiv** (induktiver Sensor mit Abstandskalibrierung)
und **Verbindung** (Master-Status, IP wechseln, Ports, PC-Netzwerk). Startet auch ohne angeschlossenen Master.
Nur Python-Standardbibliothek, läuft auf Linux, macOS und Windows.

*Stand: 2026-11-15*

## Ziel und nächste Schritte

- IO-Link-Sensoren vom PC aus lesen, live anzeigen, kalibrieren, als CSV exportieren.
- **Nächster Schritt:** Wechsel vom Temperatursensor auf einen **induktiven Sensor**, mit möglichst hoher zeitlicher Auflösung (Ziel ~200 Hz).
- **200 Hz sind über die IoT-Core-API unmöglich** (max. ~16,7 Hz, siehe *Gemessene Grenzen*). Mögliche Wege, noch nicht entschieden:
  1. EtherCAT-Variante des ifm-Masters + SOEM/`pysoem` am Linux-PC (1-ms-Takt) – Empfehlung, wenn keine SPS da ist
  2. AL1102 über PROFINET an einer SPS (z. B. S7-1200) oder Software-SPS (CODESYS + PROFINET-Controller, Lizenz)
  3. Induktiver Sensor mit Analogausgang + USB-Messkarte
- **Offen:** genauer Typ des induktiven Sensors; ob „200 Hz“ die Schaltfrequenz oder die benötigte Messwertrate ist.

## Hardware und Netzwerk (verifiziert)

| | |
|---|---|
| Master | ifm **AL1102** (PROFINET), SN 000214715827, FW `AL1x0x_cn_pn_v2.3.23`, MAC 00:02:01:67:1F:67/68 |
| Master-IP | `169.254.33.228` / 255.255.0.0 (Link-Local, kein DHCP) |
| Sensor | autosen **AT001** (PT1000-Transmitter, -50 … 150 °C) an **Port 1**, Ports 2–8 leer |
| Rohwert AT001 | 2 Byte, int16 × 0,1 °C, Bereich -500 … 1500 (laut IODD bestätigt, auch bei Einheit °F bleibt der Rohwert in 0,1 °C) |
| PC (Linux) | Zorin OS, Ethernet `enp14s0` direkt am Master |

Der PC braucht eine Adresse im selben Netz, z. B. `169.254.33.10/16`:

- **Linux:** `nmcli dev modify enp14s0 ipv4.method manual ipv4.addresses 169.254.33.10/16`
  (gilt nur bis zum nächsten Neuverbinden). Der Monitor setzt sie automatisch, sobald der Master nicht erreichbar ist –
  aber nur an einem Ethernet-Anschluss mit Kabel, der **gar keine** IPv4-Adresse hat (abschaltbar im Reiter *Verbindung*).
- **macOS:** meist automatisch (selbst zugewiesene 169.254.x.x), sonst manuell unter Netzwerk → Adapter → TCP/IP.
- **Windows:** vergibt ohne DHCP nach ca. 1 Minute selbst eine 169.254.x.x-Adresse. Sonst als Administrator:
  `netsh interface ipv4 set address name="Ethernet" static 169.254.33.10 255.255.0.0`

IP des Masters unbekannt? Er sendet sie per LLDP: `sudo tcpdump -i <iface> -nn -vv -c 1 ether proto 0x88cc`

## Starten

| System | Starter |
|---|---|
| Linux | `Start_Linux.sh` – Rechtsklick → *Als Programm ausführen* (oder `./Start_Linux.sh`) |
| macOS | `Start_macOS.command` – Doppelklick im Finder |
| Windows mit Python | `Start_Windows.bat` – Doppelklick. Python 3 von python.org, bei Installation *Add python.exe to PATH* ankreuzen. |
| Windows ohne Python | `IO-Link-Monitor.exe` – Doppelklick. Vorher einmalig auf einem Windows-PC mit Python `Windows_EXE_bauen.bat` ausführen. |
| Direkt | `python3 iolink_monitor.py` |

- Alle Starter öffnen nur das Dashboard, wenn der Monitor schon läuft.
- Dashboard: http://localhost:8080 (Temperatur), …/#ind (Induktiv), …/#verb (Verbindung).
- Startet auch ohne Master; der Master kann später eingesteckt werden, der Monitor verbindet von selbst.
- Optionen (Starter reichen sie durch): `--master` (überschreibt die IP aus `verbindung.json`), `--port` (Temperatur, 0 = aus), `--induktiv-port`, `--intervall`, `--faktor`, `--web`, `--max`, `--kein-browser`.
- Windows: Das Konsolenfenster bleibt offen, solange der Monitor läuft. Schließen beendet ihn.
  Die `.exe` löst beim ersten Start evtl. SmartScreen aus („Weitere Informationen“ → „Trotzdem ausführen“).
  Einstellungen liegen neben der `.exe`; sie lässt sich mit den JSON-Dateien in jeden Ordner kopieren.
  Alte Dateinamen (`kalibrierung.json`, `induktiv.json`) werden beim Start automatisch umbenannt.

## Bedienung

### Allgemein
- Oben Umschalten zwischen **Temperatur** und **Induktiv**. Pro Ansicht: Port, Zeitfenster, Abtastung (60 ms … 10 s), Pause.
- **CSV exportieren:** `;`-getrennt, Dezimalkomma, UTF-8 mit BOM (Excel-tauglich), inkl. Rohwert-Hex.
- **Daten löschen** und **Beenden** brauchen einen zweiten Klick. Messwerte liegen nur im RAM – vor dem Beenden exportieren.

### Verbindung
- Kacheln: verbunden/getrennt (seit wann, Ursache), Antwortzeit, Werte/s, Abbrüche seit Start.
- **Master:** IP eintragen → *Verbinden* (wird in `verbindung.json` gespeichert). *Master suchen* prüft alle 169.254.x.x-Adressen
  aus der ARP-Tabelle des PCs, also Geräte, mit denen der PC schon Kontakt hatte; eine ganz unbekannte IP findet nur LLDP (siehe oben).
  Darunter Gerät, Seriennummer, Firmware.
- **Ports:** Modus, Status und Gerät aller 8 Ports (alle 5 s), mit Zuordnung zu den Ansichten.
- **PC-Netzwerk:** zeigt die 169.254.x.x-Adresse des PCs bzw. warnt, wenn keine da ist. Linux: automatische PC-IP an/aus,
  *PC-IP jetzt setzen*. Windows/macOS: Anleitung zum manuellen Setzen.
- **Ereignisprotokoll:** verbunden, getrennt (mit Ursache), IP gewechselt, PC-IP gesetzt.
- In den anderen Ansichten erscheint bei getrenntem Master ein roter Hinweis; Messwerte bleiben erhalten, der Graph zeigt eine Lücke.

### Temperatur
- Kalibrierung **1-Punkt** (Offset), **2-Punkt** (Offset + Steigung, Punkte ≥ 1 °C auseinander) oder **manuell** (T = Steigung × T_roh + Offset).
- Kalibrierpunkte nehmen den Mittelwert der letzten 5 s.
- **Kalibrieroffset im Sensor (coF):** Nullpunktkorrektur, die im AT001 selbst gespeichert wird (±10,0 °C, 0,1-°C-Schritte)
  und damit auch für SPS/andere Master und den 4–20-mA-Ausgang gilt. *Sensor abgleichen* (auf Referenz, Mittel 5 s),
  *direkt setzen* oder *auf 0* – jeweils mit zweitem Klick; danach wird zur Kontrolle zurückgelesen.
  Nur für autosen AT001–AT004 (Vendor 837, Device 6) freigegeben. Die Monitor-Kalibrierung wirkt zusätzlich obendrauf –
  sinnvoll ist meist nur eine von beiden.

### Induktiv
1. Sensor anstecken, oben den **Port** wählen.
2. **Prozessdaten dekodieren** (unten): Startbit, Bitlänge, Vorzeichen, Faktor, Einheit und Schaltbit laut Datenblatt/IODD
   (*ProcessDataIn*, Bit-Offset gezählt ab dem niederwertigsten Bit). Die Bitanzeige zeigt live, welche Bits gelesen werden
   (blau = Wert, orange = Schaltbit). Eine Dekodierung, die nicht in die Prozessdaten passt, wird abgelehnt.
3. **Abstandskalibrierung:** Messobjekt auf bekannte Abstände bringen (Fühlerlehre, Endmaß, Mikrometertisch),
   mm eintragen, *Punkt aufnehmen* – gespeichert wird der Mittelwert des Rohwerts über 1/2/5 s (mit Streuung σ).
   Punkte auch von Hand möglich. Ab 2 Punkten Anzeige in mm, besser 5–10 Punkte über den ganzen Messbereich.
   - Kennlinie *stückweise linear* (durch alle Punkte, passt zur gekrümmten Kennlinie induktiver Sensoren) oder *Ausgleichsgerade*.
   - Spalte *Abw. Gerade* zeigt die Linearität; daneben die Kennlinie mit dem aktuellen Wert.
   - Außerhalb der kalibrierten Punkte wird extrapoliert und gewarnt.
4. **Nullpunkt:** *Auf 0 setzen* zeigt Wege relativ zur aktuellen Position.

Kacheln: Abstand, Min, Max, Spanne, Rauschen σ; der Schaltausgang erscheint als grünes Band im Graph.
Eine Änderung von Startbit/Länge/Vorzeichen/Faktor löscht die Kalibrierpunkte (zweiter Klick nötig);
gespeicherte Messwerte werden dabei neu ausgewertet.

## Dateien

| Datei | Inhalt |
|---|---|
| `iolink_monitor.py` | Monitor: Webserver + Dashboard (HTML/JS eingebettet), beide Ansichten |
| `Start_Linux.sh` / `Start_macOS.command` / `Start_Windows.bat` | Starter Linux / macOS / Windows |
| `Windows_EXE_bauen.bat` | baut `IO-Link-Monitor.exe` mit PyInstaller (`--onefile`) |
| `iolink_auslesen.py` | einmalige Übersicht: Master-Infos und alle 8 Ports |
| `kalibrierung_temperatur.json` | Temperatur: Kalibrierung im Monitor (vom Monitor angelegt) |
| `kalibrierung_induktiv.json` | Induktiv: Port, Dekodierung, Abstandskalibrierung, Nullpunkt des induktiven Sensors (vom Monitor angelegt) |
| `verbindung.json` | Master-IP, PC-IP, automatische PC-IP an/aus (vom Monitor angelegt, sobald im Reiter *Verbindung* etwas geändert wird) |

## Technik

### ifm IoT Core API (HTTP POST JSON an `http://169.254.33.228/`)
- Einzelwert: `{"code":"request","cid":1,"adr":"/iolinkmaster/port[1]/iolinkdevice/pdin/getdata"}` → `data.value` (Hex-String)
- Mehrere: `adr:"/getdatamulti"`, `data:{"datatosend":[...]}` – Pfade **ohne** `/getdata`; leerer Port → Code 503
- ISDU lesen: `adr:"/iolinkmaster/port[1]/iolinkdevice/iolreadacyclic"`, `data:{"index":16,"subindex":0}`
  (16 = Hersteller, 18 = Produkt, 20 = Text, 21 = Seriennr.)
- Baum: `adr:"/gettree"`

### AT001 laut IODD (Vendor-ID 837, Device-ID 6, `autosen-000006-20160408-IODD1.1` aus dem [IODD-Finder](https://ioddfinder.io-link.com))
| Index | Parameter | Bedeutung | Wert am Sensor (2026-11-12) |
|---|---|---|---|
| 5001 | **coF** | Nullpunktkalibrierung (Kalibrieroffset), int16 × 0,1 °C, -100 … 100 (= ±10,0 °C) | `0000` = 0 (Werkseinstellung) |
| 551 | Uni | Einheit 0 = °C, 1 = °F | 0 = °C |
| 630 / 631 | ASP2 / AEP2 | Analogbereich Anfang/Ende (4–20 mA), × 0,1 °C | -50,0 / 150,0 °C |
| 590 | ou2 | Analogausgang | 1 |
| 532 | FOU2 | Verhalten bei Fehler | 4 |

- **Kalibrierung:** Der Sensor hat nur einen Nullpunkt-Offset (**coF**), keine Steigung. Er wirkt direkt auf die Prozessdaten
  und gilt damit für jedes Auslesegerät. Am Sensor ist keiner gesetzt, im Monitor ist ebenfalls keine Kalibrierung aktiv
  (`kalibrierung_temperatur.json`: Steigung 1, Offset 0). Laut Hersteller Genauigkeit < ±0,5 °C (PT1000 Klasse A).
- coF lesen/schreiben im Monitor: `iolreadacyclic` / `iolwriteacyclic` mit `{"index":5001,"subindex":0,"value":"FFF6"}`
  (int16 als Hex, hier -1,0 °C). Schreibbefehle antworten **ohne** `data`-Feld. Getestet nur mit Wert 0 (= unverändert).

### Gemessene Grenzen
- IO-Link-Zyklus Port 1: **3,9 ms** (COM2).
- IoT-Core-API: **max. ~16,7 Hz** (fest ~60 ms pro Anfrage mit Keep-Alive). `getdatamulti` mit 2 Ports: 64 ms.
- Parallele Verbindungen helfen nicht (2–8 Verbindungen: ~14,7 Hz + Fehler). Ohne Keep-Alive gelegentlich Connect-Timeouts von mehreren Sekunden.
- **Solange eine Keep-Alive-Verbindung offen ist (Monitor läuft), beantwortet der Master keine zweite Verbindung** (Timeout).
  Deshalb laufen im Monitor Polling und coF-Zugriffe über eine gemeinsame Verbindung mit Lock, und `iolink_auslesen.py`
  funktioniert nur, wenn der Monitor beendet ist.

### Aufbau von `iolink_monitor.py`
- Eine einzige Keep-Alive-Verbindung (`master`, mit Lock; Host im laufenden Betrieb änderbar). Ein Polling-Thread liest alle aktiven Ports in **einer** `getdatamulti`-Anfrage.
  Ansichten auf demselben Port teilen sich die Daten. Produktname wird nachgeladen (nach Start/Portwechsel/Umstecken).
- Pro Kanal (`temp`, `ind`) ein Ringpuffer `(ts, wert_unkalibriert, hex, schaltbit)`, max. 500 000 Werte. Ein Zähler `gen`
  signalisiert dem Browser „neu laden“ (Löschen, Portwechsel, neue Dekodierung).
- Kalibrierung wird angewandt bei Anzeige und CSV, gespeichert werden Rohwerte. Kennlinienfunktion existiert doppelt:
  `kal_funktion()` (Python, CSV) und `kalFunktion()` (JS, Anzeige) – bei Änderungen beide anpassen.
- Verbindungsüberwachung im Polling-Thread: auch ohne aktiven Port eine Anfrage (`/deviceinfo/productcode`), damit der Status stimmt.
  Zustand `vstat` + Protokoll `vlog`; bei getrenntem Master Wiederholung höchstens 1×/s, Linux-Auto-IP höchstens alle 10 s.
  Master-Infos und Port-Übersicht alle 5 s (eine `getdatamulti`-Anfrage mit 29 Pfaden).
- Starter übergeben keine `--master`-IP mehr (sonst würde die im Dashboard gewählte IP überschrieben) und setzen keine PC-IP mehr – das macht der Monitor.
- Temperatur-Port wird nicht gespeichert (`--port`), Induktiv-Port in `kalibrierung_induktiv.json`.
- Browser fragt im Takt der Abtastung inkrementell ab (`/api/daten?k=temp|ind&seit=…`); Graph zeichnet pro Pixelspalte Min/Max.
- Zwei-Klick-Bestätigung im Knopf, weil `confirm()` im eingebetteten Browser blockiert ist.
- Bei `sys.frozen` (PyInstaller) liegen die JSON-Dateien neben der `.exe`. Ist der Web-Port belegt, öffnet ein zweiter Start nur das Dashboard (Exit 1).
- `.bat`-Dateien bewusst nur ASCII + CRLF (cmd-Codepage).

## Status
- Auf Linux getestet: beide Ansichten, Temperatur-Kalibrierung (1-Punkt/manuell), CSV, Starter `Start_Linux.sh`, Doppelstart.
- Verbindung (2026-11-15): Start ohne Master, Anzeige/Protokoll, IP wechseln, Suche, *PC-IP jetzt setzen* (ohne Änderung) mit dem echten PC getestet;
  Abziehen/Wiedereinstecken nur mit simuliertem Master (lokaler Testserver), **nicht mit dem echten AL1102**. Auto-IP hat bisher nie etwas gesetzt.
- Induktiv-Ansicht nur mit dem AT001 an Port 1 als Ersatz getestet (Dekodierung, Kalibrierung, Nullpunkt, Schaltbit, CSV,
  Speichern über Neustart). **Noch kein induktiver Sensor angeschlossen**; `kalibrierung_induktiv.json` enthält Standardwerte (Port aus).
- Ungetestet: `Start_macOS.command` (macOS), `Start_Windows.bat` und `Windows_EXE_bauen.bat` (Windows), echte 2-Punkt-Kalibrierung.
- Kein Git-Repo.

## Arbeitsweise (für Claude)
- Deutsch, knapp. Befehle selbst ausführen statt den Nutzer tippen zu lassen; Sicherheitssperren nicht umgehen.
  Befehle mit `sudo` im Terminal-Tab starten, der Nutzer tippt das Passwort.
- Nach längeren Aufgaben (> ~3 min) PushNotification mit dem Ergebnis vorne.
