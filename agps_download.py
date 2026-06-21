#!/usr/bin/env python3
"""
Free AGPS data generator for u-blox M10.

Downloads today's RINEX 3 mixed broadcast navigation file from BKG/IGS
(no registration, no token required) and converts GPS ephemeris + current
time to UBX-MGA binary format for use with ubx_m10_send_agps().

Sources (tried in order, all free):
  - BKG (Germany):  https://igs.bkg.bund.de/root_ftp/IGS/BRDC/...
  - EUREF (BKG):    https://igs.bkg.bund.de/root_ftp/EUREF/BRDC/...

Usage:
    # Write a raw .ubx file
    python3 agps_download.py [--output agps.ubx] [--date YYYY-MM-DD]
                             [--max-age-h 4] [--stats]

    # Online hot start — stream straight to the board and trigger the fix.
    # Position is seeded automatically (IP geolocation, Portugal fallback);
    # use --pos=pt to force Portugal offline, or --pos=lat,lon to set it.
    python3 agps_download.py --port /dev/ttyACM0

    # Online hot start — print shell lines to paste into the board terminal
    python3 agps_download.py --format shell

Ephemeris source (--source): 'hourly' (default via auto) pulls per-station hourly
broadcast nav (age < 1 h → true hot start); falls back to the daily file.

The data contains:
  - UBX-MGA-INI-TIME-UTC  (current time injection)
  - UBX-MGA-INI-POS-LLH   (optional, with --pos)
  - UBX-MGA-GPS-EPH       (one frame per fresh GPS satellite)

On the board the GNSS test consumes it via the shell:
    test gnss agps init          # power/init the M10
    test gnss agps <hexbytes>    # one UBX-MGA chunk per line (repeated)
    test gnss agps fix [timeout] # wait for the hot-start fix, print the TTFF
"""

import argparse
import gzip
import json
import math
import struct
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

PI = math.pi

# ── Free RINEX 3 BRDC source URLs (BKG, no auth) ────────────────────────────
# Long-filename RINEX 3 mixed nav (GPS + GLONASS + Galileo + BeiDou)
_BKG = "https://igs.bkg.bund.de/root_ftp"
BRDC_URLS = [
    _BKG + "/IGS/BRDC/{year:04d}/{doy:03d}/BRDM00DLR_S_{year:04d}{doy:03d}0000_01D_MN.rnx.gz",
    _BKG + "/EUREF/BRDC/{year:04d}/{doy:03d}/BRDM00DLR_R_{year:04d}{doy:03d}0000_01D_MN.rnx.gz",
    # Short-filename fallback (older naming)
    _BKG + "/IGS/BRDC/{year:04d}/{doy:03d}/BRDM{year:04d}{doy:03d}0.rnx.gz",
]

# UBX-MGA constants
UBX_CLASS_MGA       = 0x13
MGA_GPS             = 0x00   # UBX-MGA-GPS-EPH
MGA_INI             = 0x40   # UBX-MGA-INI-*
GPS_LEAP_SECONDS    = 18     # GPS–UTC offset since 2017-01-01

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)


# ── UBX frame builder ────────────────────────────────────────────────────────

def _ubx_checksum(data: bytes):
    a = b = 0
    for byte in data:
        a = (a + byte) & 0xFF
        b = (b + a) & 0xFF
    return a, b


def make_ubx_frame(cls: int, msg_id: int, payload: bytes) -> bytes:
    header = bytes([0xB5, 0x62, cls, msg_id]) + struct.pack("<H", len(payload))
    body = header + payload
    ck_a, ck_b = _ubx_checksum(body[2:])
    return body + bytes([ck_a, ck_b])


# ── UBX-MGA-INI-TIME-UTC (inject current UTC time) ──────────────────────────

