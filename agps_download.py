#!/usr/bin/env python3
"""
Free AGPS data generator for u-blox M10.

Two independent sources of aiding, merged into one file:

  * live RINEX ephemeris: today's broadcast navigation data from BKG/IGS
    hourly stations, no registration and no token, converted to
    UBX-MGA-GPS-EPH. GPS only, precise, valid for its ~4 h fit interval.
  * AssistNow: the u-blox service reached through a Thingstream ZTP token,
    carrying almanac, predicted orbits (MGA-ANO) for four constellations,
    and -- where the device profile allows it -- the ionosphere model,
    satellite health and live ephemeris.

Both go through sanitize_mga_stream() before they are written, which is what
makes one file safe: the AssistNow half is served from a cache and carries its
own timestamps and expiry dates, so its MGA-INI-TIME (the time of the fetch, not
of the download) and any MGA-ANO for a day already past are dropped, along with
live ephemeris past its fit interval and almanac records with impossible orbits.
A structurally broken blob is not written at all, so the previous good file keeps
being served. --assistnow-output writes the AssistNow half to its own file
instead, for anyone who wants the two sources served separately.

Sources (tried in order, all free):
  - BKG hourly NRT:  https://igs.bkg.bund.de/root_ftp/IGS/nrt/{doy}/{hh}/
  - BKG daily BRDC:  https://igs.bkg.bund.de/root_ftp/IGS/BRDC/...

Usage:
    # Write a raw .ubx file (live RINEX ephemeris only)
    python3 agps_download.py [--output agps.ubx] [--date YYYY-MM-DD]
                             [--max-age-h 4] [--stats]

    # Server mode: the single file published at a stable URL (see
    # .github/workflows/agps.yml for the full hourly invocation)
    python3 agps_download.py --no-ini --assistnow-data both
        --output docs/latest.ubx --assistnow-cache docs/agps_assistnow_cache.ubx

    # Check a file someone else produced (frame inventory + field alignment)
    python3 agps_download.py --verify docs/latest.ubx

    # Online hot start -- stream straight to the board and trigger the fix.
    # Position is seeded automatically (IP geolocation, Portugal fallback);
    # use --pos=pt to force Portugal offline, or --pos=lat,lon to set it.
    python3 agps_download.py --port /dev/ttyACM0

    # Online hot start -- print shell lines to paste into the board terminal
    python3 agps_download.py --format shell

Ephemeris source (--source): 'hourly' (default via auto) pulls per-station hourly
broadcast nav (age < 1 h -> true hot start), taking stations one region at a time
until --min-prns satellites have fresh ephemeris; falls back to the daily file.

On the board the GNSS test consumes either file via the shell:
    test gnss agps init          # power/init the M10
    test gnss agps <hexbytes>    # one UBX-MGA chunk per line (repeated)
    test gnss agps fix [timeout] # wait for the hot-start fix, print the TTFF
"""

import argparse
import gzip
import json
import math
import os
import re
import struct
import sys
import urllib.request
import urllib.error
import urllib.parse
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

def _check_gps_eph_layout(payload: bytes) -> None:
    """Raise ValueError if a UBX-MGA-GPS-EPH payload is not laid out correctly.

    Every field offset depends on the two-byte type/version header being there,
    and the UBX checksum is computed over whatever was packed, so a shifted
    payload is accepted by the file format and rejected by the receiver. Reading
    e and sqrtA back at their spec offsets catches that: any misalignment turns
    them into nonsense, while a correct frame always lands in the GPS family
    (sqrtA ~ 5153.6 m^1/2, a ~ 26 560 km).
    """
    if len(payload) != 68:
        raise ValueError("payload is %d bytes, expected 68" % len(payload))
    if payload[0] != 0x01 or payload[1] != 0x00:
        raise ValueError("type/version = %02X/%02X, expected 01/00"
                         % (payload[0], payload[1]))
    if not 1 <= payload[2] <= 32:
        raise ValueError("svId = %d out of range 1..32" % payload[2])
    e = struct.unpack_from("<I", payload, 32)[0] * 2.0 ** -33
    sqrt_a = struct.unpack_from("<I", payload, 36)[0] * 2.0 ** -19
    if not 0.0 <= e < 0.05:
        raise ValueError("e = %.6f implausible -- fields misaligned?" % e)
    if not 5100.0 <= sqrt_a <= 5200.0:
        raise ValueError("sqrtA = %.1f m^1/2 implausible -- fields misaligned?"
                         % sqrt_a)
    # Every GPS satellite shares essentially the same orbit, so the nodal rate
    # is always close to -2.6e-9 semicircles/s. At the ICD's 2^-43 LSB that is
    # about -22 700; a wrong scale factor or offset lands nowhere near it.
    om_dot = struct.unpack_from("<i", payload, 60)[0]
    if not -40000 <= om_dot <= -5000:
        raise ValueError("omegaDot = %d LSB implausible -- wrong scale factor?"
                         % om_dot)


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
    # toc/toe have a 16 s LSB. Round, do not truncate: toc and toe are the
    # same instant for GPS, and truncating a value a hair above the multiple
    # pushed toc one LSB past toe on every single frame.
    toc  = int(round(rec["toc_sow"] / 16)) & 0xFFFF
    toe  = int(round(rec["toe_sow"] / 16)) & 0xFFFF

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
    # omegaDot is I4 with the ICD's 2^-43 LSB, not 2^-31: the field is 4 bytes
    # wide precisely because the ICD gives it 24 bits at 2^-43. Scaling it like
    # the 2^-31 angles collapsed every satellite's nodal rate to about six LSB.
    omDot   = _clamp(rec["Omega_dot"] / PI / 2**-43, -(2**31), 2**31 - 1)
    idot    = rad_to_semi_i2(rec["IDOT"])

    # UBX-MGA-GPS-EPH payload: 68 bytes, starting with the two-byte
    # type/version header every UBX-MGA message carries. Without it the whole
    # payload is shifted by two bytes: the receiver reads svId as the message
    # type and discards the frame (visible as a NAK in UBX-MGA-ACK).
    # struct.calcsize("<BBBBBBBbHHBbhihhihhIIHhihhiiihH") == 68
    payload = struct.pack(
        "<BBBBBBBbHHBbhihhihhIIHhihhiiihH",
        0x01,    # U1  type = EPH
        0x00,    # U1  version
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
        0,       # U2  reserved3
    )
    _check_gps_eph_layout(payload)
    return make_ubx_frame(UBX_CLASS_MGA, MGA_GPS, payload)


# ── RINEX header ionosphere → UBX-MGA-GPS-IONO ──────────────────────────────
# The Klobuchar coefficients sit in the nav file header we already download, in
# IONOSPHERIC CORR rows, and the AssistNow profile is not entitled to them
# ('ukion' counts as a Live component). Off the air GPS repeats them only once
# per 12.5 min page cycle, so a receiver that fixes and goes back to sleep may
# never collect them -- which for a single-band receiver means carrying the full
# ionospheric delay, metres of it, uncorrected.
#
# Scale factors are the GPS ICD ones. That is not taken on trust: the values in
# the header are whole multiples of these LSBs (they are broadcast as 8-bit
# integers at these scales), so _check_gps_iono_layout rejects a set that does
# not quantise cleanly -- a wrong scale factor cannot survive that.

