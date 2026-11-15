#!/usr/bin/env python3
"""IO-Link-Monitor fuer einen ifm IO-Link-Master (IoT Core API) mit zwei Ansichten:

  * Temperatur – Temperatursensor (z. B. autosen AT001), 1-/2-Punkt-Kalibrierung
  * Induktiv   – induktiver Sensor, frei einstellbare Dekodierung der Prozessdaten,
                 Mehrpunkt-Abstandskalibrierung (Rohwert -> mm), Schaltausgang, Nullpunkt

Beide Ports werden in EINER Anfrage (getdatamulti) gelesen. Live-Graph, CSV-Export.
Nur Python-Standardbibliothek, laeuft auf Linux und macOS.

    python3 iolink_monitor.py
    python3 iolink_monitor.py --master 169.254.33.228 --port 1 --induktiv-port 2 --intervall 0.06

Danach http://localhost:8080 (Temperatur) bzw. http://localhost:8080/#ind (Induktiv).
Einstellungen: kalibrierung_temperatur.json, kalibrierung_induktiv.json neben diesem Skript.
"""
import argparse
import bisect
import csv
import http.client
import io
import ipaddress
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser(description="IO-Link Monitor (Temperatur + induktiver Sensor)")
ap.add_argument("--master", default=None,
                help="IP des IO-Link-Masters (Standard: aus verbindung.json bzw. 169.254.33.228; im Browser aenderbar)")
ap.add_argument("--port", type=int, default=1, help="IO-Link-Port des Temperatursensors (0 = aus)")
ap.add_argument("--induktiv-port", type=int, default=None,
                help="IO-Link-Port des induktiven Sensors (0 = aus; Standard: aus kalibrierung_induktiv.json)")
ap.add_argument("--intervall", type=float, default=1.0, help="Abfrageintervall in Sekunden (im Browser aenderbar)")
ap.add_argument("--faktor", type=float, default=0.1, help="Skalierung Rohwert -> Grad C (Temperatur)")
ap.add_argument("--web", type=int, default=8080, help="Port der lokalen Weboberflaeche")
ap.add_argument("--max", type=int, default=500_000, help="max. gespeicherte Messwerte pro Sensor")
ap.add_argument("--kein-browser", action="store_true", help="Browser nicht automatisch oeffnen")
args = ap.parse_args()

# Einstellungen neben dem Skript bzw. neben der .exe (PyInstaller entpackt __file__ in einen Temp-Ordner)
HIER = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
KAL_DATEI = os.path.join(HIER, "kalibrierung_temperatur.json")
IND_DATEI = os.path.join(HIER, "kalibrierung_induktiv.json")
for _alt, _neu in (("kalibrierung.json", KAL_DATEI), ("induktiv.json", IND_DATEI)):  # alte Dateinamen uebernehmen
    _alt = os.path.join(HIER, _alt)
    if os.path.exists(_alt) and not os.path.exists(_neu):
        os.replace(_alt, _neu)
INTERVALLE = [0.06, 0.1, 0.2, 0.5, 1, 2, 5, 10]  # 60 ms ~ Maximum des AL1102 (gemessen)

lock = threading.Lock()
cfg = {"intervall": args.intervall}
status = {"rate_ist": 0.0, "ok": False, "fehler": "noch keine Daten"}

# ---------------------------------------------------------------- Verbindung (Einstellungen + Zustand)
VERB_DATEI = os.path.join(HIER, "verbindung.json")
VERB_STANDARD = {"master": "169.254.33.228", "pc_ip": "169.254.33.10/16", "auto_ip": True}


def verb_laden():
    v = dict(VERB_STANDARD)
    try:
        with open(VERB_DATEI, encoding="utf-8") as f:
            v.update({k: w for k, w in json.load(f).items() if k in VERB_STANDARD})
    except Exception:
        pass
    return v


verb = verb_laden()
if args.master:
    verb["master"] = args.master
vstat = {"verbunden": False, "seit": None, "abbrueche": 0, "latenz_ms": None, "fehler": "noch kein Versuch",
         "info": {}, "ports": []}
vlog = deque(maxlen=100)  # (unix_ts, art: ok|fehler|info, text)


def vlog_add(art, text):
    vlog.append((time.time(), art, text))


def jetzt_iso():
    return datetime.now().isoformat(timespec="seconds")