def make_mga_ini_time_utc(dt: datetime) -> bytes:
    """
    UBX-MGA-INI-TIME-UTC payload (24 bytes).
    Tells the M10 the current time so it skips the 30-second subframe wait.
    """
    payload = struct.pack(
        "<BBHbBHBBBBBBIHI",
        0x10,               # type = TIME_UTC
        0,                  # version
        0,                  # ref (U2)
        GPS_LEAP_SECONDS,   # leapSecs (I1)
        0,                  # reserved1
        dt.year,            # year (U2)
        dt.month,           # month
        dt.day,             # day
        dt.hour,            # hour
        dt.minute,          # minute
        dt.second,          # second
        0,                  # reserved2
        0,                  # ns (U4, 0 = whole seconds)
        2,                  # tAccS: 2 s accuracy
        0,                  # tAccNs (U4)
    )
    assert len(payload) == 24
    return make_ubx_frame(UBX_CLASS_MGA, MGA_INI, payload)


# ── UBX-MGA-INI-POS-LLH (inject approximate position) ───────────────────────

def make_mga_ini_pos_llh(lat_deg: float, lon_deg: float, alt_m: float,
                         pos_acc_m: float = 10.0) -> bytes:
    """
    UBX-MGA-INI-POS-LLH payload (20 bytes).
    Seeds the receiver with a rough position so it can prune the satellite
    search — together with time + ephemeris this gives a hot start.
    """
    payload = struct.pack(
        "<BBHiiiI",
        0x01,                       # type = POS_LLH
        0,                          # version
        0,                          # reserved1 (U2)
        int(round(lat_deg * 1e7)),  # lat  (I4, 1e-7 deg)
        int(round(lon_deg * 1e7)),  # lon  (I4, 1e-7 deg)
        int(round(alt_m * 100)),    # alt  (I4, cm)
        int(round(pos_acc_m * 100)),# posAcc (U4, cm)
    )
    assert len(payload) == 20
    return make_ubx_frame(UBX_CLASS_MGA, MGA_INI, payload)


# ── URA index lookup ─────────────────────────────────────────────────────────

_URA_BOUNDS = [2.4, 3.4, 4.85, 6.85, 9.65, 13.65, 24.0,
               48.0, 96.0, 192.0, 384.0, 768.0, 1536.0, 3072.0, 6144.0]

def _ura_index(sv_acc_m: float) -> int:
    if sv_acc_m < 0:
        return 1
    for i, bound in enumerate(_URA_BOUNDS):
        if sv_acc_m <= bound:
            return i
    return 15


def _clamp(v, lo, hi):
    return max(lo, min(hi, int(round(v))))


# ── GPS ephemeris → UBX-MGA-GPS-EPH (68-byte payload) ───────────────────────