_IONO_ALPHA_LSB = (2.0 ** -30, 2.0 ** -27, 2.0 ** -24, 2.0 ** -24)
_IONO_BETA_LSB = (2.0 ** 11, 2.0 ** 14, 2.0 ** 16, 2.0 ** 16)
MGA_TYPE_IONO = 0x06


def parse_iono_klobuchar(text: str):
    """(alpha[4], beta[4]) from the GPSA/GPSB header rows, or None if absent.

    RINEX 3 lays the row out as A4,1X,4D12.4, and a concatenation of hourly
    station files has one header per station -- the first complete pair wins,
    they are a system-wide broadcast and identical between stations.
    """
    alpha = beta = None
    for line in text.splitlines():
        if "IONOSPHERIC CORR" not in line:
            continue
        tag = line[:4].strip()
        if tag not in ("GPSA", "GPSB"):
            continue
        vals = [_f(line[5 + i * 12:17 + i * 12]) for i in range(4)]
        if tag == "GPSA" and alpha is None:
            alpha = vals
        elif tag == "GPSB" and beta is None:
            beta = vals
        if alpha and beta:
            return alpha, beta
    return None


def _check_gps_iono_layout(payload: bytes, alpha, beta) -> None:
    """Raise ValueError unless the packed coefficients read back as the values
    that went in, at the offsets the message specifies."""
    if len(payload) != 16:
        raise ValueError("payload is %d bytes, expected 16" % len(payload))
    if payload[0] != MGA_TYPE_IONO or payload[1] != 0x00:
        raise ValueError("type/version = %02X/%02X, expected 06/00"
                         % (payload[0], payload[1]))
    got = struct.unpack_from("<8b", payload, 4)
    for name, want, raw, lsb in zip(
            ("a0", "a1", "a2", "a3", "b0", "b1", "b2", "b3"),
            list(alpha) + list(beta), got, _IONO_ALPHA_LSB + _IONO_BETA_LSB):
        # The header prints 5 significant digits, so allow a hundredth of an LSB
        # of formatting slack -- but no more: anything larger means the scale
        # factor is wrong, since these are 8-bit broadcast values.
        if abs(want / lsb - raw) > 0.01:
            raise ValueError("%s = %g is %.3f LSB, not the integer %d -- wrong "
                             "scale factor?" % (name, want, want / lsb, raw))


def make_mga_gps_iono(alpha, beta) -> bytes:
    """UBX-MGA-GPS-IONO (16-byte payload): the Klobuchar ionosphere model."""
    coeffs = [_clamp(v / lsb, -128, 127) for v, lsb in
              zip(list(alpha) + list(beta), _IONO_ALPHA_LSB + _IONO_BETA_LSB)]
    payload = struct.pack(
        "<BBH8bI",
        MGA_TYPE_IONO,   # U1  type = IONO
        0x00,            # U1  version
        0,               # U2  reserved1
        *coeffs,         # I1  alpha0..3, beta0..3
        0,               # U4  reserved2
    )
    _check_gps_iono_layout(payload, alpha, beta)
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
# Each file is one station's broadcast nav for that hour (toe within ~1 h);
# several are merged, chosen for geographic spread (see HOURLY_REGION_ORDER).
_BKG_NRT = _BKG + "/IGS/nrt"
_HOURLY_RE = r'([A-Z0-9]{9})_R_\d+_01H_MN\.rnx\.gz'

# An hourly file only holds what that station tracked during that hour, so
# geographic spread — not proximity — is what covers the constellation:
# broadcast ephemeris is identical wherever it is received, and satellites out
# of view from Iberia are only fresh in a station that can see them. Stations
# are therefore taken one region at a time, in this order, so the first few
# downloads already look at different parts of the sky. Whatever is not listed
# here comes after, alphabetically.
HOURLY_REGION_ORDER = [
    "PRT", "ESP", "AUS", "PER", "KOR", "ZAF", "PYF", "GRL", "ARG", "PHL",
    "IND", "CAN", "KEN", "REU", "GUF", "NCL", "SYC", "UZB", "DJI", "BES",
    "MTQ", "SPM", "ISL", "FIN", "SWE", "GRC", "TUR", "CYP", "UKR",
]
# Used only when the directory index cannot be read.
HOURLY_STATIONS_FALLBACK = [
    "RAEG00PRT", "ALAC00ESP", "NNOR00AUS", "AREG00PER", "GAMG00KOR",
    "HARB00ZAF", "FAA100PYF", "THU200GRL", "MGUE00ARG", "PTGG00PHL",
    "GDKG00IND", "YEL200CAN", "MAL200KEN", "REUN00REU", "KOUR00GUF",
    "CEBR00ESP", "CACE00ESP", "NKLG00GAB", "REYK00ISL", "BRUX00BEL",
]


def _list_hourly_stations(doy: int, hh: int):
    """Stations with an hourly mixed-nav file in the BKG NRT tree for that hour.

    Reading the index first turns a list of guesses into the list that actually
    exists, so no round-trip is spent on a 404. Returns the station list, [] if
    the hour is not published yet (so the caller moves straight to the previous
    hour instead of probing a dozen missing files), or None if the index itself
    could not be read and the built-in list should be used.
    """
    url = f"{_BKG_NRT}/{doy:03d}/{hh:02d}/"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            html = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return [] if exc.code == 404 else None
    except urllib.error.URLError:
        return None
    return sorted(set(re.findall(_HOURLY_RE, html)))


def _order_stations_for_spread(stations: list) -> list:
    """Round-robin the stations across regions, preferred regions first."""
    by_region = {}
    for st in stations:
        by_region.setdefault(st[-3:], []).append(st)
    ranked = sorted(by_region, key=lambda c: (
        HOURLY_REGION_ORDER.index(c) if c in HOURLY_REGION_ORDER else 99, c))
    out = []
    while any(by_region.values()):
        for code in ranked:
            if by_region[code]:
                out.append(by_region[code].pop(0))
    return out


def _record_age_h(rec: dict, now_sow: float) -> float:
    """Absolute age of a nav record in hours, the short way round the week."""
    d = (now_sow - rec["toc_sow"]) % 604800
    if d > 302400:
        d -= 604800
    return abs(d) / 3600.0