def json_schreiben(pfad, obj):
    with open(pfad, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- Kanaele
class Kanal:
    """Ein Sensor an einem Port: Messwerte (ts, wert_unkalibriert, hex, schaltbit|None) + Status."""

    def __init__(self, kid, port):
        self.kid, self.port = kid, port
        self.samples = deque(maxlen=args.max)
        self.gen = 0  # zaehlt hoch bei "Daten loeschen", Portwechsel, neuer Dekodierung
        self.sensor, self.ok, self.fehler, self.fehlversuche = "", False, "noch keine Daten", 0
        self.letzter_hex, self.name_t = "", 0.0

    def leeren(self):
        with lock:
            self.samples.clear()
            self.gen += 1

    def status(self):
        return {"ok": self.ok, "fehler": self.fehler, "sensor": self.sensor, "port": self.port,
                "fehlversuche": self.fehlversuche, "hex": self.letzter_hex}

    def mittel(self, sekunden):
        """Mittelwert, Standardabweichung und Anzahl der unkalibrierten Werte der letzten Sekunden."""
        with lock:
            if not self.samples:
                return None
            t_end = self.samples[-1][0]
            werte = []
            for s in reversed(self.samples):
                if s[0] < t_end - sekunden:
                    break
                werte.append(s[1])
        m = sum(werte) / len(werte)
        sd = math.sqrt(sum((w - m) ** 2 for w in werte) / (len(werte) - 1)) if len(werte) > 1 else 0.0
        return m, sd, len(werte)


# ---------------------------------------------------------------- Temperatur
def kal_laden():
    try:
        with open(KAL_DATEI, encoding="utf-8") as f:
            k = json.load(f)
        return {"steigung": float(k["steigung"]), "offset": float(k["offset"]),
                "geaendert": k.get("geaendert", ""), "punkte": k.get("punkte", [])}
    except Exception:
        return {"steigung": 1.0, "offset": 0.0, "geaendert": "", "punkte": []}


def kal_speichern(k):
    k["geaendert"] = jetzt_iso()
    json_schreiben(KAL_DATEI, k)


kal = kal_laden()


def dekodiere_temp(hexwert):
    raw = int(hexwert[:4], 16)  # erste 16 Bit, vorzeichenbehaftet
    if raw >= 0x8000:
        raw -= 0x10000
    return round(raw * args.faktor, 3), None


# ---------------------------------------------------------------- Induktiv
IND_STANDARD = {
    "port": 0,
    # Bit-Offset wie in der IODD: gezaehlt ab dem niederwertigsten Bit der gesamten Prozessdaten
    "dekoder": {"bitoffset": 0, "bitlaenge": 16, "signed": False, "faktor": 1.0, "einheit": "digits",
                "schaltbit": -1},
    "kal": {"modus": "stueckweise", "punkte": [], "tara": None, "geaendert": ""},
}


def ind_laden():
    k = json.loads(json.dumps(IND_STANDARD))
    try:
        with open(IND_DATEI, encoding="utf-8") as f:
            d = json.load(f)
        k["port"] = int(d.get("port", 0))
        k["dekoder"].update(d.get("dekoder", {}))
        k["kal"].update(d.get("kal", {}))
    except Exception:
        pass
    return k


ind = ind_laden()
if args.induktiv_port is not None:
    ind["port"] = args.induktiv_port


def ind_speichern():
    json_schreiben(IND_DATEI, ind)


def dekoder_pruefen(d):
    d = {"bitoffset": int(d["bitoffset"]), "bitlaenge": int(d["bitlaenge"]), "signed": bool(d["signed"]),
         "faktor": float(d["faktor"]), "einheit": str(d.get("einheit", "")).strip()[:20],
         "schaltbit": int(d.get("schaltbit", -1))}
    if not 0 <= d["bitoffset"] <= 255 or not 1 <= d["bitlaenge"] <= 64:
        raise ValueError("Startbit 0…255, Bitlänge 1…64")
    if d["faktor"] == 0 or not math.isfinite(d["faktor"]):
        raise ValueError("Faktor darf nicht 0 sein")
    return d


def dekodiere_ind(hexwert, d=None):
    d = d or ind["dekoder"]
    if not hexwert:
        raise ValueError("Sensor liefert keine Prozessdaten")
    v, n = int(hexwert, 16), len(hexwert) * 4
    lo, ln = d["bitoffset"], d["bitlaenge"]
    if lo + ln > n:
        raise ValueError(f"Prozessdaten sind nur {n} Bit lang – Startbit/Bitlänge anpassen")
    x = (v >> lo) & ((1 << ln) - 1)
    if d["signed"] and x >> (ln - 1):
        x -= 1 << ln
    sb = d["schaltbit"]
    return round(x * d["faktor"], 6), ((v >> sb) & 1 if 0 <= sb < n else None)


def kal_funktion(k):
    """Rohwert -> mm aus den Kalibrierpunkten, oder None (weniger als 2 verschiedene Rohwerte)."""
    gruppen = {}
    for p in k.get("punkte", []):
        gruppen.setdefault(p["roh"], []).append(p["mm"])
    pts = sorted((r, sum(m) / len(m)) for r, m in gruppen.items())
    if len(pts) < 2:
        return None
    if k.get("modus") == "linear":  # Ausgleichsgerade (kleinste Quadrate)
        mx = sum(p[0] for p in pts) / len(pts)
        my = sum(p[1] for p in pts) / len(pts)
        sxx = sum((p[0] - mx) ** 2 for p in pts)
        a = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx
        return lambda r: a * (r - mx) + my
    xs = [p[0] for p in pts]

    def stueckweise(r):  # lineare Interpolation, ausserhalb mit erstem/letztem Segment extrapoliert
        i = min(max(bisect.bisect_right(xs, r) - 1, 0), len(pts) - 2)
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        return y0 + (r - x0) * (y1 - y0) / (x1 - x0)
    return stueckweise


kanaele = {"temp": Kanal("temp", args.port), "ind": Kanal("ind", ind["port"])}
DEKODER = {"temp": dekodiere_temp, "ind": dekodiere_ind}


def ind_neu_dekodieren():
    """Nach Aenderung der Dekodierung alle gespeicherten Rohdaten (Hex) neu auswerten."""
    k = kanaele["ind"]
    with lock:
        alt = list(k.samples)
        k.samples.clear()
        for ts, _, h, _ in alt:
            try:
                w, s = dekodiere_ind(h)
            except ValueError:
                continue
            k.samples.append((ts, w, h, s))
        k.gen += 1


# ---------------------------------------------------------------- Master-Abfrage
class Master:
    """Haelt eine Keep-Alive-Verbindung zum Master; baut sie bei Fehlern neu auf."""

    def __init__(self, host):
        self.host, self.conn = host, None
        # Der AL1102 bedient keine zweite Verbindung, solange eine Keep-Alive-Verbindung offen ist
        # (gemessen: Timeout). Deshalb teilen sich Polling und coF-Zugriffe diese eine Verbindung.
        self.lock = threading.RLock()

    def request(self, *a, **kw):
        with self.lock:
            return self._request(*a, **kw)

    def _request(self, adr, data=None, timeout=2.0):
        b = {"code": "request", "cid": 1, "adr": adr}
        if data is not None:
            b["data"] = data
        body = json.dumps(b)
        for versuch in (1, 2):
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPConnection(self.host, 80, timeout=timeout)
                self.conn.request("POST", "/", body, {"Content-Type": "application/json"})
                resp = json.loads(self.conn.getresponse().read())
                if resp.get("code") != 200:
                    raise RuntimeError(f"Master antwortet mit Code {resp.get('code')}")
                return resp.get("data")  # Schreibbefehle antworten ohne "data"
            except (OSError, http.client.HTTPException, ValueError):
                self.conn.close() if self.conn else None
                self.conn = None
                if versuch == 2:
                    raise


# ---------------------------------------------------------------- coF (Kalibrieroffset im AT001-AT004)
# Laut IODD autosen-000006-20160408-IODD1.1: Index 5001, int16, -100..100 in 0,1 °C (= ±10 °C), nur Nullpunkt.
COF_INDEX, COF_MIN, COF_MAX = 5001, -100, 100
COF_GERAET = (837, 6)  # Vendor-ID autosen, Device-ID AT001-AT004
master = Master(verb["master"])  # einzige Verbindung zum Master (Polling + coF)


def cof_pruefe_geraet(port):
    if not port:
        raise ValueError("Temperatur-Port ist aus")
    b = f"/iolinkmaster/port[{port}]/iolinkdevice"
    ids = master.request("/getdatamulti", {"datatosend": [b + "/vendorid", b + "/deviceid"]})
    ven, dev = (ids.get(b + x, {}).get("data") for x in ("/vendorid", "/deviceid"))
    if (ven, dev) != COF_GERAET:
        raise ValueError(f"Gerät an Port {port} ist kein autosen AT001–AT004 (Vendor {ven}, Device {dev}) – coF nicht verfügbar")
    return b


def cof_lesen(port):
    with master.lock:
        b = cof_pruefe_geraet(port)
        h = master.request(b + "/iolreadacyclic", {"index": COF_INDEX, "subindex": 0})["value"]
    raw = int(h, 16)
    return (raw - 0x10000 if raw >= 0x8000 else raw) / 10


def cof_schreiben(port, grad):
    raw = round(grad * 10)
    if not COF_MIN <= raw <= COF_MAX:
        raise ValueError(f"coF muss zwischen {COF_MIN / 10:+.1f} und {COF_MAX / 10:+.1f} °C liegen (berechnet: {raw / 10:+.1f} °C)")
    with master.lock:
        b = cof_pruefe_geraet(port)
        master.request(b + "/iolwriteacyclic", {"index": COF_INDEX, "subindex": 0, "value": f"{raw & 0xFFFF:04X}"})
    zurueck = cof_lesen(port)  # zur Kontrolle zuruecklesen
    if round(zurueck * 10) != raw:
        raise RuntimeError(f"Sensor meldet nach dem Schreiben {zurueck:+.1f} °C statt {raw / 10:+.1f} °C")
    return zurueck


def fehlertext(e):
    t = str(e) or e.__class__.__name__
    lt = t.lower()
    if "timed out" in lt:
        return "keine Antwort (Zeitüberschreitung) – Kabel, Strom und IP prüfen"
    if "network is unreachable" in lt or "errno 101" in lt or "10051" in lt or "errno 51" in lt:
        return "Netz nicht erreichbar – der PC hat keine passende IP (169.254.x.x)"
    if "no route" in lt or "host is down" in lt or "unreachable" in lt or "errno 113" in lt or "10065" in lt:
        return "Master antwortet nicht – Strom, Kabel und IP prüfen"
    if "refused" in lt or "10061" in lt:
        return "Verbindung abgelehnt – ist das wirklich ein ifm-Master?"
    return t


def verbindung_ok():
    """Gibt True zurueck, wenn die Verbindung gerade (wieder) hergestellt wurde."""
    vstat["fehler"] = ""
    if vstat["verbunden"]:
        return False
    vstat.update(verbunden=True, seit=time.time())
    vlog_add("ok", f"Verbunden mit Master {master.host}")
    return True


def verbindung_fehler(text):
    if vstat["verbunden"]:
        vstat["abbrueche"] += 1
        vstat.update(verbunden=False, seit=time.time(), latenz_ms=None)
        vlog_add("fehler", f"Verbindung zu {master.host} getrennt: {text}")
    elif vstat["seit"] is None or vstat["fehler"] in ("", "noch kein Versuch"):
        vstat["seit"] = vstat["seit"] or time.time()
        vlog_add("fehler", f"Master {master.host} nicht erreichbar: {text}")
    elif text != vstat["fehler"]:
        vlog_add("fehler", text)
    vstat["fehler"] = text


PORT_MODI = {0: "aus", 1: "DI", 2: "DO", 3: "IO-Link"}
PORT_STATUS = {0: "kein Gerät", 1: "Preoperate", 2: "Operate", 3: "Kommunikationsfehler"}


def master_info_laden():
    """Geraeteinfos und Port-Uebersicht des Masters (eine getdatamulti-Anfrage)."""
    felder = ("vendor", "productcode", "serialnumber", "swrevision", "hwrevision")
    pf = [f"/deviceinfo/{x}" for x in felder]
    for p in range(1, 9):
        b = f"/iolinkmaster/port[{p}]"
        pf += [b + "/mode", b + "/iolinkdevice/status", b + "/iolinkdevice/productname"]
    try:
        d = master.request("/getdatamulti", {"datatosend": pf})
    except Exception:
        return

    def g(k):
        e = d.get(k) or {}
        return e.get("data") if e.get("code") == 200 else None
    vstat["info"] = {x: g(f"/deviceinfo/{x}") for x in felder}
    ports = []
    for p in range(1, 9):
        b = f"/iolinkmaster/port[{p}]"
        mo, st = g(b + "/mode"), g(b + "/iolinkdevice/status")
        ports.append({"port": p, "modus": PORT_MODI.get(mo, mo), "status": PORT_STATUS.get(st, st),
                      "geraet": g(b + "/iolinkdevice/productname") or ""})
    vstat["ports"] = ports


def _run(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


_pc_cache = {"t": 0.0, "ips": []}


def pc_linklocal():
    """Link-Local-Adressen (169.254.x.x) dieses PCs – nur als Hinweis fuer die Anzeige."""
    if time.time() - _pc_cache["t"] < 3:
        return _pc_cache["ips"]
    if sys.platform.startswith("linux"):
        cmd = ["ip", "-4", "-o", "addr"]
    elif sys.platform == "win32":
        cmd = ["ipconfig"]
    else:
        cmd = ["ifconfig"]
    _, out = _run(cmd, 3)
    ips = sorted({ip for ip in re.findall(r"169\.254\.\d{1,3}\.\d{1,3}", out) if not ip.endswith(".255.255")})
    _pc_cache.update(t=time.time(), ips=ips)
    return ips


def auto_ip(erzwingen=False):
    """Linux: Ethernet-Anschluss mit Kabel, aber ganz ohne IPv4-Adresse, bekommt die PC-IP (wie Start_Linux.sh).
    Anschluesse mit einer Adresse (z. B. Firmennetz per DHCP) werden nie veraendert."""
    if not sys.platform.startswith("linux"):
        return "Automatisch nur unter Linux – unter Windows/macOS die IP von Hand setzen (siehe Hinweis)."
    if not erzwingen and (not verb["auto_ip"] or not verb["master"].startswith("169.254.")):
        return None
    if not shutil.which("nmcli"):
        return "nmcli (NetworkManager) nicht gefunden – IP bitte von Hand setzen."
    _, out = _run(["nmcli", "-t", "-f", "DEVICE,TYPE", "dev"])
    geaendert, gruende = [], []
    for zeile in out.splitlines():
        dev, _, typ = zeile.partition(":")
        if typ != "ethernet":
            continue
        try:
            with open(f"/sys/class/net/{dev}/carrier") as f:
                kabel = f.read().strip() == "1"
        except OSError:
            kabel = False
        if not kabel:
            gruende.append(f"{dev}: kein Kabel/Link")
            continue
        _, addr = _run(["ip", "-4", "-o", "addr", "show", "dev", dev])
        vorhanden = re.findall(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", addr)
        if vorhanden:
            gruende.append(f"{dev}: hat schon {', '.join(vorhanden)} – nicht verändert")
            continue
        rc, msg = _run(["nmcli", "dev", "modify", dev, "ipv4.method", "manual", "ipv4.addresses", verb["pc_ip"]], 10)
        if rc == 0:
            geaendert.append(dev)
            vlog_add("info", f"PC-IP {verb['pc_ip']} an {dev} gesetzt")
        else:
            vlog_add("fehler", f"PC-IP an {dev} setzen fehlgeschlagen: {msg.strip()[:200]}")
            gruende.append(f"{dev}: Fehler – {msg.strip()[:120]}")
    _pc_cache["t"] = 0.0
    if geaendert:
        return f"PC-IP {verb['pc_ip']} gesetzt an: {', '.join(geaendert)}"
    return ("Nichts geändert. " + "; ".join(gruende)) if gruende else "Kein Ethernet-Anschluss gefunden."


def master_setzen(ip):
    ip = str(ipaddress.IPv4Address(ip.strip()))
    with master.lock:
        if master.conn:
            master.conn.close()
        master.conn, master.host = None, ip
    verb["master"] = ip
    json_schreiben(VERB_DATEI, verb)
    vstat.update(verbunden=False, seit=None, latenz_ms=None, fehler="noch kein Versuch", info={}, ports=[])
    for k in kanaele.values():
        k.sensor, k.name_t = "", 0.0
    vlog_add("info", f"Master-IP auf {ip} gesetzt – verbinde …")
    return ip


def master_suchen():
    """Probiert alle 169.254.x.x-Adressen aus der Nachbartabelle (ARP) des PCs und die eingestellte IP.
    Findet Master, mit denen der PC schon Kontakt hatte; eine unbekannte IP findet nur LLDP (root)."""
    eigene = set(pc_linklocal())
    kand = {verb["master"]}
    for cmd in (["ip", "neigh"], ["arp", "-a"]):
        rc, out = _run(cmd, 3)
        if rc == 0:
            kand |= {ip for ip in re.findall(r"169\.254\.\d{1,3}\.\d{1,3}", out) if not ip.endswith(".255.255")}
    kand -= eigene
    gefunden = []
    pf = ["/deviceinfo/vendor", "/deviceinfo/productcode", "/deviceinfo/serialnumber", "/deviceinfo/swrevision"]

    def probe(ip):
        if ip == master.host and vstat["verbunden"]:  # zweite Verbindung wuerde der Master nicht bedienen
            gefunden.append({"ip": ip, "aktiv": True, **{k: vstat["info"].get(k) for k in
                                                        ("vendor", "productcode", "serialnumber", "swrevision")}})
            return
        c = http.client.HTTPConnection(ip, 80, timeout=1.5)
        try:
            c.request("POST", "/", json.dumps({"code": "request", "cid": 1, "adr": "/getdatamulti",
                                               "data": {"datatosend": pf}}), {"Content-Type": "application/json"})
            d = json.loads(c.getresponse().read()).get("data") or {}
            gefunden.append({"ip": ip, "aktiv": ip == master.host,
                             **{x.split("/")[-1]: (d.get(x) or {}).get("data") for x in pf}})
        except Exception:
            pass
        finally:
            c.close()
    th = [threading.Thread(target=probe, args=(ip,), daemon=True) for ip in sorted(kand)]
    for t in th:
        t.start()
    for t in th:
        t.join(4)
    return sorted(gefunden, key=lambda x: x["ip"]), sorted(kand)


def poll_loop():
    m = master
    zeiten = deque(maxlen=50)
    info_t = ip_t = 0.0
    while True:
        t0 = time.time()
        aktiv = [k for k in kanaele.values() if k.port]
        pfade = {k.port: f"/iolinkmaster/port[{k.port}]/iolinkdevice/pdin" for k in aktiv}
        try:
            # ohne aktiven Port trotzdem anfragen, damit die Verbindung ueberwacht wird
            antw = m.request("/getdatamulti", {"datatosend": list(pfade.values()) or ["/deviceinfo/productcode"]})
            lat = (time.time() - t0) * 1000
            vstat["latenz_ms"] = round(lat if vstat["latenz_ms"] is None else 0.8 * vstat["latenz_ms"] + 0.2 * lat, 1)
            if verbindung_ok():
                info_t = 0.0
            for k in aktiv:
                if k.port not in pfade:  # Port wurde waehrend dieser Runde umgestellt
                    continue
                e = antw.get(pfade[k.port], {})
                if e.get("code") == 200:
                    hexwert = e.get("data") or ""
                    k.letzter_hex = hexwert
                    try:
                        w, s = DEKODER[k.kid](hexwert)
                    except ValueError as ex:
                        k.ok, k.fehler = False, str(ex)
                        continue
                    with lock:
                        k.samples.append((t0, w, hexwert, s))
                    k.ok, k.fehler = True, ""
                else:
                    k.fehlversuche += 1
                    k.ok, k.sensor, k.letzter_hex = False, "", ""
                    k.fehler = (f"kein IO-Link-Gerät an Port {k.port}" if e.get("code") == 503
                                else f"Port {k.port}: Code {e.get('code')}")
            if aktiv:
                zeiten.append(t0)
                if len(zeiten) > 1:
                    status["rate_ist"] = round((len(zeiten) - 1) / (zeiten[-1] - zeiten[0]), 2)
            else:
                zeiten.clear()
                status["rate_ist"] = 0.0
            status.update(ok=True, fehler="" if aktiv else "kein Port ausgewählt")
        except Exception as e:
            text = fehlertext(e)
            verbindung_fehler(text)
            status.update(ok=False, fehler=text, rate_ist=0.0)
            zeiten.clear()
            for k in aktiv:
                k.fehlversuche += 1
                k.ok, k.fehler = False, text
        if vstat["verbunden"]:
            # Produktname nachladen (nach Start, Portwechsel oder Umstecken), hoechstens alle 5 s
            for k in aktiv:
                if k.ok and not k.sensor and time.time() - k.name_t > 5:
                    k.name_t = time.time()
                    try:
                        k.sensor = m.request(f"/iolinkmaster/port[{k.port}]/iolinkdevice/productname/getdata")["value"]
                    except Exception:
                        pass
            if time.time() - info_t > 5:  # Master-Infos und Port-Uebersicht
                info_t = time.time()
                master_info_laden()
        elif time.time() - ip_t > 10:  # Linux: PC-IP nach Ab-/Anstecken automatisch neu setzen
            ip_t = time.time()
            auto_ip()
        pause = cfg["intervall"] if vstat["verbunden"] else max(cfg["intervall"], 1.0)
        time.sleep(max(0.0, pause - (time.time() - t0)))


# ---------------------------------------------------------------- CSV
def dez(x, d=3):
    return "" if x is None else f"{x:.{d}f}".replace(".", ",")


def csv_bytes(kid):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    with lock:
        rows = list(kanaele[kid].samples)
    if kid == "temp":
        k = dict(kal)
        w.writerow(["zeitstempel", "unix_zeit", "temperatur_c", "temperatur_unkalibriert_c", "rohwert_hex",
                    f"kalibrierung: T = {k['steigung']:.6f} * T_roh + {k['offset']:.4f}"])
        for ts, wert, hexwert, _ in rows:
            w.writerow([datetime.fromtimestamp(ts).isoformat(timespec="milliseconds"), f"{ts:.3f}",
                        dez(k["steigung"] * wert + k["offset"]), dez(wert), hexwert])
    else:
        d, kk = dict(ind["dekoder"]), json.loads(json.dumps(ind["kal"]))
        f, tara = kal_funktion(kk), kk.get("tara") or 0.0
        roh_name = "rohwert_" + (d["einheit"] or "digits").replace(" ", "_")
        info = (f"dekodierung: bit {d['bitoffset']}+{d['bitlaenge']} {'signed' if d['signed'] else 'unsigned'}"
                f" x {d['faktor']:g}")
        if f:
            info += (f"; kalibrierung: {'Ausgleichsgerade' if kk['modus'] == 'linear' else 'stückweise linear'}, "
                     f"{len(kk['punkte'])} Punkte" + (f"; nullpunkt bei {tara:.4f} mm" if kk.get("tara") is not None else ""))
        kopf = ["zeitstempel", "unix_zeit"] + (["abstand_mm"] if f else []) + [roh_name, "schaltausgang", "rohwert_hex", info]
        w.writerow(kopf)
        for ts, wert, hexwert, s in rows:
            w.writerow([datetime.fromtimestamp(ts).isoformat(timespec="milliseconds"), f"{ts:.3f}"]
                       + ([dez(f(wert) - tara, 4)] if f else [])
                       + [dez(wert, 6).rstrip("0").rstrip(","), "" if s is None else s, hexwert])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM -> Excel erkennt UTF-8


# ---------------------------------------------------------------- Webserver
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code=200):
        self.send(code, "application/json", json.dumps(obj).encode())

    def do_GET(self):
        path, _, query = self.path.partition("?")
        q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        kid = q.get("k", "temp") if q.get("k") in kanaele else "temp"
        k = kanaele[kid]
        if path == "/":
            self.send(200, "text/html; charset=utf-8", HTML.encode())
        elif path == "/api/daten":
            seit = float(q.get("seit", 0) or 0)
            with lock:
                if kid == "ind":
                    neu = [[round(s[0], 3), s[1], s[3]] for s in k.samples if s[0] > seit]
                else:
                    neu = [[round(s[0], 3), s[1]] for s in k.samples if s[0] > seit]
            self.json({"neu": neu, "gen": k.gen, "status": k.status(), "rate_ist": status["rate_ist"],
                       "master": master.host, "master_ok": vstat["verbunden"], "master_fehler": vstat["fehler"],
                       "intervall": cfg["intervall"], "intervalle": INTERVALLE,
                       "cfg": {"kal": kal} if kid == "temp" else {"dekoder": ind["dekoder"], "kal": ind["kal"]}})
        elif path == "/api/verbindung":
            self.json({"verbindung": vstat, "master": master.host, "rate_ist": status["rate_ist"],
                       "log": [list(x) for x in reversed(vlog)], "os": sys.platform, "auto_ip": verb["auto_ip"],
                       "pc_ip": verb["pc_ip"], "pc_linklocal": pc_linklocal(),
                       "kanal_ports": {k: c.port for k, c in kanaele.items()}})
        elif path == "/api/temp/cof":
            try:
                self.json({"ok": True, "cof": cof_lesen(kanaele["temp"].port), "zeit": jetzt_iso()})
            except Exception as e:
                self.json({"ok": False, "fehler": str(e) or e.__class__.__name__})
        elif path == "/export.csv":
            name = datetime.now().strftime(("temperatur" if kid == "temp" else "induktiv") + "_%Y-%m-%d_%H-%M-%S.csv")
            self.send(200, "text/csv; charset=utf-8", csv_bytes(kid),
                      {"Content-Disposition": f'attachment; filename="{name}"'})
        else:
            self.send(404, "text/plain", b"nicht gefunden")

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self.json({"fehler": "ungueltiges JSON"}, 400)
        try:
            return self.post(self.path, body)
        except (KeyError, TypeError, ValueError) as e:
            return self.json({"fehler": f"ungültige Eingabe: {e}"}, 400)

    def post(self, path, body):
        global kal
        kid = body.get("k") if body.get("k") in kanaele else "temp"
        if path == "/api/reset":
            kanaele[kid].leeren()
            return self.json({"ok": True})
        if path == "/api/beenden":
            self.json({"ok": True})
            threading.Thread(target=srv.shutdown, daemon=True).start()
            return
        if path == "/api/intervall":
            cfg["intervall"] = max(0.02, float(body["intervall"]))
            return self.json({"ok": True})
        if path == "/api/port":
            p = int(body["port"])
            if not 0 <= p <= 8:
                return self.json({"fehler": "Port 0…8"}, 400)
            k = kanaele[kid]
            if p != k.port:
                k.port, k.sensor, k.letzter_hex, k.name_t = p, "", "", 0.0
                k.ok, k.fehler, k.fehlversuche = False, "warte auf Daten", 0
                k.leeren()
                if kid == "ind":
                    ind["port"] = p
                    ind_speichern()
            return self.json({"ok": True})
        if path.startswith("/api/ind/"):
            return self.post_ind(path[len("/api/ind/"):], body)
        if path == "/api/verbindung":
            try:
                return self.json({"ok": True, "master": master_setzen(str(body["master"]))})
            except ValueError:
                return self.json({"fehler": "Ungültige IP-Adresse (Beispiel: 169.254.33.228)"}, 400)
        if path == "/api/verbindung/suchen":
            gefunden, geprueft = master_suchen()
            return self.json({"ok": True, "gefunden": gefunden, "geprueft": geprueft})
        if path == "/api/verbindung/auto_ip":
            verb["auto_ip"] = bool(body["an"])
            json_schreiben(VERB_DATEI, verb)
            return self.json({"ok": True})
        if path == "/api/verbindung/pc_ip":
            return self.json({"ok": True, "text": auto_ip(erzwingen=True)})
        if path in ("/api/temp/cof", "/api/temp/cof/abgleich"):
            port = kanaele["temp"].port
            try:
                if path == "/api/temp/cof":
                    neu = float(body["wert"])
                else:  # Sensor so abgleichen, dass sein Messwert (Mittel 5 s) der Referenz entspricht
                    k = kanaele["temp"]
                    r = k.mittel(float(body.get("mittel_s", 5)))
                    if not k.ok or r is None or r[2] < 3:
                        return self.json({"fehler": "zu wenige aktuelle Messwerte – kurz warten"}, 400)
                    neu = cof_lesen(port) + float(body["referenz"]) - r[0]
                alt = cof_lesen(port)
                return self.json({"ok": True, "alt": alt, "cof": cof_schreiben(port, neu), "zeit": jetzt_iso()})
            except ValueError as e:
                return self.json({"fehler": str(e)}, 400)
            except Exception as e:
                return self.json({"fehler": f"Master/Sensor: {str(e) or e.__class__.__name__}"}, 502)
        # ---- Temperatur-Kalibrierung
        if path == "/api/kal/manuell":
            k = {"steigung": float(body["steigung"]), "offset": float(body["offset"]), "punkte": []}
        elif path == "/api/kal/reset":
            k = {"steigung": 1.0, "offset": 0.0, "punkte": []}
        elif path == "/api/kal/punkt":
            # body: {"referenz": 25.0, "nr": 1|2}  nr=1 alleine -> 1-Punkt (nur Offset)
            r = kanaele["temp"].mittel(float(body.get("mittel_s", 5)))
            if r is None:
                return self.json({"fehler": "noch keine Messwerte"}, 400)
            m = r[0]
            ref, nr = float(body["referenz"]), int(body.get("nr", 1))
            punkte = [p for p in kal.get("punkte", []) if p["nr"] != nr]
            punkte.append({"nr": nr, "gemessen": round(m, 3), "referenz": ref})
            punkte.sort(key=lambda p: p["nr"])
            if nr == 1:  # 1-Punkt: Steigung behalten, Offset so dass Messwert = Referenz
                k = {"steigung": kal["steigung"], "offset": ref - kal["steigung"] * m, "punkte": punkte[:1]}
            else:
                p1 = next((p for p in punkte if p["nr"] == 1), None)
                if p1 is None:
                    return self.json({"fehler": "zuerst Punkt 1 setzen"}, 400)
                if abs(m - p1["gemessen"]) < 1.0:
                    return self.json({"fehler": "Punkt 2 muss mind. 1 °C von Punkt 1 entfernt sein "
                                                "(z. B. Eiswasser und warmes Wasser)"}, 400)
                s = (ref - p1["referenz"]) / (m - p1["gemessen"])
                k = {"steigung": s, "offset": p1["referenz"] - s * p1["gemessen"], "punkte": punkte}
        else:
            return self.send(404, "text/plain", b"nicht gefunden")
        kal_speichern(k)
        kal = kal_laden()
        self.json({"ok": True, "kal": kal})

    def post_ind(self, was, body):
        kk = ind["kal"]
        if was == "dekoder":
            d = dekoder_pruefen(body)
            hexwert = kanaele["ind"].letzter_hex
            if hexwert:  # pruefen, bevor gespeicherte Messwerte neu ausgewertet (und evtl. verworfen) werden
                dekodiere_ind(hexwert, d)
            alt = ind["dekoder"]
            wert_geaendert = any(d[x] != alt[x] for x in ("bitoffset", "bitlaenge", "signed", "faktor"))
            ind["dekoder"] = d
            if wert_geaendert:  # Rohwerte bedeuten jetzt etwas anderes -> Kalibrierung passt nicht mehr
                kk.update(punkte=[], tara=None, geaendert=jetzt_iso())
            ind_speichern()
            ind_neu_dekodieren()
            return self.json({"ok": True, "kal_geloescht": wert_geaendert})
        if was == "kal/punkt":  # Mittelwert der letzten Sekunden als Rohwert, Abstand vom Nutzer
            mm, sek = float(body["mm"]), float(body.get("mittel_s", 2))
            k = kanaele["ind"]
            if not k.ok:
                return self.json({"fehler": "Sensor liefert gerade keine Werte"}, 400)
            r = k.mittel(sek)
            if r is None or r[2] < 3:
                return self.json({"fehler": "zu wenige Messwerte – kurz warten oder Abtastung erhöhen"}, 400)
            kk["punkte"].append({"roh": round(r[0], 6), "mm": mm, "sigma": round(r[1], 6), "n": r[2]})
        elif was == "kal/punkt_manuell":
            kk["punkte"].append({"roh": float(body["roh"]), "mm": float(body["mm"]), "sigma": None, "n": 0})
        elif was == "kal/loeschen":
            i = int(body["index"])
            if not 0 <= i < len(kk["punkte"]):
                return self.json({"fehler": "Punkt existiert nicht"}, 400)
            kk["punkte"].pop(i)
        elif was == "kal/reset":
            kk["punkte"] = []
        elif was == "kal/modus":
            if body["modus"] not in ("stueckweise", "linear"):
                return self.json({"fehler": "unbekannter Modus"}, 400)
            kk["modus"] = body["modus"]
        elif was == "tara":
            if body.get("setzen"):
                f = kal_funktion(kk)
                if f is None:
                    return self.json({"fehler": "Nullpunkt erst nach der Kalibrierung (mind. 2 Punkte)"}, 400)
                r = kanaele["ind"].mittel(1.0)
                if r is None:
                    return self.json({"fehler": "noch keine Messwerte"}, 400)
                kk["tara"] = round(f(r[0]), 6)
            else:
                kk["tara"] = None
            kk["geaendert"] = jetzt_iso()
            ind_speichern()
            return self.json({"ok": True})
        else:
            return self.send(404, "text/plain", b"nicht gefunden")
        kk["punkte"].sort(key=lambda p: p["mm"])
        kk["tara"] = None if kal_funktion(kk) is None else kk.get("tara")
        kk["geaendert"] = jetzt_iso()
        ind_speichern()
        self.json({"ok": True})


HTML = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IO-Link Monitor</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c1f24;--mut:#6b7280;--line:#e5e7eb;--acc:#d9480f;--acc2:#1971c2;--raw:#9aa0a6;--ok:#2f9e44;--err:#c92a2a;--sel:#d0ebff;--sw:#ffe8cc}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--card:#1d2026;--fg:#e8eaed;--mut:#9aa0a6;--line:#2c3038;--acc:#ff8a4c;--acc2:#4dabf7;--raw:#5f6670;--sel:#1c3a57;--sw:#5c3a12}}
*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
header{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;margin-bottom:6px}
h1{font-size:18px;margin:0}h2{font-size:15px;margin:0 0 10px}
nav{display:flex;gap:4px;background:var(--line);padding:3px;border-radius:9px}
nav a{padding:5px 14px;border-radius:7px;color:var(--fg);text-decoration:none;font-size:14px}
nav a.on{background:var(--card);font-weight:600;box-shadow:0 1px 2px #0002}
.sensor{font-size:13px;color:var(--mut);margin-bottom:12px;min-height:1.3em}
.st{font-size:13px;color:var(--mut);display:flex;align-items:center;gap:6px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.tiles{display:grid;grid-template-columns:2fr 1fr 1fr 1fr;gap:10px;margin-bottom:10px}
.tiles.t5{grid-template-columns:2fr 1fr 1fr 1fr 1fr}
@media (max-width:640px){.tiles,.tiles.t5{grid-template-columns:1fr 1fr}.tiles .big{grid-column:1/-1}}
.lab{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.val{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
.big .val{font-size:44px;color:var(--acc)}
#v_ind .big .val{color:var(--acc2)}
.sub{font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px}
button,select,input{font:inherit;padding:6px 10px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
button{cursor:pointer}button.pri{background:var(--acc);border-color:var(--acc);color:#fff}
#v_ind button.pri{background:var(--acc2);border-color:var(--acc2)}
input{width:90px}input[type=checkbox]{width:auto}
.sp{flex:1}
canvas{width:100%;height:380px;display:block}
small,.hint{color:var(--mut);font-size:13px}
.kal{margin-top:10px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
@media (max-width:800px){.kal{grid-template-columns:1fr}}
.kal .row,.row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:6px}
.msg{min-height:1.4em;font-size:13px;margin-top:8px}
.leg{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--mut)}
.leg i{width:14px;height:2px;display:inline-block}
.sw{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600;background:var(--line);color:var(--mut)}
.sw.an{background:var(--ok);color:#fff}
.bits{font:13px/1.6 ui-monospace,Menlo,Consolas,monospace;word-break:break-all;margin-top:6px}
.bits span{display:inline-block;width:1.15em;text-align:center;border-radius:3px}
.bits span.s{background:var(--sel)}.bits span.w{background:var(--sw);font-weight:700}
.bits b{display:inline-block;width:.6em}
.ikal{margin-top:10px;display:grid;grid-template-columns:1fr 1.2fr 1.2fr;gap:16px}
@media (max-width:900px){.ikal{grid-template-columns:1fr}}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:4px 6px;border-bottom:1px solid var(--line)}th{color:var(--mut);font-weight:500}
td button{padding:1px 7px;font-size:12px}
#i_kc{height:240px}
.dek{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px 14px;margin-top:8px}
.dek label{display:flex;flex-direction:column;gap:3px}.dek input,.dek select{width:100%}
.warn{color:var(--err)}
.banner{border:1px solid var(--err);color:var(--err);background:color-mix(in srgb,var(--err) 8%,transparent);border-radius:9px;padding:8px 12px;margin-bottom:10px;font-size:14px}
.banner a{color:inherit;font-weight:600}
.vstat.ok .val{color:var(--ok)}.vstat.err .val{color:var(--err)}
.log{max-height:260px;overflow:auto;font-size:13px;font-variant-numeric:tabular-nums}
.log div{padding:3px 0;border-bottom:1px solid var(--line)}.log .t{color:var(--mut);margin-right:8px}
.log .ok{color:var(--ok)}.log .fehler{color:var(--err)}
td.l,th.l{text-align:left}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
@media (max-width:800px){.grid2{grid-template-columns:1fr}}
</style></head><body><main>
<header><h1>IO-Link Monitor</h1>
<nav><a href="#temp" id="tab_temp">Temperatur</a><a href="#ind" id="tab_ind">Induktiv</a><a href="#verb" id="tab_verb">Verbindung</a></nav>
<span class="sp"></span>
<div class="st"><span class="dot" id="dot"></span><span id="stxt">verbinde …</span></div>
<button id="quit" title="Programm beenden (nicht exportierte Messwerte gehen verloren)">Beenden</button></header>
<div class="sensor" id="sline"></div>
<div class="banner" id="banner" hidden>Keine Verbindung zum Master <b id="b_ip"></b> – <span id="b_err"></span>. Die Anzeige läuft weiter und verbindet automatisch neu. <a href="#verb">Verbindung prüfen →</a></div>

<!-- ======================= Temperatur ======================= -->
<section id="v_temp">
<div class="tiles">
 <div class="card big"><div class="lab">Aktuell <span id="t_kalflag"></span></div><div class="val" id="t_cur">–</div><div class="sub" id="t_curraw"></div></div>
 <div class="card"><div class="lab">Min</div><div class="val" id="t_min">–</div></div>
 <div class="card"><div class="lab">Max</div><div class="val" id="t_max">–</div></div>
 <div class="card"><div class="lab">Mittel</div><div class="val" id="t_avg">–</div></div>
</div>
<div class="card">
 <div class="bar">
  <label class="lab" for="t_port">Port</label><select id="t_port" class="port"></select>
  <label class="lab" for="t_fen">Zeitfenster</label><select id="t_fen" class="fen"></select>
  <label class="lab">Abtastung</label><select class="ivl"></select>
  <button class="pause">Pause</button>
  <span class="leg"><i style="background:var(--acc)"></i>kalibriert</span>
  <span class="leg" id="t_legraw"><i style="background:var(--raw)"></i>Rohwert</span>
  <span class="sp"></span>
  <button id="t_reset">Daten löschen</button>
  <button class="pri" id="t_csv">CSV exportieren</button>
 </div>
 <canvas id="t_c"></canvas>
 <small id="t_info"></small>
</div>
<div class="card" style="margin-top:10px">
 <h2>Kalibrierung</h2>
 <div class="hint" id="t_kalakt"></div>
 <div class="kal">
  <div><div class="lab">1-Punkt (Offset)</div>
   <div class="hint">Sensor und Referenzthermometer an dieselbe Stelle, warten bis stabil, Referenzwert eintragen. Es wird der Mittelwert der letzten 5 s genommen.</div>
   <div class="row"><input id="r1" type="number" step="0.01" placeholder="°C"><button id="p1">Punkt 1 übernehmen</button></div></div>
  <div><div class="lab">2-Punkt (Offset + Steigung)</div>
   <div class="hint">Erst Punkt 1 (z. B. Eiswasser 0 °C), dann bei einer anderen Temperatur Punkt 2 (z. B. warmes Wasser).</div>
   <div class="row"><input id="r2" type="number" step="0.01" placeholder="°C"><button id="p2">Punkt 2 übernehmen</button></div></div>
  <div><div class="lab">Manuell</div>
   <div class="row">T = <input id="ms" type="number" step="0.0001" title="Steigung"> × T<sub>roh</sub> + <input id="mo" type="number" step="0.01" title="Offset"></div>
   <div class="row"><button id="msave">Speichern</button><button id="kreset">Zurücksetzen</button></div></div>
 </div>
 <div class="msg" id="t_kmsg"></div>
</div>
<div class="card" style="margin-top:10px">
 <h2>Kalibrieroffset im Sensor (coF)</h2>
 <div class="hint">Nullpunktkorrektur, die <b>im AT001 selbst</b> gespeichert wird (Parameter coF, ±10,0 °C in 0,1-°C-Schritten).
  Sie verschiebt die Prozessdaten und den 4–20-mA-Ausgang – gilt also auch für SPS oder andere Master. Die Kalibrierung oben im Monitor wirkt zusätzlich obendrauf.</div>
 <div class="kal">
  <div><div class="lab">Aktuell im Sensor</div>
   <div class="val" id="c_akt" style="margin-top:4px">–</div><div class="sub" id="c_zeit"></div>
   <div class="row"><button id="c_lesen">Neu lesen</button></div></div>
  <div><div class="lab">Abgleich auf Referenz</div>
   <div class="hint">Sensor und Referenzthermometer an dieselbe Stelle, warten bis stabil, Referenz eintragen. Der Sensor wird so verstellt, dass sein Mittelwert der letzten 5 s der Referenz entspricht.</div>
   <div class="row"><input id="c_ref" type="number" step="0.1" placeholder="°C"><button id="c_abgl">Sensor abgleichen</button></div></div>
  <div><div class="lab">Direkt setzen</div>
   <div class="row"><input id="c_wert" type="number" step="0.1" min="-10" max="10" placeholder="°C"><button id="c_setz">Im Sensor speichern</button></div>
   <div class="row"><button id="c_null">coF auf 0 (Werkseinstellung)</button></div></div>
 </div>
 <div class="msg" id="c_msg"></div>
</div>
</section>

<!-- ======================= Induktiv ======================= -->
<section id="v_ind" hidden>
<div class="tiles t5">
 <div class="card big"><div class="lab"><span id="i_curlab">Abstand</span> <span id="i_kalflag"></span></div><div class="val" id="i_cur">–</div>
  <div class="sub"><span id="i_curraw"></span> <span class="sw" id="i_sw" hidden></span></div></div>
 <div class="card"><div class="lab">Min</div><div class="val" id="i_min">–</div></div>
 <div class="card"><div class="lab">Max</div><div class="val" id="i_max">–</div></div>
 <div class="card"><div class="lab">Spanne</div><div class="val" id="i_span">–</div></div>
 <div class="card"><div class="lab" title="Standardabweichung im Zeitfenster">Rauschen σ</div><div class="val" id="i_sig">–</div></div>
</div>
<div class="card">
 <div class="bar">
  <label class="lab" for="i_port">Port</label><select id="i_port" class="port"></select>
  <label class="lab" for="i_fen">Zeitfenster</label><select id="i_fen" class="fen"></select>
  <label class="lab">Abtastung</label><select class="ivl"></select>
  <button class="pause">Pause</button>
  <span class="leg"><i style="background:var(--acc2)"></i><span id="i_legval">Abstand</span></span>
  <span class="leg" id="i_legsw"><i style="background:var(--ok);height:6px"></i>Schaltausgang</span>
  <span class="sp"></span>
  <button id="i_reset">Daten löschen</button>
  <button class="pri" id="i_csv">CSV exportieren</button>
 </div>
 <canvas id="i_c"></canvas>
 <small id="i_info"></small>
</div>

<div class="card" style="margin-top:10px">
 <h2>Abstandskalibrierung</h2>
 <div class="hint" id="i_kalakt"></div>
 <div class="ikal">
  <div>
   <div class="lab">Punkt aufnehmen</div>
   <div class="hint">Messobjekt auf bekannten Abstand bringen (Fühlerlehre, Endmaß, Mikrometertisch), warten bis der Wert ruhig ist, Abstand eintragen. Gespeichert wird der Mittelwert des Rohwerts.</div>
   <div class="row"><input id="i_mm" type="number" step="0.001" placeholder="mm"> mm
    <select id="i_mit" title="Mittelungsdauer"><option value="1">Ø 1 s</option><option value="2" selected>Ø 2 s</option><option value="5">Ø 5 s</option></select></div>
   <div class="row"><button class="pri" id="i_pauf">Punkt aufnehmen</button></div>
   <div class="lab" style="margin-top:14px">Punkt von Hand</div>
   <div class="row"><input id="i_mroh" type="number" step="any" placeholder="Rohwert"> → <input id="i_mmm" type="number" step="0.001" placeholder="mm"><button id="i_pman">Hinzufügen</button></div>
   <div class="lab" style="margin-top:14px">Kennlinie</div>
   <div class="row"><select id="i_modus" style="width:100%"><option value="stueckweise">Stückweise linear (durch alle Punkte)</option><option value="linear">Ausgleichsgerade (lineare Regression)</option></select></div>
   <div class="lab" style="margin-top:14px">Nullpunkt</div>
   <div class="hint">Aktuellen Abstand als 0 setzen, um Wege relativ dazu zu messen.</div>
   <div class="row"><button id="i_tara">Auf 0 setzen</button><button id="i_taraweg">Aufheben</button></div>
  </div>
  <div>
   <div class="lab">Kalibrierpunkte</div>
   <table><thead><tr><th>#</th><th>Abstand mm</th><th id="i_throh">Rohwert</th><th title="Streuung des Rohwerts während der Aufnahme">σ</th><th title="Abweichung von der Ausgleichsgeraden">Abw. Gerade</th><th></th></tr></thead><tbody id="i_tab"></tbody></table>
   <div class="row"><span class="sp"></span><button id="i_kreset">Alle Punkte löschen</button></div>
  </div>
  <div>
   <div class="lab">Kennlinie Rohwert → Abstand</div>
   <canvas id="i_kc"></canvas>
  </div>
 </div>
 <div class="msg" id="i_kmsg"></div>
</div>

<div class="card" style="margin-top:10px">
 <h2>Prozessdaten dekodieren</h2>
 <div class="hint">Wie der Rohwert aus den Prozessdaten des Sensors gelesen wird. Die Werte stehen im Datenblatt bzw. in der IODD unter „ProcessDataIn“ (Bit-Offset gezählt ab dem niederwertigsten Bit, rechts). Blau = Wert, orange = Schaltbit.</div>
 <div class="row"><span class="lab">Prozessdaten</span> <code id="i_hex">–</code> <small id="i_hexinfo"></small></div>
 <div class="bits" id="i_bits"></div>
 <div class="dek">
  <label><span class="lab">Startbit (Offset)</span><input id="i_bo" type="number" min="0" max="255" step="1"></label>
  <label><span class="lab">Bitlänge</span><input id="i_bl" type="number" min="1" max="64" step="1"></label>
  <label><span class="lab">Vorzeichen</span><select id="i_sg"><option value="0">ohne (unsigned)</option><option value="1">mit (signed)</option></select></label>
  <label><span class="lab">Faktor</span><input id="i_fa" type="number" step="any"></label>
  <label><span class="lab">Einheit Rohwert</span><input id="i_eh" type="text" maxlength="20" placeholder="digits"></label>
  <label><span class="lab">Schaltbit</span><input id="i_sb" type="number" min="-1" max="255" step="1" title="-1 = kein Schaltbit"></label>
 </div>
 <div class="row"><button id="i_dsave">Übernehmen</button><small>Ändern von Startbit, Länge, Vorzeichen oder Faktor löscht die Kalibrierpunkte; gespeicherte Messwerte werden neu ausgewertet.</small></div>
 <div class="msg" id="i_dmsg"></div>
</div>
</section>

<!-- ======================= Verbindung ======================= -->
<section id="v_verb" hidden>
<div class="tiles">
 <div class="card big vstat" id="v_kachel"><div class="lab">Master <span id="v_ipanz"></span></div><div class="val" id="v_stat">–</div><div class="sub" id="v_seit"></div></div>
 <div class="card"><div class="lab" title="Antwortzeit pro Abfrage (gleitender Mittelwert)">Antwortzeit</div><div class="val" id="v_lat">–</div></div>
 <div class="card"><div class="lab">Werte/s</div><div class="val" id="v_rate">–</div></div>
 <div class="card"><div class="lab" title="Verbindungsabbrüche seit Programmstart">Abbrüche</div><div class="val" id="v_abbr">–</div></div>
</div>
<div class="grid2">
 <div class="card">
  <h2>Master</h2>
  <div class="row"><input id="v_ip" type="text" style="width:170px" placeholder="169.254.33.228" spellcheck="false"><button class="pri" id="v_verbinden">Verbinden</button><button id="v_suchen">Master suchen</button></div>
  <div class="hint" style="margin-top:6px">Der Monitor verbindet bei Abbruch automatisch neu – nach dem Einstecken einfach warten. Eine andere IP hier eintragen und <i>Verbinden</i>. <i>Master suchen</i> prüft alle 169.254.x.x-Geräte, mit denen der PC schon Kontakt hatte.</div>
  <div class="msg" id="v_msg"></div>
  <div id="v_such"></div>
  <table style="margin-top:6px"><tbody id="v_info"></tbody></table>
 </div>
 <div class="card">
  <h2>Ports</h2>
  <table><thead><tr><th>Port</th><th class="l">Modus</th><th class="l">Status</th><th class="l">Gerät</th><th class="l">Ansicht</th></tr></thead><tbody id="v_ports"></tbody></table>
  <div class="hint" style="margin-top:6px">Wird alle 5 s aktualisiert, solange der Master verbunden ist.</div>
 </div>
 <div class="card">
  <h2>PC-Netzwerk</h2>
  <div id="v_pc"></div>
  <div id="v_pclinux" hidden>
   <div class="row"><label style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="v_auto"> PC-IP automatisch setzen, wenn der Master nicht erreichbar ist</label></div>
   <div class="hint">Nur an einem Ethernet-Anschluss mit Kabel, der <b>gar keine</b> IP hat (Master direkt am PC). Anschlüsse mit Netzwerk-Adresse (z. B. Firmen-LAN) werden nie verändert.</div>
   <div class="row"><button id="v_pcip">PC-IP jetzt setzen</button></div>
  </div>
  <div class="hint" id="v_pchint" style="margin-top:6px"></div>
  <div class="msg" id="v_pcmsg"></div>
 </div>
 <div class="card">
  <h2>Ereignisprotokoll</h2>
  <div class="log" id="v_log"></div>
 </div>
</div>
</section>
</main>
<script>
const $=id=>document.getElementById(id);
const S={temp:{data:[],seit:0,gen:null},ind:{data:[],seit:0,gen:null}};
let ansicht="temp",pause=false,intervall=1,ivlInit=false,timer=null,beendet=false,pollId=0,master="";
let kal={steigung:1,offset:0,punkte:[]};                      // Temperatur
let dek={bitoffset:0,bitlaenge:16,signed:false,faktor:1,einheit:"digits",schaltbit:-1}, ikal={modus:"stueckweise",punkte:[],tara:null}, iF=null, ihex="";
const dz=(v,d)=>{if(v==null||!isFinite(v))return "–";const s=v.toFixed(d).replace(".",",");return /^-0(,0*)?$/.test(s)?s.slice(1):s};
const fmt=(v,d=1)=>v==null||isNaN(v)?"–":dz(v,d)+" °C";
const KT=v=>kal.steigung*v+kal.offset;
const istKal=()=>Math.abs(kal.steigung-1)>1e-9||Math.abs(kal.offset)>1e-9;
const post=async(u,b)=>{const r=await fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})});return [r.ok,await r.json()]};
function meld(id,txt,err){$(id).textContent=txt;$(id).style.color=err?"var(--err)":"var(--ok)"}

// ---------- gemeinsame Bedienelemente
for(const s of document.querySelectorAll(".port"))s.innerHTML='<option value="0">aus</option>'+[1,2,3,4,5,6,7,8].map(p=>`<option value="${p}">${p}</option>`).join("");
for(const s of document.querySelectorAll(".fen"))s.innerHTML='<option value="10">10 s</option><option value="60">1 min</option><option value="300" selected>5 min</option><option value="900">15 min</option><option value="3600">1 h</option><option value="0">alles</option>';
$("i_fen").value="60";
for(const b of document.querySelectorAll(".pause"))b.onclick=()=>{pause=!pause;for(const x of document.querySelectorAll(".pause"))x.textContent=pause?"Weiter":"Pause";zeichne()};
for(const s of document.querySelectorAll(".fen"))s.onchange=()=>zeichne();
for(const s of document.querySelectorAll(".ivl"))s.onchange=async e=>{await post("/api/intervall",{intervall:+e.target.value});intervall=+e.target.value;poll()};
for(const k of["temp","ind"]){const p=k[0];
  $(p+"_port").onchange=async e=>{await post("/api/port",{k,port:+e.target.value});poll()};
  $(p+"_csv").onclick=()=>location.href="/export.csv?k="+k;
}
addEventListener("resize",()=>zeichne());
// Bestaetigung direkt im Knopf (confirm() ist in manchen eingebetteten Browsern blockiert)
function bestaetigen(btn,frage,aktion){
  const text=btn.textContent;
  btn.onclick=async()=>{
    if(!btn.dataset.armed){btn.dataset.armed=1;btn.textContent=frage;btn.style.borderColor="var(--err)";btn.style.color="var(--err)";
      btn._t=setTimeout(()=>{delete btn.dataset.armed;btn.textContent=text;btn.style.borderColor=btn.style.color=""},4000);return}
    clearTimeout(btn._t);delete btn.dataset.armed;btn.textContent=text;btn.style.borderColor=btn.style.color="";await aktion();
  };
}
function leeren(k){const s=S[k];s.data.length=0;s.seit=0;
  for(const id of k==="temp"?["t_cur","t_min","t_max","t_avg"]:["i_cur","i_min","i_max","i_span","i_sig"])$(id).textContent="–";
  $(k[0]+"_curraw").textContent="";zeichne()}
bestaetigen($("t_reset"),"Wirklich löschen?",async()=>{await post("/api/reset",{k:"temp"});poll()});
bestaetigen($("i_reset"),"Wirklich löschen?",async()=>{await post("/api/reset",{k:"ind"});poll()});
bestaetigen($("quit"),"Wirklich beenden?",async()=>{
  try{await post("/api/beenden")}catch(e){}
  beendet=true;clearTimeout(timer);
  document.querySelector("main").innerHTML='<div class="card" style="margin-top:40px;text-align:center"><h2>Monitor beendet</h2>'+
    '<div class="hint">Das Programm wurde geschlossen. Dieses Fenster kann zu. Neu starten mit dem Starter (<b>Start_Linux.sh</b>, <b>Start_Windows.bat</b>, <b>IO-Link-Monitor.exe</b> oder <b>Start_macOS.command</b>).</div></div>';
});
function zeige(){
  ansicht={"#ind":"ind","#verb":"verb"}[location.hash]||"temp";
  for(const v of["temp","ind","verb"]){$("v_"+v).hidden=ansicht!==v;$("tab_"+v).className=ansicht===v?"on":""}
  document.title={temp:"Temperatur",ind:"Induktiv",verb:"Verbindung"}[ansicht]+" – IO-Link Monitor";
  poll();
}
addEventListener("hashchange",zeige);

// ---------- Temperatur-Kalibrierung
function setKal(k,msg,err){kal=k;showKal();if(msg!=null)meld("t_kmsg",msg,err);zeichne()}
function showKal(){
  $("ms").value=+kal.steigung.toFixed(6);$("mo").value=+kal.offset.toFixed(4);
  const p=(kal.punkte||[]).map(p=>`P${p.nr}: gemessen ${fmt(p.gemessen,2)} → Referenz ${fmt(p.referenz,2)}`).join(" · ");
  $("t_kalakt").textContent=istKal()?`Aktiv: T = ${kal.steigung.toFixed(5)} × T_roh ${kal.offset<0?"−":"+"} ${Math.abs(kal.offset).toFixed(3)} °C`+
    (p?"  ("+p+")":"")+(kal.geaendert?"  – gespeichert "+kal.geaendert.replace("T"," "):""):"Keine Kalibrierung aktiv (Rohwert × Faktor).";
  $("t_kalflag").textContent=istKal()?"· kalibriert":"";$("t_legraw").style.display=istKal()?"":"none";
}
async function punkt(nr){
  const v=$("r"+nr).value; if(v===""){setKal(kal,"Bitte Referenztemperatur eintragen.",true);return}
  const [ok,r]=await post("/api/kal/punkt",{nr,referenz:+v});
  ok?setKal(r.kal,nr==1?"Punkt 1 übernommen (Offset angepasst).":"Punkt 2 übernommen – Offset und Steigung berechnet."):setKal(kal,r.fehler,true);
}
$("p1").onclick=()=>punkt(1);$("p2").onclick=()=>punkt(2);
$("msave").onclick=async()=>{const [ok,r]=await post("/api/kal/manuell",{steigung:+$("ms").value,offset:+$("mo").value});ok?setKal(r.kal,"Gespeichert."):setKal(kal,r.fehler,true)};
bestaetigen($("kreset"),"Wirklich zurücksetzen?",async()=>{const [,r]=await post("/api/kal/reset");setKal(r.kal,"Kalibrierung zurückgesetzt.")});

// ---------- coF: Kalibrieroffset im Sensor (AT001–AT004)
const fmtS=v=>(v>0?"+":"")+fmt(v,1);
function cofKnoepfe(an){for(const id of["c_abgl","c_setz","c_null"])$(id).disabled=!an}
async function cofLesen(){
  $("c_zeit").textContent="lese …";
  try{const r=await (await fetch("/api/temp/cof")).json();
    if(r.ok){$("c_akt").textContent=fmtS(r.cof);$("c_zeit").textContent="gelesen "+r.zeit.replace("T"," ");cofKnoepfe(true)}
    else{$("c_akt").textContent="–";$("c_zeit").textContent=r.fehler;cofKnoepfe(false)}
  }catch(e){$("c_zeit").textContent="Programm nicht erreichbar"}
}
async function cofAktion(u,b,was){
  const [ok,r]=await post(u,b);
  ok?meld("c_msg",`${was}: coF ${fmtS(r.alt)} → ${fmtS(r.cof)} im Sensor gespeichert.`+(istKal()?" Hinweis: Die Monitor-Kalibrierung oben wirkt zusätzlich – ggf. zurücksetzen.":""))
    :meld("c_msg",r.fehler,true);
  await cofLesen();
}
function cofKnopf(btn,eingabe,aktion){ // erst Eingabe pruefen, dann zweiter Klick zum Bestaetigen (schreibt in den Sensor)
  const text=btn.textContent;
  btn.onclick=async()=>{
    const v=eingabe?$(eingabe).value:"0";
    if(v===""){meld("c_msg","Bitte einen Wert in °C eintragen.",true);return}
    if(!btn.dataset.armed){btn.dataset.armed=1;btn.textContent="Wirklich in den Sensor schreiben?";btn.style.borderColor=btn.style.color="var(--err)";
      btn._t=setTimeout(()=>{delete btn.dataset.armed;btn.textContent=text;btn.style.borderColor=btn.style.color=""},4000);return}
    clearTimeout(btn._t);delete btn.dataset.armed;btn.textContent=text;btn.style.borderColor=btn.style.color="";btn.disabled=true;
    try{await aktion(+v)}finally{btn.disabled=false}
  };
}
cofKnopf($("c_abgl"),"c_ref",v=>cofAktion("/api/temp/cof/abgleich",{referenz:v},"Abgleich auf "+fmt(v,1)));
cofKnopf($("c_setz"),"c_wert",v=>cofAktion("/api/temp/cof",{wert:v},"Gesetzt"));
cofKnopf($("c_null"),null,()=>cofAktion("/api/temp/cof",{wert:0},"Zurückgesetzt"));
$("c_lesen").onclick=cofLesen;
cofKnoepfe(false);

// ---------- Induktiv: Kalibrierfunktion (gleich wie kal_funktion() in Python)
function kalFunktion(k){
  const g=new Map();for(const p of k.punkte||[]){if(!g.has(p.roh))g.set(p.roh,[]);g.get(p.roh).push(p.mm)}
  const pts=[...g].map(([r,m])=>[r,m.reduce((a,b)=>a+b,0)/m.length]).sort((a,b)=>a[0]-b[0]);
  if(pts.length<2)return null;
  if(k.modus==="linear"){const n=pts.length,mx=pts.reduce((a,p)=>a+p[0],0)/n,my=pts.reduce((a,p)=>a+p[1],0)/n;
    const a=pts.reduce((s,p)=>s+(p[0]-mx)*(p[1]-my),0)/pts.reduce((s,p)=>s+(p[0]-mx)**2,0);const f=r=>a*(r-mx)+my;f.lo=pts[0][0];f.hi=pts[n-1][0];return f}
  const f=r=>{let lo=0,hi=pts.length;while(lo<hi){const m=(lo+hi)>>1;pts[m][0]<=r?lo=m+1:hi=m}
    const i=Math.min(Math.max(lo-1,0),pts.length-2),[x0,y0]=pts[i],[x1,y1]=pts[i+1];return y0+(r-x0)*(y1-y0)/(x1-x0)};
  f.lo=pts[0][0];f.hi=pts[pts.length-1][0];return f;
}
function geradeFit(k){ // fuer die Spalte "Abw. Gerade" (Linearitaet)
  const p=k.punkte||[];if(p.length<3)return null;const n=p.length,mx=p.reduce((a,q)=>a+q.roh,0)/n,my=p.reduce((a,q)=>a+q.mm,0)/n;
  const sxx=p.reduce((s,q)=>s+(q.roh-mx)**2,0);if(!sxx)return null;const a=p.reduce((s,q)=>s+(q.roh-mx)*(q.mm-my),0)/sxx;return r=>a*(r-mx)+my;
}
const tara=()=>ikal.tara==null?0:ikal.tara;
const IV=r=>iF?iF(r)-tara():r;                         // angezeigter Wert
const iEinheit=()=>iF?"mm":(dek.einheit||"");
const rohDez=()=>{const f=Math.abs(dek.faktor);return f>=1?0:Math.min(6,Math.ceil(-Math.log10(f)))};
const iFmt=(v,diff)=>v==null||!isFinite(v)?"–":dz(v,iF?(diff?4:3):rohDez())+" "+iEinheit();

function zeigeDekoder(d){$("i_bo").value=d.bitoffset;$("i_bl").value=d.bitlaenge;$("i_sg").value=d.signed?"1":"0";$("i_fa").value=d.faktor;$("i_eh").value=d.einheit;$("i_sb").value=d.schaltbit}
function zeigeBits(){
  $("i_hex").textContent=ihex||"–";
  if(!ihex){$("i_bits").innerHTML="";$("i_hexinfo").textContent="";return}
  const n=ihex.length*4;let v=BigInt("0x"+ihex),h="";
  const bo=+$("i_bo").value,bl=+$("i_bl").value,sb=+$("i_sb").value;
  for(let b=n-1;b>=0;b--){const bit=(v>>BigInt(b))&1n;const c=b===sb?"w":(b>=bo&&b<bo+bl?"s":"");
    h+=`<span class="${c}" title="Bit ${b}">${bit}</span>`;if(b%8===0&&b)h+="<b></b>"}
  $("i_bits").innerHTML=h;$("i_hexinfo").textContent=`${n/8} Byte = ${n} Bit (links Bit ${n-1}, rechts Bit 0)`;
}
for(const id of["i_bo","i_bl","i_sb"])$(id).oninput=zeigeBits;
async function dekSpeichern(){
  const d={k:"ind",bitoffset:+$("i_bo").value,bitlaenge:+$("i_bl").value,signed:$("i_sg").value==="1",faktor:+$("i_fa").value,einheit:$("i_eh").value,schaltbit:$("i_sb").value===""?-1:+$("i_sb").value};
  const [ok,r]=await post("/api/ind/dekoder",d);
  ok?meld("i_dmsg",r.kal_geloescht?"Übernommen. Kalibrierpunkte wurden gelöscht (Rohwert hat sich geändert).":"Übernommen."):meld("i_dmsg",r.fehler,true);poll();
}
$("i_dsave").onclick=()=>{
  const andersWert=+$("i_bo").value!==dek.bitoffset||+$("i_bl").value!==dek.bitlaenge||($("i_sg").value==="1")!==dek.signed||+$("i_fa").value!==dek.faktor;
  if(andersWert&&ikal.punkte.length){ // Kalibrierung ginge verloren -> zweiter Klick noetig
    const b=$("i_dsave");if(!b.dataset.armed){b.dataset.armed=1;b.textContent="Kalibrierung geht verloren – nochmal klicken";b.style.color="var(--err)";
      b._t=setTimeout(()=>{delete b.dataset.armed;b.textContent="Übernehmen";b.style.color=""},5000);return}
    clearTimeout(b._t);delete b.dataset.armed;b.textContent="Übernehmen";b.style.color="";
  }
  dekSpeichern();
};
async function ikAktion(u,b,ok_msg){const [ok,r]=await post(u,Object.assign({k:"ind"},b||{}));ok?meld("i_kmsg",ok_msg):meld("i_kmsg",r.fehler,true);poll()}
$("i_pauf").onclick=()=>{const v=$("i_mm").value;if(v===""){meld("i_kmsg","Bitte Abstand in mm eintragen.",true);return}
  ikAktion("/api/ind/kal/punkt",{mm:+v,mittel_s:+$("i_mit").value},`Punkt bei ${dz(+v,3)} mm aufgenommen.`)};
$("i_pman").onclick=()=>{if($("i_mroh").value===""||$("i_mmm").value===""){meld("i_kmsg","Rohwert und Abstand eintragen.",true);return}
  ikAktion("/api/ind/kal/punkt_manuell",{roh:+$("i_mroh").value,mm:+$("i_mmm").value},"Punkt hinzugefügt.")};
$("i_modus").onchange=e=>ikAktion("/api/ind/kal/modus",{modus:e.target.value},"Kennlinie umgestellt.");
$("i_tara").onclick=()=>ikAktion("/api/ind/tara",{setzen:true},"Nullpunkt gesetzt – Anzeige ist jetzt relativ.");
$("i_taraweg").onclick=()=>ikAktion("/api/ind/tara",{setzen:false},"Nullpunkt aufgehoben – Anzeige wieder absolut.");
bestaetigen($("i_kreset"),"Wirklich alle löschen?",()=>ikAktion("/api/ind/kal/reset",{},"Alle Kalibrierpunkte gelöscht."));

function zeigeIkal(){
  iF=kalFunktion(ikal);$("i_modus").value=ikal.modus;
  const e=dek.einheit||"digits",gf=geradeFit(ikal);$("i_throh").textContent="Rohwert ("+e+")";
  const tb=$("i_tab");tb.innerHTML="";
  (ikal.punkte||[]).forEach((p,i)=>{const tr=document.createElement("tr");
    tr.innerHTML=`<td>${i+1}</td><td>${dz(p.mm,3)}</td><td>${dz(p.roh,rohDez()+1)}</td><td>${p.sigma==null?'<span title="von Hand eingetragen">Hand</span>':dz(p.sigma,rohDez()+1)}</td><td>${gf?dz(p.mm-gf(p.roh),4):"–"}</td><td><button title="Punkt löschen">×</button></td>`;
    bestaetigen(tr.querySelector("button"),"löschen?",()=>ikAktion("/api/ind/kal/loeschen",{index:i},`Punkt ${i+1} gelöscht.`));tb.appendChild(tr)});
  if(!ikal.punkte.length)tb.innerHTML='<tr><td colspan="6" style="text-align:center;color:var(--mut)">noch keine Punkte</td></tr>';
  const n=ikal.punkte.length;
  $("i_kalakt").textContent=iF?`Aktiv: ${ikal.modus==="linear"?"Ausgleichsgerade":"stückweise linear"} aus ${n} Punkten`+
    (ikal.tara!=null?` · Nullpunkt bei ${dz(ikal.tara,4)} mm (Anzeige relativ)`:"")+(ikal.geaendert?" – gespeichert "+ikal.geaendert.replace("T"," "):"")
    :n?"Noch nicht aktiv – mindestens 2 Punkte mit verschiedenem Rohwert nötig.":"Keine Kalibrierung – angezeigt wird der Rohwert. Mindestens 2 Punkte aufnehmen, besser 5–10 über den Messbereich.";
  $("i_kalflag").textContent=iF?(ikal.tara!=null?"· relativ":"· kalibriert"):"";
  $("i_curlab").textContent=iF?"Abstand":"Rohwert";$("i_legval").textContent=iF?"Abstand":"Rohwert";
}

// ---------- Datenabruf
async function poll(){
  clearTimeout(timer);if(beendet)return;
  const id=++pollId,k=ansicht,s=S[k];
  if(k==="verb")return pollVerb(id);
  try{
    const r=await (await fetch(`/api/daten?k=${k}&seit=${s.seit}`)).json();
    if(id!==pollId)return; // ueberholt (Ansicht gewechselt o. ae.)
    intervall=r.intervall;master=r.master;
    if(!ivlInit){for(const x of document.querySelectorAll(".ivl"))x.innerHTML=r.intervalle.map(v=>`<option value="${v}">${v<1?Math.round(1/v)+" Hz ("+Math.round(v*1000)+" ms)":v+" s"}</option>`).join("");ivlInit=true}
    for(const x of document.querySelectorAll(".ivl"))if(document.activeElement!==x)x.value=String(r.intervall);
    const pe=$(k[0]+"_port");if(document.activeElement!==pe)pe.value=String(r.status.port);
    const cj=JSON.stringify(r.cfg);
    if(cj!==s.cfgJson){s.cfgJson=cj;
      if(k==="temp")setKal(r.cfg.kal);
      else{if(JSON.stringify(r.cfg.dekoder)!==JSON.stringify(dek)){dek=r.cfg.dekoder;zeigeDekoder(dek)}ikal=r.cfg.kal;zeigeIkal()}}
    if(s.gen!==null&&r.gen!==s.gen){s.gen=r.gen;leeren(k);timer=setTimeout(poll,0);return} // geloescht/neu dekodiert (auch aus anderem Tab)
    s.gen=r.gen;
    for(const p of r.neu){s.data.push(p);s.seit=p[0]}
    if(s.data.length>1e6)s.data.splice(0,s.data.length-1e6);
    const st=r.status;
    if(k==="ind"&&st.hex!==ihex){ihex=st.hex;zeigeBits()}
    if(k==="temp"&&(st.sensor+"@"+st.port)!==s.sensor){s.sensor=st.sensor+"@"+st.port;cofLesen()} // Start, Portwechsel, Umstecken
    $("sline").textContent=st.port?`${st.sensor||"Sensor"} · Port ${st.port} @ ${r.master}`+(st.fehlversuche?` · ${st.fehlversuche} Fehlversuche`:""):"Port aus – oben einen Port wählen";
    const ok=r.master_ok&&st.ok;
    $("dot").style.background=ok?"var(--ok)":"var(--err)";
    $("stxt").textContent=!r.master_ok?"Master getrennt":st.ok?`verbunden · ${dz(r.rate_ist,1)} Werte/s`:"Fehler: "+st.fehler;
    $("banner").hidden=r.master_ok;$("b_ip").textContent=r.master;$("b_err").textContent=r.master_fehler;
  }catch(e){if(id!==pollId)return;$("dot").style.background="var(--err)";$("stxt").textContent="Programm nicht erreichbar"}
  if(!pause)zeichne();
  timer=setTimeout(poll,Math.max(50,intervall*1000)); // Dashboard-Update = Abtastrate
}

// ---------- Zeichnen
function sichtbar(data,f){
  if(!f||!data.length)return data;
  const t0=data[data.length-1][0]-f;let lo=0,hi=data.length;while(lo<hi){const m=(lo+hi)>>1;data[m][0]<t0?lo=m+1:hi=m}return data.slice(lo);
}
function nice(span){const p=Math.pow(10,Math.floor(Math.log10(span)))/10;for(const m of[1,2,5,10])if(span/(m*p)<=6)return m*p;return 10*p}
const farbe=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function leinwand(cv){const dpr=devicePixelRatio||1,W=cv.clientWidth,H=cv.clientHeight;cv.width=W*dpr;cv.height=H*dpr;const g=cv.getContext("2d");g.scale(dpr,dpr);g.clearRect(0,0,W,H);
  g.font="12px system-ui,sans-serif";g.fillStyle=farbe("--mut");return [g,W,H]}

// Zeitverlauf: linien=[{fn(p)->wert, farbe, breite}], o={fen, achse, minSpan, band(p)->0/1/null}
function zeitgraph(cv,pts,linien,o){
  const [g,W,H]=leinwand(cv),L=62,R=12,T=10,B=28,pw=W-L-R,ph=H-T-B;
  if(pts.length<2){g.fillText("Warte auf Messwerte …",L+10,T+20);return}
  let lo=Infinity,hi=-Infinity;for(const l of linien)for(const p of pts){const v=l.fn(p);if(v<lo)lo=v;if(v>hi)hi=v}
  if(!isFinite(lo)){g.fillText("keine gültigen Werte",L+10,T+20);return}
  if(hi-lo<o.minSpan){const m=(hi+lo)/2;lo=m-o.minSpan/2;hi=m+o.minSpan/2}
  const pad=(hi-lo)*.1;lo-=pad;hi+=pad;
  const tEnd=pts[pts.length-1][0],tStart=o.fen?Math.max(tEnd-o.fen,pts[0][0]):pts[0][0],ts=Math.max(1,tEnd-tStart);
  const X=t=>L+(t-tStart)/ts*pw,Y=v=>T+(hi-v)/(hi-lo)*ph;
  const st=nice(hi-lo),dec=Math.min(6,Math.max(0,-Math.floor(Math.log10(st)+1e-9)));
  g.strokeStyle=farbe("--line");g.lineWidth=1;g.textAlign="right";g.textBaseline="middle";
  for(let i=Math.ceil(lo/st);i*st<=hi;i++){const v=i*st,y=Math.round(Y(v))+.5;g.beginPath();g.moveTo(L,y);g.lineTo(W-R,y);g.stroke();
    g.fillText(dz(v,dec)+o.achse,L-6,y)}
  const tst=[1,2,5,10,15,30,60,120,300,600,900,1800,3600,7200,21600].find(s=>ts/s<=7)||43200;
  g.textAlign="center";g.textBaseline="top";
  for(let t=Math.ceil(tStart/tst)*tst;t<=tEnd;t+=tst)
    g.fillText(new Date(t*1000).toLocaleTimeString("de-DE",tst<60?{}:{hour:"2-digit",minute:"2-digit"}),X(t),H-B+8);
  const gap=Math.max(5,intervall*5);
  if(o.band){ // Schaltausgang als Streifen unten im Diagramm
    g.fillStyle=farbe("--ok");g.globalAlpha=.55;let start=null,prev=null;
    const zu=t=>{if(start!==null)g.fillRect(X(start),T+ph-7,Math.max(1,X(t)-X(start)),7);start=null};
    for(const p of pts){const b=o.band(p);if(prev!==null&&p[0]-prev>gap)zu(prev);if(b===1&&start===null)start=p[0];if(b!==1&&start!==null)zu(p[0]);prev=p[0]}
    if(start!==null)zu(prev);g.globalAlpha=1;
  }
  for(const l of linien){
    // pro Pixelspalte min/max zeichnen, damit auch 100.000+ Punkte fluessig bleiben
    g.strokeStyle=l.farbe;g.lineWidth=l.breite;g.lineJoin="round";g.beginPath();
    let prevT=null,col=-1,cmin=0,cmax=0,cfirst=0,clast=0;
    const flush=()=>{if(col<0)return;g.lineTo(col,Y(cfirst));if(cmin!==cmax){g.lineTo(col,Y(cmin));g.lineTo(col,Y(cmax))}g.lineTo(col,Y(clast))};
    for(const p of pts){const t=p[0],v=l.fn(p),x=Math.round(X(t));
      if(prevT===null||t-prevT>gap){flush();col=-1;g.moveTo(x,Y(v))}
      if(x!==col){flush();col=x;cmin=cmax=cfirst=clast=v}else{if(v<cmin)cmin=v;if(v>cmax)cmax=v;clast=v}
      prevT=t}
    flush();g.stroke();
  }
  const l=linien[linien.length-1],p=pts[pts.length-1];g.fillStyle=l.farbe;g.beginPath();g.arc(X(p[0]),Y(l.fn(p)),4,0,7);g.fill();
}

function zeichne(){if(ansicht==="temp")zeichneTemp();else if(ansicht==="ind")zeichneInd()}
function zeichneTemp(){
  const data=S.temp.data,pts=sichtbar(data,+$("t_fen").value),kalAn=istKal();
  if(data.length){const last=data[data.length-1][1];$("t_cur").textContent=fmt(KT(last),2);$("t_curraw").textContent=kalAn?"Rohwert "+fmt(last,1):""}
  let mn=Infinity,mx=-Infinity,sum=0;for(const p of pts){const v=KT(p[1]);if(v<mn)mn=v;if(v>mx)mx=v;sum+=v}
  if(pts.length){$("t_min").textContent=fmt(mn,2);$("t_max").textContent=fmt(mx,2);$("t_avg").textContent=fmt(sum/pts.length,2)}
  $("t_info").textContent=data.length.toLocaleString("de-DE")+" Messwerte gesamt, "+pts.length.toLocaleString("de-DE")+" im Fenster";
  const linien=[];if(kalAn)linien.push({fn:p=>p[1],farbe:farbe("--raw"),breite:1});linien.push({fn:p=>KT(p[1]),farbe:farbe("--acc"),breite:2});
  zeitgraph($("t_c"),pts,linien,{fen:+$("t_fen").value,achse:" °",minSpan:.5});
}
function zeichneInd(){
  const data=S.ind.data,pts=sichtbar(data,+$("i_fen").value),hatSw=dek.schaltbit>=0;
  if(data.length){const [,r,sw]=data[data.length-1];$("i_cur").textContent=iFmt(IV(r));
    const aus=iF&&(r<Math.min(iF.lo,iF.hi)||r>Math.max(iF.lo,iF.hi));
    $("i_curraw").innerHTML=(iF?"Rohwert "+dz(r,rohDez())+" "+(dek.einheit||""):"")+(aus?' <span class="warn">· außerhalb des kalibrierten Bereichs</span>':"");
    $("i_sw").hidden=!hatSw||sw==null;$("i_sw").textContent="Schaltausgang "+(sw?"EIN":"AUS");$("i_sw").className="sw"+(sw?" an":"")}
  let mn=Infinity,mx=-Infinity,sum=0,sq=0;for(const p of pts){const v=IV(p[1]);if(v<mn)mn=v;if(v>mx)mx=v;sum+=v;sq+=v*v}
  if(pts.length){const n=pts.length,m=sum/n;$("i_min").textContent=iFmt(mn);$("i_max").textContent=iFmt(mx);$("i_span").textContent=iFmt(mx-mn,1);
    $("i_sig").textContent=n>1?iFmt(Math.sqrt(Math.max(0,(sq-n*m*m)/(n-1))),1):"–"}
  $("i_info").textContent=data.length.toLocaleString("de-DE")+" Messwerte gesamt, "+pts.length.toLocaleString("de-DE")+" im Fenster";
  $("i_legsw").style.display=hatSw?"":"none";
  zeitgraph($("i_c"),pts,[{fn:p=>IV(p[1]),farbe:farbe("--acc2"),breite:2}],
    {fen:+$("i_fen").value,achse:iF?" mm":"",minSpan:iF?0.01:Math.abs(dek.faktor)*4||1,band:hatSw?p=>p[2]:null});
  zeichneKennlinie(data.length?data[data.length-1][1]:null);
}
function zeichneKennlinie(aktuell){
  const [g,W,H]=leinwand($("i_kc")),L=58,R=10,T=10,B=34,pw=W-L-R,ph=H-T-B,pk=ikal.punkte||[];
  if(!pk.length){g.fillText("Noch keine Kalibrierpunkte.",L,T+20);return}
  let x0=Math.min(...pk.map(p=>p.roh)),x1=Math.max(...pk.map(p=>p.roh));if(aktuell!=null){x0=Math.min(x0,aktuell);x1=Math.max(x1,aktuell)}
  if(x1-x0<1e-12){x0-=1;x1+=1}const px=(x1-x0)*.08;x0-=px;x1+=px;
  const kurve=[];if(iF)for(let i=0;i<=120;i++){const r=x0+(x1-x0)*i/120;kurve.push([r,iF(r)])}
  const ys=pk.map(p=>p.mm).concat(kurve.map(k=>k[1]));let y0=Math.min(...ys),y1=Math.max(...ys);if(y1-y0<1e-9){y0-=.5;y1+=.5}const py=(y1-y0)*.08;y0-=py;y1+=py;
  const X=r=>L+(r-x0)/(x1-x0)*pw,Y=v=>T+(y1-v)/(y1-y0)*ph;
  g.strokeStyle=farbe("--line");g.lineWidth=1;g.textAlign="right";g.textBaseline="middle";
  const sy=nice(y1-y0),dy=Math.min(6,Math.max(0,-Math.floor(Math.log10(sy)+1e-9)));
  for(let i=Math.ceil(y0/sy);i*sy<=y1;i++){const y=Math.round(Y(i*sy))+.5;g.beginPath();g.moveTo(L,y);g.lineTo(W-R,y);g.stroke();g.fillText(dz(i*sy,dy),L-6,y)}
  const sx=nice(x1-x0),dx=Math.min(6,Math.max(0,-Math.floor(Math.log10(sx)+1e-9)));g.textAlign="center";g.textBaseline="top";
  for(let i=Math.ceil(x0/sx);i*sx<=x1;i++)g.fillText(dz(i*sx,dx),X(i*sx),H-B+5);
  g.fillText("Rohwert"+(dek.einheit?" ("+dek.einheit+")":""),L+pw/2,H-14);
  g.save();g.translate(12,T+ph/2);g.rotate(-Math.PI/2);g.fillText("Abstand mm",0,-4);g.restore();
  if(kurve.length){g.strokeStyle=farbe("--acc2");g.lineWidth=2;g.beginPath();kurve.forEach(([r,v],i)=>i?g.lineTo(X(r),Y(v)):g.moveTo(X(r),Y(v)));g.stroke()}
  g.fillStyle=farbe("--fg");for(const p of pk){g.beginPath();g.arc(X(p.roh),Y(p.mm),3.5,0,7);g.fill()}
  if(aktuell!=null){g.strokeStyle=farbe("--acc");g.setLineDash([4,3]);g.beginPath();g.moveTo(X(aktuell)+.5,T);g.lineTo(X(aktuell)+.5,T+ph);g.stroke();g.setLineDash([]);
    if(iF){g.fillStyle=farbe("--acc");g.beginPath();g.arc(X(aktuell),Y(iF(aktuell)),4.5,0,7);g.fill()}}
}
// ---------- Verbindung
let ipGeaendert=false;
$("v_ip").oninput=()=>ipGeaendert=true;
const uhr=ts=>new Date(ts*1000).toLocaleTimeString("de-DE");
function dauer(sek){sek=Math.max(0,Math.round(sek));if(sek<60)return sek+" s";const m=Math.floor(sek/60);if(m<60)return m+" min "+(sek%60)+" s";return Math.floor(m/60)+" h "+(m%60)+" min"}
const esc=t=>String(t??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
async function verbinden(ip){
  const [ok,r]=await post("/api/verbindung",{master:ip});
  if(ok){ipGeaendert=false;$("v_ip").value=r.master;meld("v_msg","Verbinde mit "+r.master+" …")}else meld("v_msg",r.fehler,true);
  poll();
}
$("v_verbinden").onclick=()=>verbinden($("v_ip").value);
$("v_ip").onkeydown=e=>{if(e.key==="Enter")verbinden($("v_ip").value)};
$("v_suchen").onclick=async()=>{
  const b=$("v_suchen");b.disabled=true;b.textContent="suche …";$("v_such").innerHTML="";
  try{const [,r]=await post("/api/verbindung/suchen");
    if(!r.gefunden.length){$("v_such").innerHTML=`<div class="hint warn">Kein Master gefunden (geprüft: ${r.geprueft.map(esc).join(", ")||"–"}). Kabel/Strom prüfen; bei unbekannter IP siehe README (LLDP).</div>`}
    else{$("v_such").innerHTML='<table><thead><tr><th class="l">IP</th><th class="l">Gerät</th><th class="l">Seriennr.</th><th></th></tr></thead><tbody>'+
      r.gefunden.map(g=>`<tr><td class="l">${esc(g.ip)}</td><td class="l">${esc([g.vendor,g.productcode].filter(Boolean).join(" "))}</td><td class="l">${esc(g.serialnumber)}</td><td>${g.aktiv?"verbunden":`<button data-ip="${esc(g.ip)}">Verwenden</button>`}</td></tr>`).join("")+"</tbody></table>";
      for(const x of $("v_such").querySelectorAll("button[data-ip]"))x.onclick=()=>verbinden(x.dataset.ip)}
  }finally{b.disabled=false;b.textContent="Master suchen"}
};
$("v_auto").onchange=async e=>{await post("/api/verbindung/auto_ip",{an:e.target.checked})};
$("v_pcip").onclick=async()=>{const b=$("v_pcip");b.disabled=true;try{const [,r]=await post("/api/verbindung/pc_ip");meld("v_pcmsg",r.text,!/gesetzt/.test(r.text))}finally{b.disabled=false}poll()};
async function pollVerb(id){
  try{
    const r=await (await fetch("/api/verbindung")).json();
    if(id!==pollId)return;
    const v=r.verbindung,jetzt=Date.now()/1000,inf=v.info||{};
    $("v_kachel").className="card big vstat "+(v.verbunden?"ok":"err");
    $("v_ipanz").textContent=r.master;
    $("v_stat").textContent=v.verbunden?"Verbunden":"Getrennt";
    $("v_seit").textContent=(v.seit?(v.verbunden?"seit ":"getrennt seit ")+dauer(jetzt-v.seit):"")+(v.verbunden?"":(v.fehler?" · "+v.fehler:""));
    $("v_lat").textContent=v.verbunden&&v.latenz_ms!=null?dz(v.latenz_ms,0)+" ms":"–";
    $("v_rate").textContent=v.verbunden?dz(r.rate_ist,1):"–";
    $("v_abbr").textContent=v.abbrueche;
    if(!ipGeaendert&&document.activeElement!==$("v_ip"))$("v_ip").value=r.master;
    $("v_info").innerHTML=v.verbunden&&inf.productcode?[["Gerät",[inf.vendor,inf.productcode].filter(Boolean).join(" ")],["Seriennummer",inf.serialnumber],["Firmware",inf.swrevision],["Hardware",inf.hwrevision]]
      .map(([a,b])=>`<tr><td class="l" style="color:var(--mut)">${a}</td><td class="l">${esc(b||"–")}</td></tr>`).join(""):"";
    const nutz={};for(const [k,p] of Object.entries(r.kanal_ports))if(p)(nutz[p]=nutz[p]||[]).push(k==="temp"?"Temperatur":"Induktiv");
    $("v_ports").innerHTML=v.ports.length?v.ports.map(p=>`<tr><td>${p.port}</td><td class="l">${esc(p.modus??"–")}</td><td class="l">${esc(p.status??"–")}</td><td class="l">${esc(p.geraet)}</td><td class="l">${(nutz[p.port]||[]).join(", ")}</td></tr>`).join("")
      :'<tr><td colspan="5" style="text-align:center;color:var(--mut)">'+(v.verbunden?"wird geladen …":"Master nicht verbunden")+"</td></tr>";
    const ll=r.pc_linklocal;
    $("v_pc").innerHTML=ll.length?`<div>Link-Local-Adresse des PCs: <b>${ll.map(esc).join(", ")}</b></div>`
      :'<div class="warn">Der PC hat keine 169.254.x.x-Adresse – ohne sie ist ein Master mit 169.254-IP nicht erreichbar.</div>';
    const linux=r.os.startsWith("linux");$("v_pclinux").hidden=!linux;
    if(document.activeElement!==$("v_auto"))$("v_auto").checked=r.auto_ip;
    $("v_pchint").innerHTML=linux?"":r.os==="win32"?
      'Windows vergibt ohne DHCP nach ca. 1 Minute selbst eine 169.254.x.x-Adresse. Sonst: Einstellungen → Netzwerk → Ethernet → IP-Zuweisung → Manuell (z. B. 169.254.33.10, Maske 255.255.0.0).'
      :'macOS vergibt meist selbst eine 169.254.x.x-Adresse. Sonst: Systemeinstellungen → Netzwerk → Ethernet-Adapter → Details → TCP/IP → Manuell (z. B. 169.254.33.10, Maske 255.255.0.0).';
    $("v_log").innerHTML=r.log.length?r.log.map(([t,a,x])=>`<div><span class="t">${uhr(t)}</span><span class="${a}">${esc(x)}</span></div>`).join(""):'<div class="hint">noch keine Ereignisse</div>';
    $("sline").textContent="Master "+r.master+(inf.productcode&&v.verbunden?" · "+[inf.vendor,inf.productcode].filter(Boolean).join(" "):"");
    $("banner").hidden=true;
    $("dot").style.background=v.verbunden?"var(--ok)":"var(--err)";
    $("stxt").textContent=v.verbunden?`verbunden · ${dz(r.rate_ist,1)} Werte/s`:"Master getrennt";
  }catch(e){if(id!==pollId)return;$("dot").style.background="var(--err)";$("stxt").textContent="Programm nicht erreichbar"}
  timer=setTimeout(poll,1000);
}

zeigeDekoder(dek);zeige();
</script></body></html>
"""

if __name__ == "__main__":
    url = f"http://localhost:{args.web}"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.web), Handler)
    except OSError:  # Port belegt -> meist laeuft der Monitor schon (z. B. Doppelklick auf die .exe)
        print(f"Port {args.web} ist schon belegt - laeuft der Monitor bereits? Oeffne {url}")
        if not args.kein_browser:
            webbrowser.open(url)
        time.sleep(3)
        sys.exit(1)
    threading.Thread(target=poll_loop, daemon=True).start()
    print(f"IO-Link Monitor laeuft: {url}   (Master {master.host}, alle {cfg['intervall']}s)")
    print(f"  Temperatur: Port {kanaele['temp'].port or 'aus'}  ->  {url}/#temp")
    print(f"  Induktiv:   Port {kanaele['ind'].port or 'aus'}  ->  {url}/#ind")
    print("Beenden mit Strg+C")
    if not args.kein_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    print("\nBeendet.")