def gps_ephem_to_ubx(rec: dict) -> bytes:
    """
    Convert a parsed RINEX 3 GPS nav record to UBX-MGA-GPS-EPH.

    RINEX 3 stores angular parameters in radians (GPS ICD uses semicircles).
    UBX-MGA-GPS-EPH stores angles in semicircles with LSB = 2^-31.
    Conversion: value_ubx = round(value_rad / PI / 2^-31)
               = round(value_rad * 2^31 / PI)
    """
    # ── angular scale helpers ────────────────────────────────────────────────
    def rad_to_semi_i4(v):   # I4, 2^-31 semicircles/LSB
        return _clamp(v / PI / 2**-31, -(2**31), 2**31 - 1)
    def rad_to_semi_i2(v):   # I2, 2^-43 semicircles/LSB
        return _clamp(v / PI / 2**-43, -(2**15), 2**15 - 1)
    def rad_to_rad_i2(v):    # I2, 2^-29 rad/LSB (Cuc, Cus, Cic, Cis)
        return _clamp(v / 2**-29, -(2**15), 2**15 - 1)

    prn    = _clamp(rec["prn"], 1, 32)
    ura    = _ura_index(rec["sv_acc"])
    health = rec["sv_health"] & 0x3F
    fit    = 1 if rec["fit_int"] > 4.0 else 0

    # Clock params
    af0  = _clamp(rec["af0"] / 2**-31,    -(2**31), 2**31 - 1)
    af1  = _clamp(rec["af1"] / 2**-43,    -(2**15), 2**15 - 1)
    af2  = _clamp(rec["af2"] / 2**-55,    -128, 127)
    tgd  = _clamp(rec["TGD"] / 2**-31,    -128, 127)
    iodc = rec["IODC"] & 0x3FF
    toc  = int(rec["toc_sow"] / 16) & 0xFFFF
    toe  = int(rec["toe_sow"] / 16) & 0xFFFF

    # Orbit params
    crs    = _clamp(rec["Crs"]  / 2**-5,  -(2**15), 2**15 - 1)
    crc    = _clamp(rec["Crc"]  / 2**-5,  -(2**15), 2**15 - 1)
    cuc    = rad_to_rad_i2(rec["Cuc"])
    cus    = rad_to_rad_i2(rec["Cus"])
    cic    = rad_to_rad_i2(rec["Cic"])
    cis    = rad_to_rad_i2(rec["Cis"])
    e      = _clamp(rec["e"]    / 2**-33, 0, 2**32 - 1)
    sqrtA  = _clamp(rec["sqrtA"]/ 2**-19, 0, 2**32 - 1)

    deltaN  = rad_to_semi_i2(rec["Delta_n"])
    m0      = rad_to_semi_i4(rec["M0"])
    omega0  = rad_to_semi_i4(rec["Omega0"])
    i0      = rad_to_semi_i4(rec["i0"])
    omega   = rad_to_semi_i4(rec["omega"])
    omDot   = rad_to_semi_i4(rec["Omega_dot"])   # I4 in UBX (not I2!)
    idot    = rad_to_semi_i2(rec["IDOT"])

    # UBX-MGA-GPS-EPH payload: 68 bytes
    # Format verified: struct.calcsize("<BBBBBbHHBbhihhihhIIHhihhiiihI") == 68
    payload = struct.pack(
        "<BBBBBbHHBbhihhihhIIHhihhiiihI",
        prn,     # U1  svId
        0,       # U1  reserved1
        fit,     # U1  fitInterval
        ura,     # U1  uraIndex
        health,  # U1  svHealth
        tgd,     # I1  tgd
        iodc,    # U2  iodc
        toc,     # U2  toc       (x16 s)
        0,       # U1  reserved2
        af2,     # I1  af2
        af1,     # I2  af1
        af0,     # I4  af0
        crs,     # I2  crs
        deltaN,  # I2  deltaN
        m0,      # I4  m0        (semicircles, x2^-31)
        cuc,     # I2  cuc
        cus,     # I2  cus
        e,       # U4  e
        sqrtA,   # U4  sqrtA
        toe,     # U2  toe       (x16 s)
        cic,     # I2  cic
        omega0,  # I4  omega0
        cis,     # I2  cis
        crc,     # I2  crc
        i0,      # I4  i0
        omega,   # I4  omega
        omDot,   # I4  omegaDot
        idot,    # I2  idot
        0,       # U4  reserved3
    )
    assert len(payload) == 68, f"GPS EPH payload size: {len(payload)}"
    return make_ubx_frame(UBX_CLASS_MGA, MGA_GPS, payload)


# ── RINEX 3 download ─────────────────────────────────────────────────────────

def _doy(dt: datetime) -> int:
    return dt.timetuple().tm_yday


def download_rinex(date: datetime) -> str:
    year, doy = date.year, _doy(date)
    for template in BRDC_URLS:
        url = template.format(year=year, doy=doy)
        print(f"Trying {url}", file=sys.stderr)
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                raw = r.read()
            if raw[:2] == b"\x1f\x8b":   # gzip magic
                raw = gzip.decompress(raw)
            text = raw.decode("ascii", errors="replace")
            print(f"  OK — {len(text)} chars", file=sys.stderr)
            return text
        except urllib.error.URLError as exc:
            print(f"  Failed: {exc}", file=sys.stderr)
    raise RuntimeError(
        "Could not download RINEX from any source. "
        "Try --date YYYY-MM-DD for a past date (yesterday's file is always available)."
    )