def download_rinex_hourly(now: datetime, max_age_h: float = 4.0,
                          min_prns: int = 30, max_back: int = 4,
                          max_stations: int = 12) -> tuple:
    """Download hourly broadcast nav until min_prns GPS PRNs have an ephemeris
    younger than max_age_h. Returns (records, reference_datetime, iono) or
    (None, None, None); iono is the (alpha, beta) Klobuchar pair from the header.

    The stop criterion counts *fresh* PRNs, not PRNs present: every station also
    holds the last ephemeris it heard from satellites long out of view, so
    counting records made three Iberian stations look like full coverage while
    a third of the constellation was ten hours stale and dropped later.
    """
    _, now_sow = _week_sow(now)
    for back in range(max_back):
        t = now - timedelta(hours=back)
        year, doy, hh = t.year, _doy(t), t.hour
        stations = _list_hourly_stations(doy, hh)
        if stations == []:
            print(f"  hourly {doy:03d}/{hh:02d}h not published yet — trying the "
                  "previous hour", file=sys.stderr)
            continue
        if stations is None:
            print(f"  hourly {doy:03d}/{hh:02d}h: no directory index, using the "
                  "built-in station list", file=sys.stderr)
            stations = HOURLY_STATIONS_FALLBACK
        order = _order_stations_for_spread(stations)[:max_stations]
        records, fresh_prns, iono = [], set(), None
        for st in order:
            url = (f"{_BKG_NRT}/{doy:03d}/{hh:02d}/"
                   f"{st}_R_{year:04d}{doy:03d}{hh:02d}00_01H_MN.rnx.gz")
            try:
                with urllib.request.urlopen(url, timeout=20) as r:
                    raw = r.read()
            except urllib.error.URLError:
                continue
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            text = raw.decode("ascii", errors="replace")
            recs = parse_gps_nav(text)
            records += recs
            iono = iono or parse_iono_klobuchar(text)
            fresh_prns |= {r["prn"] for r in recs
                           if _record_age_h(r, now_sow) <= max_age_h}
            print(f"  hourly {st} {doy:03d}/{hh:02d}h OK — {len(fresh_prns)} PRNs "
                  f"fresh (< {max_age_h} h)", file=sys.stderr)
            if len(fresh_prns) >= min_prns:
                break
        if records:
            print(f"Hourly ephemeris @ {doy:03d}/{hh:02d}h UTC: {len(records)} "
                  f"records, {len(fresh_prns)} PRNs fresher than {max_age_h} h",
                  file=sys.stderr)
            return records, t, iono
    return None, None, None


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
    """Convert a RINEX 3 GPS nav epoch to GPS seconds-of-week.

    RINEX 3 timestamps GPS navigation records in GPS time, not UTC, so no leap
    second is added here. Adding one made toc land 18 s past toe, which the 16 s
    LSB then rounded into a permanent one-LSB skew between the two.
    """
    if year < 100:
        year += 2000
    dt = datetime(year, month, day, hour, minute, int(second),
                  tzinfo=timezone.utc)
    return ((dt - GPS_EPOCH).total_seconds()) % 604800


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
        age_h = _record_age_h(r, now_sow)
        if age_h <= max_age_h:
            prn = r["prn"]
            if prn not in best or age_h < best[prn][0]:
                best[prn] = (age_h, r)

    return [v for _, v in sorted(best.values(), key=lambda x: x[1]["prn"])]


