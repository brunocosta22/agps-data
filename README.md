# agps-data

A-GPS aiding for a u-blox MAX-M10S, rebuilt hourly and published at a stable
URL: <https://brunocosta22.github.io/agps-data/latest.ubx>

One UBX-MGA stream, roughly 30 KB, containing:

| | source | covers |
|---|---|---|
| precise broadcast ephemeris | RINEX 3 (BKG, IGN) | GPS, Galileo, BeiDou |
| Klobuchar ionosphere model | RINEX 3 header | GPS |
| almanac | u-blox AssistNow | GPS, Galileo, BeiDou |
| predicted orbits, 3 days (MGA-ANO) | u-blox AssistNow | GPS, Galileo, BeiDou |

GLONASS and QZSS are left out: the receiver does not track GLONASS, and QZSS
never rises in Europe. The file carries **no** `UBX-MGA-INI-TIME` — the receiver
supplies its own time when it applies the data, so a file downloaded hours after
it was built is never wrong about the hour.

`docs/test/` holds one file per hypothesis, all built from the same data, for
bisecting a receiver that will not fix; each rung adds one thing to the one
above it, and `06-full.ubx` is byte-identical to `latest.ubx`.

## Running it

```sh
python3 agps_download.py --verify docs/latest.ubx   # inspect a published file
PUBLISH=0 ./refresh.sh                              # build everything, commit nothing
```

`refresh.sh` is what CI runs, so it reproduces a publish exactly. It needs
`ASSISTNOW_TOKEN` in the environment for the AssistNow half; without it the file
comes out with the RINEX ephemeris and ionosphere only.

## The hourly cadence needs an external trigger

The workflow asks for hourly and GitHub delivered it — 23 runs a day — until the
Actions incidents of 2026-08-24 and 2026-08-26. Since then the same cron
produces a run every 2 to 6 hours, averaging 4.4. Every run succeeds in under
half a minute and the workflow is `active`: scheduled events are best-effort by
documentation and these are being dropped, so there is nothing here to fix.

It matters because broadcast ephemeris is valid only for its ~4 h fit interval.
Measured on a file built at 05:29 and read at 09:57: 31 of 31 Galileo frames and
36 of 37 BeiDou frames were already out of fit, leaving the predicted orbits to
carry the file alone.

`trigger-refresh.sh` fixes it from outside. Run it hourly from any machine that
keeps time, with a token scoped to *Actions: write* on this repository and
nothing else. It uses only the Python standard library -- python3 is already
needed to build the file, and curl is not always installed:

```sh
5 * * * * GH_TOKEN_FILE=$HOME/.config/agps/gh-token $HOME/agps/trigger-refresh.sh >> $HOME/.local/state/agps-trigger.log 2>&1
```

Pointing it at a file rather than passing `GH_TOKEN=` keeps the token out of the
process listing.

The workflow keeps its cron as a fallback; a dispatch and a schedule do the same
work. To cut GitHub out of the timing altogether, run `refresh.sh` on that same
machine instead and let it push — it needs only `python3` and `git`.

## Notes

Message layouts come from the [u-blox M10 SPG 5.10 interface
description](https://content.u-blox.com/sites/default/files/u-blox-M10-SPG-5.10_InterfaceDescription_UBX-21035062.pdf)
(UBX-21035062), the firmware the module reports in `UBX-MON-VER`. Every run
decodes that identity and warns if the hardware or the constellations it tracks
no longer match what the layouts were verified against.

The device's `download_client` does not follow redirects: serve the file from the
GitHub Pages URL above or a raw permalink, never a `github.com` branch URL.