# ── Hourly RINEX 3 BRDC (fresh ephemeris for a true hot start) ──────────────
# BKG near-real-time tree: /IGS/nrt/{doy}/{hh}/{STATION}_R_{yyyy}{doy}{hh}00_01H_MN.rnx.gz
# Each file is one station's broadcast nav for that hour (toe within ~1 h). A
# few stations are merged for full GPS coverage; Iberian/European ones first so
# the satellites visible from Portugal are covered with the freshest ephemeris.
_BKG_NRT = _BKG + "/IGS/nrt"
HOURLY_STATIONS = [
    "EBRE00ESP", "CEBR00ESP", "CACE00ESP", "CANT00ESP", "ALAC00ESP",  # Iberia
    "BRUX00BEL", "BRST00FRA", "GANP00SVK", "BUCU00ROU",               # Europe
    "AREG00PER", "GAMG00KOR", "FAA100PYF", "ABMF00GLP",               # global
]


def download_rinex_hourly(now: datetime, max_back: int = 4,
                          min_stations: int = 4) -> tuple:
    """Download the latest available hourly broadcast nav from a few stations.
    Returns (concatenated_text, reference_datetime) or (None, None)."""
    for back in range(max_back):
        t = now - timedelta(hours=back)
        year, doy, hh = t.year, _doy(t), t.hour
        texts = []
        for st in HOURLY_STATIONS:
            url = (f"{_BKG_NRT}/{doy:03d}/{hh:02d}/"
                   f"{st}_R_{year:04d}{doy:03d}{hh:02d}00_01H_MN.rnx.gz")
            try:
                with urllib.request.urlopen(url, timeout=20) as r:
                    raw = r.read()
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                texts.append(raw.decode("ascii", errors="replace"))
                print(f"  hourly {st} {doy:03d}/{hh:02d}h OK", file=sys.stderr)
            except urllib.error.URLError:
                continue
            if len(texts) >= min_stations:
                break
        if texts:
            print(f"Hourly ephemeris: {len(texts)} station(s) @ {doy:03d}/{hh:02d}h UTC",
                  file=sys.stderr)
            return "\n".join(texts), t
    return None, None


# ── RINEX 3 GPS navigation parser ────────────────────────────────────────────

def _f(s: str) -> float:
    try:
        return float(s.strip().replace("D", "e").replace("d", "e"))
    except ValueError:
        return 0.0


def _row(line: str):
    """Parse 4 RINEX data fields from one broadcast orbit line."""
    return [_f(line[4 + i * 19: 23 + i * 19]) for i in range(4)]


def _toc_to_sow(year, month, day, hour, minute, second) -> float:
    """Convert RINEX toc epoch to GPS seconds-of-week."""
    if year < 100:
        year += 2000
    dt_utc = datetime(year, month, day, hour, minute, int(second),
                      tzinfo=timezone.utc)
    gps_sec = (dt_utc - GPS_EPOCH).total_seconds() + GPS_LEAP_SECONDS
    return gps_sec % 604800