def _week_sow(dt: datetime):
    gps_sec = (dt - GPS_EPOCH).total_seconds() + GPS_LEAP_SECONDS
    return int(gps_sec // 604800), gps_sec % 604800


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


# ── AssistNow via Thingstream ZTP (live + predicted orbits, UBX-MGA) ─────
# Modern u-blox AssistNow is authenticated with a Thingstream Zero-Touch-
# Provisioning (ZTP) device-profile token plus the receiver's identity (its
# UBX-SEC-UNIQID and UBX-MON-VER responses). Two-step flow:
#   1. POST {token, messages:{UBX-SEC-UNIQID, UBX-MON-VER}} to the ZTP endpoint
#      -> {chipcode, serviceUrl, allowedData}
#   2. GET  serviceUrl?chipcode=..&data=..&gnss=..  -> a ready UBX-MGA stream
# The stream (live MGA-* ephemeris and/or predicted MGA-ANO, plus almanac,
# ionosphere and time) merges into the same blob as the live RINEX ephemeris
# with no firmware change -- the device's inject loop is frame-type-agnostic.
#
# NOTE: the legacy token-based GetOfflineData.ashx is deliberately NOT used. A
# ZTP UUID token is a different credential and that service rejects it (HTTP 400
# "Invalid token"), which is why the previous path silently produced live-only
# output.

ZTP_CREDENTIALS_URL = "https://api.thingstream.io/ztp/assistnow/credentials"

# Identity of the target u-blox module (MAX-M10S, SPG 5.10). These are NOT
# secret -- they only authorize the ZTP request. Override with --uniqid /
# --monver (or the UBX_SEC_UNIQID / UBX_MON_VER env vars) for another module.
# The AssistNow data returned is generic GNSS aiding usable by any receiver, so
# a headless CI job with no module attached still fetches a publishable blob.
DEFAULT_UNIQID = "b56227030a0002000000ffcef10f33548a80"
DEFAULT_MONVER = (
    "b5620a04be00524f4d2053504720352e3130202837623230326529000000000000000000"
    "3030304130303030000046575645523d53504720352e3130000000000000000000000000"
    "0000000050524f545645523d33342e313000000000000000000000000000000000004d4f"
    "443d4d41582d4d3130530000000000000000000000000000000000004750533b474c4f3b"
    "47414c3b424453000000000000000000000000000000534241533b515a53530000000000"
    "0000000000000000000000000000000046ba"
)

# Components of the service 'data=' string:
#   uporb_N   N days of predicted orbits (MGA-ANO, the AssistNow Offline part).
#             A single day expires at the next UTC midnight, which is how a file
#             published at 09:00 ended up carrying yesterday's offline data.
#   ulorb_l1  live L1 ephemeris for every constellation (AssistNow Online).
#             A separate entitlement from Offline on a Thingstream profile.
#   ukion     Klobuchar ionosphere coefficients. Worth asking for: a single-band
#             receiver needs them to correct several metres of delay, and off the
#             air GPS only repeats them once per 12.5 min page cycle.
#   usvht     satellite health.
#   ualm      almanac.
ASSISTNOW_PRESETS = ("predictive", "live", "both")
_AN_EXTRAS = "ukion,usvht,ualm"
# The one combination this profile is known to be served; the floor of every
# chain, so a fetch never comes back empty because the ask was too ambitious.
_AN_FLOOR = "uporb_1,ualm"


def assistnow_data_attempts(preset: str, days: int) -> list:
    """The 'data=' strings to try, most complete first.

    Each step drops exactly one thing, so a rejection says *what* the profile is
    not entitled to rather than merely being smaller. Live orbits go first
    (Online is a separate entitlement from Offline), then the extra days, and
    only then the ionosphere and health extras -- which the previous chain could
    never tell apart, because every variant carrying them also carried live
    orbits, so three rejections in a row proved nothing about the ionosphere.
    """
    d = max(1, min(14, int(days)))
    porb = f"uporb_{d}"
    out = []
    if preset == "both":
        out.append(f"{porb},ulorb_l1,{_AN_EXTRAS}")
    elif preset == "live":
        out.append(f"ulorb_l1,{_AN_EXTRAS}")
    if preset in ("predictive", "both"):
        out.append(f"{porb},{_AN_EXTRAS}")
        if d > 1:
            out.append(f"uporb_1,{_AN_EXTRAS}")
    out.append(_AN_FLOOR)
    seen, uniq = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def assistnow_data_str(preset: str, days: int = 3) -> str:
    """The preferred (most complete) request for a preset -- also the key the
    cache is matched on, so changing what is asked for forces one refresh."""
    return assistnow_data_attempts(preset, days)[0]


# Default cache lifetime per mode: predicted orbits stay valid for days, live
# orbits expire within a few hours, so live/both refresh far more often.
ASSISTNOW_DEFAULT_MAX_AGE_H = {"predictive": 12.0, "live": 1.0, "both": 1.0}


def fetch_ztp_credentials(token, uniqid_hex, monver_hex):
    """POST the ZTP token + receiver identity; return (chipcode, service_url,
    allowed_data). Raises on any transport/format error."""
    body = json.dumps({
        "token": token,
        "messages": {"UBX-SEC-UNIQID": uniqid_hex, "UBX-MON-VER": monver_hex},
    }).encode()
    req = urllib.request.Request(
        ZTP_CREDENTIALS_URL, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        creds = json.loads(r.read().decode())
    if "chipcode" not in creds or "serviceUrl" not in creds:
        raise RuntimeError(f"ZTP response missing chipcode/serviceUrl: {creds}")
    return creds["chipcode"], creds["serviceUrl"], creds.get("allowedData", "")


# HTTP statuses that mean "this device profile may not ask for that", and so
# justify retrying with a smaller request. Anything else -- 429, 5xx, a network
# fault -- means come back later: walking the rest of the chain would spend
# three more requests of a quota that is already refusing us.
_ASSISTNOW_DOWNGRADE_CODES = (400, 401, 403, 404, 422)


def filter_data_strs(data_strs, allowed):
    """Drop request components the device profile is not entitled to.

    The credentials call answers with allowedData, a comma-separated list of the
    components this profile may ask for, e.g.
    'ualm, uporb_1, uporb_3, uporb_7, uporb_14'. Filtering the ladder against it
    is what makes 'uporb_3,ualm' get asked for at all: the service rejects a
    request carrying any Live component -- the ionosphere and satellite health
    count as Live, not just the orbits -- and the hand-written ladder only ever
    paired the extra days with the ionosphere. Three days of predicted orbits
    therefore looked unavailable while allowedData plainly offered fourteen.

    Falls back to the unfiltered ladder if allowedData is empty or filters
    everything away, so an unexpected format cannot make the fetch impossible.
    """
    tokens = {t.strip() for t in (allowed or "").split(",") if t.strip()}
    if not tokens:
        return data_strs
    out = []
    for spec in data_strs:
        kept = ",".join(c for c in spec.split(",") if c.strip() in tokens)
        if kept and kept not in out:
            out.append(kept)
    return out or data_strs


def fetch_assistnow_mga(token, uniqid_hex, monver_hex, data_strs, gnss):
    """Full ZTP AssistNow fetch (credentials -> data). Returns (raw UBX-MGA
    bytes, the 'data=' string that worked, the profile's allowedData) or raises
    RuntimeError. Consumes
    service quota -- call only via get_assistnow_blob() so the cache gates it.
    The credentials are fetched once and reused across the data-string
    fallbacks, so a downgraded request costs no extra credentials call."""
    if isinstance(data_strs, str):
        data_strs = [data_strs]
    try:
        chipcode, service_url, allowed = fetch_ztp_credentials(
            token, uniqid_hex, monver_hex)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"ZTP credentials failed ({exc.code}): "
                           f"{exc.read().decode()[:200]}")
    except (urllib.error.URLError, ValueError, RuntimeError) as exc:
        raise RuntimeError(f"ZTP credentials failed: {exc}")
    print(f"AssistNow ZTP: chipcode obtained (allowedData: {allowed})", file=sys.stderr)

    entitled = filter_data_strs(data_strs, allowed)
    if entitled != data_strs:
        print(f"AssistNow: asking only for what the profile allows: "
              f"{entitled}", file=sys.stderr)
        data_strs = entitled

    last_err = "no data string tried"
    for data_str in data_strs:
        query = urllib.parse.urlencode(
            {"chipcode": chipcode, "data": data_str, "gnss": gnss})
        print(f"AssistNow: GET {service_url} (data={data_str} gnss={gnss})",
              file=sys.stderr)
        try:
            with urllib.request.urlopen(f"{service_url}?{query}", timeout=60) as r:
                data = r.read()
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code}: {exc.read().decode()[:200]}"
            if exc.code not in _ASSISTNOW_DOWNGRADE_CODES:
                raise RuntimeError(f"AssistNow data failed, not retrying "
                                   f"({last_err})")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"AssistNow data failed: {exc}")
        else:
            if len(data) < 8 or data[0] != 0xB5 or data[1] != 0x62:
                last_err = (f"response is not UBX ({len(data)} bytes): "
                            f"{data[:80]!r}")
            else:
                print(f"  OK -- {len(data)} bytes of UBX-MGA", file=sys.stderr)
                return data, data_str, allowed
        print(f"  rejected ({last_err}) -- trying a smaller request",
              file=sys.stderr)
    raise RuntimeError(f"AssistNow data failed: {last_err}")


def get_assistnow_blob(token, uniqid_hex, monver_hex, preset, days, gnss,
                       cache_path, max_age_h, now=None, retry_h=3.0):
    """Return (blob, age_h) for the AssistNow MGA data, reusing a cached copy
    while it is still worth serving. Persist cache_path (+ '.json') between runs
    (a committed file or actions/cache). Returns (b'', 0.0) if unavailable
    (never fatal).

    Three gates keep the service quota intact, and each exists because of a way
    the previous one failed:
      * max_age_h -- do not fetch what is still good;
      * the predicted-orbit day -- an age limit cannot see midnight go by, so a
        12 h window happily served yesterday's offline data all morning;
      * retry_h after a refusal -- without it a rejection was retried on every
        hourly run, and since each attempt walks a ladder of up to four
        requests, ten attempts a day exhausted what the service was willing to
        serve before mid-morning.

    age_h is returned so the caller can drop live ephemeris that has expired.
    """
    now = now or datetime.now(timezone.utc)
    meta_path = cache_path + ".json"
    data_str = assistnow_data_str(preset, days)
    attempts = assistnow_data_attempts(preset, days)
    attempts_key = list(attempts)   # canonical order; `attempts` gets reordered
    meta, known_good, last_fetch_day = {}, None, None

    def age_h(iso):
        try:
            return (now - datetime.fromisoformat(iso)).total_seconds() / 3600.0
        except (TypeError, ValueError):
            return None

    def read_cache():
        try:
            with open(cache_path, "rb") as f:
                return f.read()
        except OSError:
            return b""

    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except (FileNotFoundError, ValueError) as exc:
        print(f"AssistNow: no usable cache metadata ({exc})", file=sys.stderr)

    cached_age = age_h(meta.get("fetched_utc"))
    params_match = (meta.get("data") == data_str and meta.get("gnss") == gnss)
    # Lead with the remembered request only if the chain that produced it is the
    # chain in use now. Comparing just the preferred request is not enough:
    # widening the middle of the ladder leaves its first entry untouched, so the
    # new variants would never be tried until the next UTC day rolled over.
    if params_match and cached_age is not None and meta.get("attempts") == attempts_key:
        known_good = meta.get("data_used")
        last_fetch_day = (now - timedelta(hours=cached_age)).date()

    # 1. A cache still worth serving costs no request at all.
    if params_match and cached_age is not None and cached_age < max_age_h:
        data = read_cache()
        ano_day = ano_latest_day(data) if data else None
        if not data:
            print("AssistNow: metadata without a cache file -- fetching",
                  file=sys.stderr)
        elif "uporb" in meta.get("data_used", data_str) and (
                ano_day is None or ano_day < now.date()):
            print(f"AssistNow: cache predicted orbits expired (latest MGA-ANO "
                  f"day {ano_day}) -- refreshing", file=sys.stderr)
        else:
            print(f"AssistNow: cache hit ({cached_age:.1f} h old, {len(data)} "
                  f"bytes) -- not calling the service", file=sys.stderr)
            return data, cached_age

    # 2. Back off after a refusal, as long as there is something to serve.
    stale = read_cache()
    failed_age = age_h(meta.get("last_attempt_utc"))
    if stale and failed_age is not None and failed_age < retry_h:
        age = cached_age if cached_age is not None else 1e6
        print(f"AssistNow: last attempt failed {failed_age:.1f} h ago "
              f"(< {retry_h} h) -- serving the cached blob rather than spending "
              f"another request", file=sys.stderr)
        return stale, age

    # 3. Spend the quota. Lead with whatever the service accepted last time,
    #    except on the first fetch of a new UTC day -- then the full request is
    #    worth one more try, so an entitlement that has since been upgraded is
    #    picked up without anyone editing this file.
    print(f"AssistNow: refreshing (cache "
          f"{'%.1f h old' % cached_age if cached_age is not None else 'absent'})",
          file=sys.stderr)
    if known_good in attempts and last_fetch_day == now.date():
        attempts = [known_good] + [a for a in attempts if a != known_good]
        print(f"AssistNow: leading with the last accepted request "
              f"({known_good})", file=sys.stderr)

    try:
        data, data_used, allowed = fetch_assistnow_mga(
            token, uniqid_hex, monver_hex, attempts, gnss)
    except RuntimeError as exc:
        # Record the failed attempt so the next run backs off instead of walking
        # the ladder again, then fall back to a stale cache if there is one --
        # old aiding beats none, and the hourly publish must never break on an
        # AssistNow hiccup. The age goes back to the caller so expired parts of
        # it are dropped.
        meta["last_attempt_utc"] = now.isoformat()
        try:
            with open(meta_path, "w") as f:
                json.dump(meta, f)
        except OSError as werr:
            print(f"AssistNow: could not record the failed attempt ({werr})",
                  file=sys.stderr)
        if stale:
            age = cached_age if cached_age is not None else 1e6
            print(f"AssistNow: fetch failed, reusing stale cache "
                  f"({len(stale)} bytes, {age:.1f} h old): {exc}", file=sys.stderr)
            return stale, age
        print(f"AssistNow: fetch failed and no cache -- live-only this run: "
              f"{exc}", file=sys.stderr)
        return b"", 0.0

    with open(cache_path, "wb") as f:
        f.write(data)
    with open(meta_path, "w") as f:
        json.dump({
            "fetched_utc": now.isoformat(),
            "data": data_str,
            "data_used": data_used,
            "attempts": attempts_key,
            "allowed_data": allowed,
            "gnss": gnss,
            "bytes": len(data),
        }, f)
    return data, 0.0


# ── UBX-MGA stream inspection / sanitising ──────────────────────────────────
# The published blob is a concatenation of two sources with different lifetimes
# (live RINEX ephemeris built now, AssistNow aiding served from a cache), so
# everything that leaves this script is walked frame by frame and anything
# stale, expired or physically impossible is dropped. That is what keeps the
# file self-consistent: a receiver applies whatever it finds, silently.

MGA_ID_NAMES = {0x00: "GPS", 0x02: "GAL", 0x03: "BDS", 0x05: "QZSS",
                0x06: "GLO", 0x20: "ANO", 0x21: "FLASH", 0x40: "INI"}
MGA_TYPE_EPH = 0x01
MGA_TYPE_ALM = 0x02
# Message types inside a per-constellation MGA message, so an inventory says
# what aiding actually arrived instead of a bare number.
MGA_TYPE_NAMES = {0x01: "EPH", 0x02: "ALM", 0x03: "TIMEOFF", 0x04: "HEALTH",
                  0x05: "UTC", 0x06: "IONO"}
MGA_ANO      = 0x20
# Constellation message ids that carry per-SV orbit data (type 1 = ephemeris).
MGA_GNSS_IDS = (0x00, 0x02, 0x03, 0x05, 0x06)

# Empirically verified against real AssistNow output (u-blox M10 SPG 5.10) and
# used only for sanity checks, never to rewrite payloads:
#   msg_id -> (sqrtA offset, LSB exponent, plausible sqrtA windows in m^1/2)
_ALM_SQRTA = {
    0x00: (12, -11, [(5100.0, 5200.0)]),                    # GPS  MEO
    0x05: (12, -11, [(6400.0, 6600.0), (5100.0, 5200.0)]),  # QZSS IGSO/GEO
    0x03: (8,  -11, [(5200.0, 5350.0), (6400.0, 6600.0)]),  # BDS  MEO / IGSO+GEO
}
# msg_id -> offset of the U1 week-number-of-almanac (mod 256). Only the two
# systems whose value was cross-checked against the real current week.
_ALM_WNA = {0x00: 6, 0x03: 4}
# GPS week number of the BeiDou epoch (2006-01-01): BDS week = GPS week - 1356.
BDS_WEEK_OFFSET = 1356


def iter_ubx_frames(blob: bytes):
    """Walk a UBX stream, yielding (offset, cls, msg_id, payload, ok).

    ok is False for a frame with a bad checksum; a structurally broken frame
    (lost sync or truncated) is reported with cls=None and ends the walk, so a
    partially written cache can never leak trailing garbage into the output.
    """
    i, n = 0, len(blob)
    while i + 8 <= n:
        if blob[i] != 0xB5 or blob[i + 1] != 0x62:
            yield (i, None, None, b"", False)
            return
        ln = struct.unpack_from("<H", blob, i + 4)[0]
        end = i + 8 + ln
        if end > n:
            yield (i, None, None, b"", False)
            return
        payload = blob[i + 6:i + 6 + ln]
        ck_a, ck_b = _ubx_checksum(blob[i + 2:i + 6 + ln])
        yield (i, blob[i + 2], blob[i + 3], payload,
               ck_a == blob[end - 2] and ck_b == blob[end - 1])
        i = end
    if i != n:
        yield (i, None, None, b"", False)