def parse_gps_nav(text: str) -> list:
    records = []
    lines = text.splitlines()
    in_header = True
    i = 0

    while i < len(lines):
        line = lines[i]
        # A new RINEX file may be concatenated (hourly multi-station merge):
        # re-enter header mode whenever a new version line appears.
        if "RINEX VERSION / TYPE" in line:
            in_header = True
        if in_header:
            if "END OF HEADER" in line:
                in_header = False
            i += 1
            continue

        # GPS SV record starts with 'G'
        if len(line) < 4 or line[0] != "G":
            i += 1
            continue

        try:
            prn  = int(line[1:3])
            year = int(line[4:8])
            mon  = int(line[9:11])
            day  = int(line[12:14])
            hr   = int(line[15:17])
            mn   = int(line[18:20])
            sc   = float(line[21:23])
            af0  = _f(line[23:42])
            af1  = _f(line[42:61])
            af2  = _f(line[61:80])
        except (ValueError, IndexError):
            i += 1
            continue

        # Read 7 broadcast orbit lines
        orbit = []
        for _ in range(7):
            i += 1
            if i < len(lines):
                orbit.append(_row(lines[i]))
        i += 1

        if len(orbit) < 7:
            continue

        # Unpack per RINEX 3 broadcast message spec (angles in radians)
        IODE,    Crs,    Delta_n,  M0      = orbit[0]
        Cuc,     e,      Cus,      sqrtA   = orbit[1]
        toe_sow, Cic,    Omega0,   Cis     = orbit[2]
        i0,      Crc,    omega,    Omega_dot = orbit[3]
        IDOT,    L2codes, gps_week, L2P    = orbit[4]
        sv_acc,  sv_hlth, TGD,     IODC   = orbit[5]
        trans_t, fit_int                  = orbit[6][0], orbit[6][1]

        toc_sow = _toc_to_sow(year, mon, day, hr, mn, sc)

        records.append({
            "prn": prn,
            "toc_sow": toc_sow, "toe_sow": toe_sow,
            "gps_week": int(gps_week),
            "af0": af0, "af1": af1, "af2": af2,
            "Crs": Crs, "Delta_n": Delta_n, "M0": M0,
            "Cuc": Cuc, "e": e, "Cus": Cus, "sqrtA": sqrtA,
            "Cic": Cic, "Omega0": Omega0, "Cis": Cis,
            "i0": i0, "Crc": Crc, "omega": omega, "Omega_dot": Omega_dot,
            "IDOT": IDOT, "sv_acc": sv_acc, "sv_health": int(sv_hlth),
            "TGD": TGD, "IODC": int(IODC), "fit_int": fit_int,
        })

    return records


# ── Freshness filter ─────────────────────────────────────────────────────────

def filter_fresh_now(records: list, now: datetime, max_age_h: float) -> list:
    """Keep the freshest record per PRN whose ephemeris (toc) is within
    max_age_h of the current time. Age is the absolute GPS time difference,
    so a toe slightly in the future (common) is handled correctly."""
    _, now_sow = _week_sow(now)
    best = {}

    for r in records:
        d = (now_sow - r["toc_sow"]) % 604800
        if d > 302400:          # take the shorter way round the week
            d -= 604800
        age_s = abs(d)
        if age_s <= max_age_h * 3600:
            prn = r["prn"]
            if prn not in best or age_s < best[prn][0]:
                best[prn] = (age_s, r)

    return [v for _, v in sorted(best.values(), key=lambda x: x[1]["prn"])]


def filter_fresh(records: list, rinex_date: datetime, max_age_h: float) -> list:
    """Keep the most recent record per PRN within max_age_h of the RINEX file date."""
    _, ref_sow = _week_sow(rinex_date)
    # End of the reference day in GPS SOW
    end_sow = (ref_sow + 86400) % 604800
    best = {}

    for r in records:
        toc = r["toc_sow"]
        # Age = how many seconds before the end of the RINEX day
        age_s = (end_sow - toc) % 604800
        if age_s <= max_age_h * 3600:
            prn = r["prn"]
            if prn not in best or age_s < best[prn][0]:
                best[prn] = (age_s, r)

    return [v for _, v in sorted(best.values(), key=lambda x: x[1]["prn"])]