def _frame_bytes(blob: bytes, offset: int, payload_len: int) -> bytes:
    return blob[offset:offset + 8 + payload_len]


def _ano_date(payload: bytes):
    """(year, month, day) of a UBX-MGA-ANO record, or None if unparsable."""
    if len(payload) < 7:
        return None
    return (2000 + payload[4], payload[5], payload[6])


def ano_latest_day(blob: bytes):
    """Most recent MGA-ANO date in a blob as a date, or None if it has none.
    Used to decide whether cached predicted orbits are still worth serving."""
    latest = None
    for _, cls, msg_id, payload, ok in iter_ubx_frames(blob):
        if not ok or cls != UBX_CLASS_MGA or msg_id != MGA_ANO:
            continue
        d = _ano_date(payload)
        if d is None:
            continue
        try:
            day = datetime(d[0], d[1], d[2], tzinfo=timezone.utc).date()
        except ValueError:
            continue
        if latest is None or day > latest:
            latest = day
    return latest


def _alm_sqrta_ok(msg_id: int, payload: bytes):
    """(ok, value) for the almanac semi-major-axis sanity check."""
    spec = _ALM_SQRTA.get(msg_id)
    if spec is None:
        return True, None
    off, exp, windows = spec
    if len(payload) < off + 4:
        return True, None
    val = struct.unpack_from("<I", payload, off)[0] * 2.0 ** exp
    return any(lo <= val <= hi for lo, hi in windows), val


def _alm_stale_weeks(msg_id: int, payload: bytes, now: datetime):
    """How many weeks old the almanac's WNa is, or None if not checkable."""
    off = _ALM_WNA.get(msg_id)
    if off is None or len(payload) <= off:
        return None
    gps_week = _week_sow(now)[0]
    cur = (gps_week - BDS_WEEK_OFFSET if msg_id == 0x03 else gps_week) % 256
    return (cur - payload[off]) % 256


def sanitize_mga_stream(blob: bytes, now: datetime, blob_age_h: float = 0.0,
                        live_max_age_h: float = 4.0,
                        alm_max_stale_weeks: int = 26,
                        keep_ini: bool = False, label: str = "AssistNow"):
    """Filter a UBX-MGA stream into something safe to publish.

    Dropped: broken frames, UBX-MGA-INI-* (unless keep_ini -- a cached INI-TIME
    is what makes a served file claim the wrong hour), MGA-ANO for days already
    past, live ephemeris older than live_max_age_h (the blob's own age, since
    live orbits expire within hours), and almanac records with an impossible
    orbit or a week-of-almanac older than alm_max_stale_weeks.

    Returns (clean_bytes, stats) -- stats maps a reason to a dropped count.
    """
    today = now.date()
    live_stale = blob_age_h > live_max_age_h
    kept, dropped = [], {}

    def drop(reason):
        dropped[reason] = dropped.get(reason, 0) + 1

    for off, cls, msg_id, payload, ok in iter_ubx_frames(blob):
        if cls is None:
            drop("broken frame (walk stopped)")
            break
        if not ok:
            drop("bad checksum")
            continue
        frame = _frame_bytes(blob, off, len(payload))
        mtype = payload[0] if payload else None

        if cls == UBX_CLASS_MGA and msg_id == MGA_INI and not keep_ini:
            drop("MGA-INI (stale time/position)")
            continue

        if cls == UBX_CLASS_MGA and msg_id == MGA_ANO:
            d = _ano_date(payload)
            try:
                day = datetime(d[0], d[1], d[2], tzinfo=timezone.utc).date()
            except (TypeError, ValueError):
                drop("MGA-ANO unparsable date")
                continue
            if day < today:
                drop("MGA-ANO expired (%s)" % day)
                continue

        if cls == UBX_CLASS_MGA and msg_id in MGA_GNSS_IDS:
            if mtype == MGA_TYPE_EPH and live_stale:
                drop("live ephemeris %.1f h old (> %.1f h)"
                     % (blob_age_h, live_max_age_h))
                continue
            if mtype == MGA_TYPE_ALM:
                sane, val = _alm_sqrta_ok(msg_id, payload)
                if not sane:
                    print("  %s: dropping %s-ALM sv=%d -- impossible orbit "
                          "(sqrtA=%.1f m^1/2)"
                          % (label, MGA_ID_NAMES.get(msg_id, "?"),
                             payload[2] if len(payload) > 2 else -1, val),
                          file=sys.stderr)
                    drop("almanac impossible orbit")
                    continue
                stale = _alm_stale_weeks(msg_id, payload, now)
                if stale is not None and stale > alm_max_stale_weeks:
                    print("  %s: dropping %s-ALM sv=%d -- WNa %d weeks stale"
                          % (label, MGA_ID_NAMES.get(msg_id, "?"),
                             payload[2] if len(payload) > 2 else -1, stale),
                          file=sys.stderr)
                    drop("almanac stale WNa")
                    continue

        kept.append(frame)

    return b"".join(kept), dropped


def describe_ubx(blob: bytes, now: datetime = None, served: bool = True):
    """Return (lines, problems) describing a UBX-MGA blob.

    Structural verification of every frame plus a physical plausibility check on
    the GPS ephemeris -- a wrong field offset shows up instantly as a nonsense
    semi-major axis, which is exactly how the missing type/version header used
    to hide behind a valid checksum.
    """
    now = now or datetime.now(timezone.utc)
    # served=True means the blob is published at a URL and read hours later, so
    # a baked-in MGA-INI time is a defect; for a blob injected right now it is
    # exactly what is wanted.
    counts, problems, lines = {}, [], []
    ano_days, prns, total, bad_cks = {}, [], 0, 0

    for off, cls, msg_id, payload, ok in iter_ubx_frames(blob):
        if cls is None:
            problems.append("stream desync / truncated frame at byte %d" % off)
            break
        total += 1
        if not ok:
            bad_cks += 1
            problems.append("bad checksum at byte %d (cls=%02X id=%02X)"
                            % (off, cls, msg_id))
            continue
        mtype = payload[0] if payload else None
        key = (cls, msg_id, mtype, len(payload))
        counts[key] = counts.get(key, 0) + 1

        if cls == UBX_CLASS_MGA and msg_id == MGA_ANO:
            d = _ano_date(payload)
            if d:
                ano_days["%04d-%02d-%02d" % d] = ano_days.get("%04d-%02d-%02d" % d, 0) + 1
        if cls == UBX_CLASS_MGA and msg_id == MGA_GPS and len(payload) == 68:
            # 68 bytes is the ephemeris payload size, so whatever the first byte
            # says, this frame has to be a well-formed EPH.
            if mtype == MGA_TYPE_EPH:
                prns.append(payload[2])
            try:
                _check_gps_eph_layout(payload)
            except ValueError as exc:
                problems.append("MGA-GPS-EPH at byte %d: %s" % (off, exc))

    lines.append("UBX frames: %d, %d bytes, %d bad checksum(s)"
                 % (total, len(blob), bad_cks))
    for (cls, msg_id, mtype, ln), n in sorted(counts.items()):
        name = MGA_ID_NAMES.get(msg_id, "%02X" % msg_id) if cls == UBX_CLASS_MGA \
            else "cls%02X" % cls
        tname = MGA_TYPE_NAMES.get(mtype, "type%s" % mtype)
        if msg_id in (MGA_ANO, MGA_INI):
            tname = "type%s" % mtype
        lines.append("  MGA-%-5s %-7s len=%-3d x%d" % (name, tname, ln, n))
    if prns:
        lines.append("  GPS EPH PRNs (%d): %s" % (len(prns), sorted(prns)))
        missing = sorted(set(range(1, 33)) - set(prns))
        if missing:
            lines.append("  GPS EPH missing PRNs: %s" % missing)
    if ano_days:
        lines.append("  MGA-ANO days: %s"
                     % ", ".join("%s x%d" % kv for kv in sorted(ano_days.items())))
        today = now.strftime("%Y-%m-%d")
        expired = [d for d in ano_days if d < today]
        if expired:
            problems.append("MGA-ANO already expired: %s" % ", ".join(sorted(expired)))
    for (cls, msg_id, mtype, _ln), n in sorted(counts.items()):
        if served and cls == UBX_CLASS_MGA and msg_id == MGA_INI:
            problems.append("%d MGA-INI frame(s) present: a served file must not "
                            "carry a fixed time/position" % n)
    return lines, problems