def _week_sow(dt: datetime):
    gps_sec = (dt - GPS_EPOCH).total_seconds() + GPS_LEAP_SECONDS
    return int(gps_sec // 604800), gps_sec % 604800


# ── Statistics ───────────────────────────────────────────────────────────────

def print_stats(frames: list, fresh: list):
    print(f"\nAGPS file summary:")
    print(f"  Total UBX frames : {len(frames)}")
    print(f"  GPS satellites   : {len(fresh)}")
    if fresh:
        prns = sorted(r["prn"] for r in fresh)
        print(f"  PRNs included    : {prns}")


# ── Approximate position resolver (for the hot-start seed) ──────────────────

# Central-Portugal fallback — good enough to seed the receiver search (a few
# tens of km of accuracy is plenty for UBX-MGA-INI-POS-LLH).
PT_DEFAULT = (39.5, -8.0, 100.0)


def geolocate_ip():
    """Approximate position from the PC's public IP (free, no key, no HTTPS).
    Returns (lat, lon, city, country)."""
    url = "http://ip-api.com/json/?fields=status,message,lat,lon,city,country"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read().decode())
    if data.get("status") != "success":
        raise RuntimeError(data.get("message", "ip-api lookup failed"))
    return float(data["lat"]), float(data["lon"]), data.get("city", ""), data.get("country", "")


def resolve_pos(spec):
    """Resolve a --pos spec to (lat, lon, alt) or None to skip injection.

    Accepts: 'auto' (IP geolocation, Portugal fallback), 'pt'/'portugal'
    (fixed Portugal, no network), 'none'/'off'/'' (skip), or 'lat,lon[,alt]'.
    """
    s = (spec or "").strip().lower()
    if s in ("", "none", "off", "no"):
        return None
    if s in ("pt", "portugal"):
        print(f"Position: Portugal default {PT_DEFAULT[0]},{PT_DEFAULT[1]}", file=sys.stderr)
        return PT_DEFAULT
    if s == "auto":
        try:
            lat, lon, city, country = geolocate_ip()
            print(f"Position (IP geolocation): {city}, {country} → {lat},{lon}", file=sys.stderr)
            return (lat, lon, 100.0)
        except Exception as exc:
            print(f"IP geolocation failed ({exc}); using Portugal fallback", file=sys.stderr)
            return PT_DEFAULT
    # Manual 'lat,lon[,alt]'
    try:
        parts = [float(x) for x in spec.split(",")]
        lat, lon = parts[0], parts[1]
        alt = parts[2] if len(parts) > 2 else 0.0
        return (lat, lon, alt)
    except (ValueError, IndexError):
        raise SystemExit("ERROR: --pos must be 'auto', 'pt', 'none' or 'lat,lon[,alt_m]'")


# ── Device injection (shell hot-start flow) ─────────────────────────────────

def _hex_chunks(binary: bytes, chunk: int):
    for i in range(0, len(binary), chunk):
        yield binary[i:i + chunk].hex()


def emit_shell(binary: bytes, chunk: int, fix_timeout: int) -> str:
    """Render the 'test gnss agps ...' shell command sequence to paste into the
    board's terminal for an online hot start."""
    fix = f"test gnss agps fix {fix_timeout}" if fix_timeout else "test gnss agps fix"
    lines = ["test gnss agps init"]
    lines += [f"test gnss agps {h}" for h in _hex_chunks(binary, chunk)]
    lines.append(fix)
    return "\n".join(lines) + "\n"


def inject_serial(binary: bytes, port: str, baud: int, chunk: int,
                  fix_timeout: int) -> None:
    """Stream the AGPS data straight to the board over its serial console,
    pacing one shell command per line so nothing is dropped."""
    try:
        import serial  # pyserial — only needed for --port
    except ImportError:
        raise SystemExit("ERROR: --port needs pyserial. Install with: pip install pyserial")
    import time

    PROMPT = b"uart:~$"

    ser = serial.Serial(port, baud, timeout=0.1)
    time.sleep(0.3)
    ser.reset_input_buffer()

    def read_until(token, timeout):
        """Read until `token` appears or `timeout` elapses. Returns (found, data)."""
        end = time.time() + timeout
        buf = b""
        while time.time() < end:
            data = ser.read(256)
            if data:
                buf += data
                if token in buf:
                    return True, buf
            else:
                time.sleep(0.005)
        return False, buf

    def send(line, timeout=5.0):
        """Send one shell command, pacing the write so the board's CDC-ACM RX
        ring never overflows, then wait for the prompt before returning."""
        data = (line + "\r\n").encode()
        for i in range(0, len(data), 32):
            ser.write(data[i:i + 32])
            ser.flush()
            time.sleep(0.008)
        return read_until(PROMPT, timeout)

    print(f"Injecting over {port} @ {baud} ...", file=sys.stderr)

    # Get a clean prompt first.
    ser.write(b"\r\n")
    ser.flush()
    read_until(PROMPT, 2.0)

    # init can block while the M10 powers up / first NMEA arrives — allow time.
    ok, _ = send("test gnss agps init", timeout=15.0)
    if not ok:
        print("  warning: no prompt after 'agps init' (M10 powered? antenna?)",
              file=sys.stderr)

    n = 0
    for h in _hex_chunks(binary, chunk):
        ok, _ = send(f"test gnss agps {h}", timeout=5.0)
        if not ok:
            print(f"  warning: no prompt after chunk {n + 1}", file=sys.stderr)
        n += 1
    print(f"  sent {n} UBX chunks ({len(binary)} bytes), waiting for fix ...",
          file=sys.stderr)

    # Trigger the fix and stream the board's response for the operator.
    fix_cmd = f"test gnss agps fix {fix_timeout}" if fix_timeout else "test gnss agps fix"
    ser.write((fix_cmd + "\r\n").encode())
    ser.flush()
    deadline = time.time() + (fix_timeout or 90) + 10
    while time.time() < deadline:
        data = ser.read(256)
        if data:
            sys.stdout.write(data.decode(errors="replace"))
            sys.stdout.flush()
            if b"HOT-START FIX" in data or b"[NOK]" in data:
                break
    ser.close()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Download free AGPS data for u-blox M10")
    ap.add_argument("--output", default="agps.ubx",
                    help="Output UBX binary file (default: agps.ubx)")
    ap.add_argument("--date", default=None,
                    help="RINEX date YYYY-MM-DD (default: today UTC). "
                         "Use yesterday if today's file is not yet uploaded.")
    ap.add_argument("--max-age-h", type=float, default=4.0,
                    help="Maximum ephemeris age in hours (default: 4)")
    ap.add_argument("--source", choices=["auto", "hourly", "daily"], default="auto",
                    help="Ephemeris source: 'hourly' = freshest (<1 h, best hot "
                         "start), 'daily' = whole-day file, 'auto' (default) = "
                         "hourly then fall back to daily")
    ap.add_argument("--stats", action="store_true",
                    help="Print content summary")
    # ── Device hot-start injection ────────────────────────────────────────────
    ap.add_argument("--format", choices=["bin", "shell"], default="bin",
                    help="bin = write .ubx file (default); "
                         "shell = print 'test gnss agps ...' lines to paste")
    ap.add_argument("--port", default=None,
                    help="Serial port of the board (e.g. /dev/ttyACM0). When set, "
                         "the AGPS data is streamed straight to the board and the "
                         "hot start is triggered (needs pyserial).")
    ap.add_argument("--baud", type=int, default=115200,
                    help="Serial baud rate for --port (default: 115200)")
    ap.add_argument("--pos", default="auto",
                    help="Approximate position seed for a stronger hot start. "
                         "'auto' (default) = IP geolocation w/ Portugal fallback; "
                         "'pt' = fixed Portugal (no network); 'none' = skip; or an "
                         "explicit '--pos=lat,lon[,alt_m]' (use '=' so the leading "
                         "minus is not parsed as a flag)")
    ap.add_argument("--chunk", type=int, default=128,
                    help="UBX bytes per shell/serial line (default: 128)")
    ap.add_argument("--fix-timeout", type=int, default=0,
                    help="Seconds to wait for the fix on device (0 = firmware default)")
    ap.add_argument("--no-ini", action="store_true",
                    help="Server mode: emit ephemeris-only (UBX-MGA-GPS-EPH), omitting the "
                         "INI-TIME/INI-POS frames. The device generates time/position itself "
                         "on apply, so a re-applied blob is never stale. Use this for the "
                         "hourly file published at a stable URL.")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)

    all_records = []

    # 1. Hourly source first (freshest → best hot start), unless the user forced
    #    a daily source or a specific past --date.
    if args.source in ("auto", "hourly") and not args.date:
        htext, _ = download_rinex_hourly(now)
        if htext:
            all_records = parse_gps_nav(htext)
            print(f"Parsed {len(all_records)} GPS records from hourly data",
                  file=sys.stderr)

    # 2. Daily source (fallback, or when explicitly requested / dated).
    if not all_records and args.source != "hourly":
        if args.date:
            rinex_date = datetime.strptime(args.date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
        else:
            rinex_date = now
        text = None
        for date_try in [rinex_date, rinex_date - timedelta(days=1)]:
            try:
                text = download_rinex(date_try)
                break
            except RuntimeError:
                pass
        if text:
            all_records = parse_gps_nav(text)
            print(f"Parsed {len(all_records)} GPS records from daily data",
                  file=sys.stderr)

    if not all_records:
        raise SystemExit("ERROR: Could not download ephemeris from any source.")

    # Keep the freshest record per PRN relative to NOW (works for both sources).
    fresh = filter_fresh_now(all_records, now, args.max_age_h)
    print(f"Fresh records (< {args.max_age_h}h): {len(fresh)}", file=sys.stderr)

    if not fresh:
        # Relax to 24 h — better an assisted/warm start than nothing.
        fresh = filter_fresh_now(all_records, now, 24.0)
        print(f"Relaxed to 24h → {len(fresh)} records", file=sys.stderr)

    # Build UBX output
    frames = []

    if args.no_ini:
        # Server mode: ephemeris-only. The device builds INI-TIME/INI-POS at apply time,
        # so a blob re-applied after a reboot is never stale.
        print("Server mode (--no-ini): emitting ephemeris-only, no INI frames", file=sys.stderr)
    else:
        # 1. Time injection (always useful)
        frames.append(make_mga_ini_time_utc(now))

        # 2. Optional position injection (helps the receiver prune the search)
        pos = resolve_pos(args.pos)
        if pos:
            frames.append(make_mga_ini_pos_llh(*pos))
            print(f"Position injected: lat={pos[0]} lon={pos[1]} alt={pos[2]} m", file=sys.stderr)

    # 3. GPS ephemeris frames
    conversion_errors = 0
    for rec in fresh:
        try:
            frames.append(gps_ephem_to_ubx(rec))
        except Exception as exc:
            print(f"  PRN G{rec['prn']:02d}: conversion error — {exc}", file=sys.stderr)
            conversion_errors += 1

    if conversion_errors:
        print(f"Warning: {conversion_errors} records failed conversion", file=sys.stderr)

    if args.stats:
        print_stats(frames, fresh)

    binary = b"".join(frames)

    # ── Output / injection ────────────────────────────────────────────────────
    if args.port:
        # Stream straight to the board and trigger the hot start.
        inject_serial(binary, args.port, args.baud, args.chunk, args.fix_timeout)
        return

    if args.format == "shell":
        # Print the command sequence to paste into the board's terminal.
        sys.stdout.write(emit_shell(binary, args.chunk, args.fix_timeout))
        print(f"\n# {len(binary)} bytes ({len(frames)} UBX frames) — paste the "
              f"lines above into the board shell", file=sys.stderr)
        return

    # Default: write the raw .ubx binary.
    with open(args.output, "wb") as f:
        f.write(binary)

    print(f"\nWrote {len(binary)} bytes ({len(frames)} UBX frames) → {args.output}")
    print("\nInject into the board (online hot start):")
    print(f"    python3 {sys.argv[0]} --port /dev/ttyACM0          # auto-stream + fix")
    print(f"    python3 {sys.argv[0]} --format shell | <paste>     # manual paste")


if __name__ == "__main__":
    main()