def report_blob(label: str, blob: bytes, now: datetime, served: bool = True) -> list:
    """Print an inventory of a blob and return its list of problems."""
    lines, problems = describe_ubx(blob, now, served=served)
    print(f"\n{label}:", file=sys.stderr)
    for line in lines:
        print(f"  {line}", file=sys.stderr)
    for prob in problems:
        print(f"  PROBLEM: {prob}", file=sys.stderr)
    return problems


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
                         "hourly, topped up from the daily file if coverage is thin")
    ap.add_argument("--no-iono", action="store_true",
                    help="Do not emit the UBX-MGA-GPS-IONO frame built from the "
                         "RINEX header's Klobuchar coefficients. They are worth "
                         "having (the AssistNow profile will not serve them and "
                         "the air takes up to 12.5 min), but the byte layout of "
                         "that message has not been cross-checked against real "
                         "service output the way the ephemeris has -- so this "
                         "turns it off if the receiver NAKs it.")
    ap.add_argument("--min-prns", type=int, default=30,
                    help="Keep pulling hourly stations (then the daily file) until "
                         "this many GPS PRNs are covered (default: 30)")
    ap.add_argument("--stats", action="store_true",
                    help="Print the content summary on stdout as well as stderr")
    ap.add_argument("--verify", metavar="FILE", default=None,
                    help="Inspect an existing .ubx file and exit: frame inventory, "
                         "checksums, MGA-GPS-EPH field alignment, MGA-ANO expiry. "
                         "Exits non-zero if a problem is found.")
    ap.add_argument("--no-strict", action="store_true",
                    help="Write the output even when verification finds a problem "
                         "(by default a broken blob is not written, so a publishing "
                         "job keeps serving the previous good file)")
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
    # ── AssistNow via ZTP (live + predicted orbits) merge ────────────────
    ap.add_argument("--assistnow-token", default=os.environ.get("ASSISTNOW_TOKEN"),
                    help="Thingstream ZTP device-profile token (a UUID), or the "
                         "ASSISTNOW_TOKEN env var. When set, an AssistNow UBX-MGA stream "
                         "(live ephemeris and/or predicted orbits) is merged into the "
                         "output. Keep it SECRET: pass via env / CI secret, never commit it.")
    ap.add_argument("--assistnow-data", choices=list(ASSISTNOW_PRESETS),
                    default="both",
                    help="Which aiding to fetch: 'predictive' (multi-day MGA-ANO orbits), "
                         "'live' (current ephemeris for every constellation, best TTFF, "
                         "expires in hours), or 'both' (default; one request).")
    ap.add_argument("--assistnow-days", type=int, default=3,
                    help="Days of predicted orbits (MGA-ANO) to request, 1-14 "
                         "(default: 3). One day expires at the next UTC midnight, "
                         "which leaves the offline data useless the morning after.")
    ap.add_argument("--assistnow-gnss", default="gps,gal,glo,bds,qzss",
                    help="Constellations for AssistNow, comma-separated "
                         "(default: gps,gal,glo,bds,qzss).")
    ap.add_argument("--assistnow-output", default=None, metavar="FILE",
                    help="Write the AssistNow aiding to its own file instead of "
                         "appending it to --output. Keeps the two sources, whose "
                         "contents have very different lifetimes, from being served "
                         "as one blob that looks internally inconsistent.")
    ap.add_argument("--assistnow-live-max-age-h", type=float, default=4.0,
                    help="Drop live ephemeris from a cached AssistNow blob older "
                         "than this (default: 4 h, the GPS fit interval). Almanac "
                         "and predicted orbits are kept.")
    ap.add_argument("--alm-max-stale-weeks", type=int, default=26,
                    help="Drop almanac records whose week-of-almanac is more than "
                         "this many weeks old (default: 26)")
    ap.add_argument("--keep-assistnow-ini", action="store_true",
                    help="Keep the MGA-INI-TIME frame that AssistNow includes. Off "
                         "by default: it carries the time of the fetch, so a served "
                         "or cached file would tell the receiver the wrong hour.")
    ap.add_argument("--uniqid", default=os.environ.get("UBX_SEC_UNIQID", DEFAULT_UNIQID),
                    help="Target module UBX-SEC-UNIQID response as hex (full UBX frame). "
                         "Defaults to the built-in MAX-M10S; authorizes the ZTP request "
                         "only (not secret).")
    ap.add_argument("--monver", default=os.environ.get("UBX_MON_VER", DEFAULT_MONVER),
                    help="Target module UBX-MON-VER response as hex (full UBX frame). "
                         "Defaults to the built-in MAX-M10S.")
    ap.add_argument("--assistnow-cache", default="agps_assistnow_cache.ubx",
                    help="Where to persist the AssistNow blob between runs (default: "
                         "agps_assistnow_cache.ubx). Persist this file AND its '.json' "
                         "sidecar across CI runs so the service quota is respected.")
    ap.add_argument("--assistnow-retry-h", type=float, default=3.0,
                    help="After the service refuses a request, wait this long "
                         "before spending another one (default: 3 h). Retrying "
                         "on every hourly run is what emptied the daily quota "
                         "by mid-morning.")
    ap.add_argument("--assistnow-max-age-h", type=float, default=None,
                    help="Only re-fetch when the cache is older than this. Default depends "
                         "on --assistnow-data: 12 h for 'predictive', 1 h for 'live'/'both' "
                         "(live orbits expire fast). A cache whose predicted orbits are for "
                         "a past day is refreshed regardless.")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    iono = None

    # ── Verify-only mode: inspect a file someone else produced ───────────────
    if args.verify:
        with open(args.verify, "rb") as f:
            blob = f.read()
        problems = report_blob(f"Verifying {args.verify}", blob, now)
        raise SystemExit(1 if problems else 0)

    strict = not args.no_strict
    all_records = []

    # 1. Hourly source first (freshest → best hot start), unless the user forced
    #    a daily source or a specific past --date.
    if args.source in ("auto", "hourly") and not args.date:
        hourly, _, iono = download_rinex_hourly(now, max_age_h=args.max_age_h,
                                                min_prns=args.min_prns)
        if hourly:
            all_records = hourly

    # 2. Daily source: the fallback when hourly is unavailable, and the top-up
    #    when the hourly stations that answered did not see the whole
    #    constellation (filter_fresh_now keeps the freshest record per PRN, so
    #    merging the two can only add coverage).
    _, now_sow = _week_sow(now)
    fresh_prns = {r["prn"] for r in all_records
                  if _record_age_h(r, now_sow) <= args.max_age_h}
    want_daily = (args.source == "daily"
                  or (not all_records and args.source != "hourly")
                  or (args.source == "auto" and len(fresh_prns) < args.min_prns))
    if want_daily:
        if all_records:
            print(f"Hourly data has fresh ephemeris for {len(fresh_prns)} PRNs "
                  f"(< {args.min_prns}) — topping up from the daily file",
                  file=sys.stderr)
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
            iono = iono or parse_iono_klobuchar(text)
            daily = parse_gps_nav(text)
            print(f"Parsed {len(daily)} GPS records from daily data", file=sys.stderr)
            all_records += daily

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

    # 3. Ionosphere model, if the RINEX header carried one. AssistNow will not
    #    serve it on this profile, and off the air it takes up to 12.5 min.
    if iono and not args.no_iono:
        try:
            frames.append(make_mga_gps_iono(*iono))
            print(f"Ionosphere (Klobuchar) from the RINEX header: "
                  f"alpha={iono[0]} beta={iono[1]}", file=sys.stderr)
        except ValueError as exc:
            print(f"  ionosphere rejected — {exc}", file=sys.stderr)
    elif not iono:
        print("No IONOSPHERIC CORR in the RINEX header — no ionosphere model",
              file=sys.stderr)

    # 4. GPS ephemeris frames
    conversion_errors = 0
    for rec in fresh:
        try:
            frames.append(gps_ephem_to_ubx(rec))
        except Exception as exc:
            print(f"  PRN G{rec['prn']:02d}: conversion error — {exc}", file=sys.stderr)
            conversion_errors += 1

    if conversion_errors:
        print(f"Warning: {conversion_errors} records failed conversion", file=sys.stderr)

    binary = b"".join(frames)

    # ── AssistNow aiding (ZTP) ──────────────────────────────────────────────
    # The device injects any UBX-MGA frames it is handed, so the AssistNow
    # stream (live ephemeris for every constellation and/or predicted MGA-ANO)
    # needs no firmware change. It is cache-gated for the service quota and
    # sanitised before use: the cached blob carries the time of its own fetch
    # and orbits that expire, neither of which may leak into a published file.
    aiding = b""
    if args.assistnow_token:
        max_age_h = (args.assistnow_max_age_h if args.assistnow_max_age_h is not None
                     else ASSISTNOW_DEFAULT_MAX_AGE_H[args.assistnow_data])
        raw, age_h = get_assistnow_blob(
            args.assistnow_token, args.uniqid, args.monver, args.assistnow_data,
            args.assistnow_days, args.assistnow_gnss, args.assistnow_cache,
            max_age_h, now, args.assistnow_retry_h)
        if raw:
            aiding, dropped = sanitize_mga_stream(
                raw, now, blob_age_h=age_h,
                live_max_age_h=args.assistnow_live_max_age_h,
                alm_max_stale_weeks=args.alm_max_stale_weeks,
                keep_ini=args.keep_assistnow_ini and not args.no_ini)
            for reason, count in sorted(dropped.items()):
                print(f"  AssistNow: dropped {count} frame(s) — {reason}",
                      file=sys.stderr)
            print(f"AssistNow aiding: {len(aiding)} bytes kept of {len(raw)} "
                  f"({args.assistnow_data}, {age_h:.1f} h old)", file=sys.stderr)
    else:
        print("No AssistNow token (--assistnow-token / ASSISTNOW_TOKEN) -- live-only "
              "output", file=sys.stderr)

    # One file by default: the device fetches a single URL, and this way it gets
    # the predicted orbits for four constellations and the fresh GPS ephemeris
    # in one download. Merging is only safe because both halves went through the
    # sanitiser first -- what used to make the combined blob incoherent was the
    # cached INI-TIME and the expired MGA-ANO riding along, not the merge.
    # --assistnow-output splits them again for anyone who wants that.
    #
    # Aiding first, ephemeris last: they land in different stores in the
    # receiver so the order does not matter, but if anything ever does prefer
    # the later frame, the precise broadcast ephemeris is the one to win.
    if aiding and not args.assistnow_output:
        binary = aiding + binary
        print(f"Merged {len(aiding)} bytes of AssistNow aiding into "
              f"{args.output}; blob now {len(binary)} bytes", file=sys.stderr)

    problems = report_blob(f"Blob for {args.output}", binary, now,
                           served=args.no_ini)
    if args.stats:
        for line in describe_ubx(binary, now, served=args.no_ini)[0]:
            print(line)

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

    # ── Output ────────────────────────────────────────────────────────────
    # Each file stands or falls on its own: a problem with one must not stop the
    # other from being published, and nothing is overwritten with a blob that
    # failed verification -- the previously published file is better than a
    # broken one, since a receiver applies whatever it is given.
    writes = [(args.output, binary, problems)]
    if args.assistnow_output:
        writes.append((args.assistnow_output, aiding,
                       report_blob(f"AssistNow blob ({args.assistnow_output})",
                                   aiding, now)))
    failures = 0
    for path, blob, probs in writes:
        if not blob:
            print(f"WARNING: nothing to write to {path} — left untouched so the "
                  f"last good file keeps being served", file=sys.stderr)
            continue
        if probs and strict:
            print(f"ERROR: {len(probs)} problem(s) in the blob for {path}; "
                  f"refusing to write it (--no-strict overrides)", file=sys.stderr)
            failures += 1
            continue
        with open(path, "wb") as f:
            f.write(blob)
        print(f"Wrote {len(blob)} bytes → {path}")
    if failures:
        raise SystemExit(f"ERROR: {failures} output file(s) not written")

    print("\nInject into the board (online hot start):")
    print(f"    python3 {sys.argv[0]} --port /dev/ttyACM0          # auto-stream + fix")
    print(f"    python3 {sys.argv[0]} --format shell | <paste>     # manual paste")


if __name__ == "__main__":
    main()
