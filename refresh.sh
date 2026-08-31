#!/usr/bin/env bash
# One A-GPS refresh: build, prune, publish, verify, commit.
#
# Lives in a script rather than in the workflow YAML because the workflow calls
# it in a loop -- see .github/workflows/agps.yml for why -- and because a shell
# script can be run by hand against a checkout to reproduce exactly what CI does.
#
# Set PUBLISH=0 to build without committing or pushing.
set -euo pipefail

TS=${TS:-$(date -u +%Y%m%d_%H%M)}
PUBLISH=${PUBLISH:-1}
ARCHIVE_DAYS=${ARCHIVE_DAYS:-2}

mkdir -p docs/archive docs/test

# ── Build ───────────────────────────────────────────────────────────────────
python3 agps_download.py \
    --no-ini \
    --min-prns 32 \
    --output "docs/archive/agps_${TS}.ubx" \
    --assistnow-data both \
    --assistnow-days 3 \
    --publish-gnss gps,gal,bds,sbas \
    --gnss-eph gps,gal,bds \
    --variants docs/test \
    --assistnow-cache docs/agps_assistnow_cache.ubx \
    --assistnow-max-age-h 12

# ── Prune the archive ───────────────────────────────────────────────────────
# By the date in the filename, not by mtime: every file in a fresh checkout
# carries the time of the checkout, so `find -mtime +N` matched nothing at all
# and the archive grew unchecked for two months.
cutoff=$(date -u -d "${ARCHIVE_DAYS} days ago" +%Y%m%d)
removed=0
for f in docs/archive/*.ubx; do
    [ -e "$f" ] || continue
    day=$(basename "$f" | grep -oE '[0-9]{8}' | head -1)
    if [ -n "$day" ] && [ "$day" -lt "$cutoff" ]; then
        rm -f "$f"
        removed=$((removed + 1))
    fi
done
echo "pruned ${removed} archive file(s) older than ${cutoff}; $(ls docs/archive/*.ubx 2>/dev/null | wc -l) left"

# ── Publish ─────────────────────────────────────────────────────────────────
# Only ever replace latest.ubx with a non-empty new file: a receiver fetching it
# mid-run must never be handed a truncated or empty blob, and it applies
# whatever it is given.
src="docs/archive/agps_${TS}.ubx"
if [ -s "$src" ]; then
    cp "$src" docs/latest.ubx.tmp
    mv docs/latest.ubx.tmp docs/latest.ubx
    echo "latest.ubx <- ${src} ($(stat -c%s docs/latest.ubx) bytes)"
else
    echo "::error::nothing produced this run - keeping the previous docs/latest.ubx"
    exit 1
fi

# ── Verify exactly what is about to be published ────────────────────────────
python3 agps_download.py --verify docs/latest.ubx
for f in docs/test/*.ubx; do
    [ -s "$f" ] && python3 agps_download.py --verify "$f" > /dev/null
done
echo "verified latest.ubx and $(ls docs/test/*.ubx 2>/dev/null | wc -l) test variant(s)"

# ── Status page ─────────────────────────────────────────────────────────────
cat > docs/index.html <<EOF
<html>
<body>
<h1>A-GPS Server</h1>
<p>Last update: ${TS} UTC</p>
<p><a href="latest.ubx">latest.ubx</a> — u-blox AssistNow aiding (almanac and
three days of predicted orbits) plus this hour's precise broadcast ephemeris
and ionosphere model from RINEX, in one UBX-MGA stream. Ephemeris covers GPS,
Galileo and BeiDou. GLONASS and QZSS are left out: the receiver does not track
them.</p>
<p>The file carries no UBX-MGA-INI-TIME: the receiver supplies its own time when
it applies the data, so a file downloaded hours after it was built is never
wrong about the hour.</p>
<h2>Test variants</h2>
<p>One file per hypothesis, all built from the same data, for bisecting a
receiver that will not fix. Each rung adds one thing to the one above it.</p>
<ul>
  <li><a href="test/01-eph-gps.ubx">01-eph-gps</a> — GPS ephemeris, nothing else</li>
  <li><a href="test/02-eph-all.ubx">02-eph-all</a> — plus Galileo and BeiDou ephemeris</li>
  <li><a href="test/03-eph-all-iono.ubx">03-eph-all-iono</a> — plus the ionosphere model</li>
  <li><a href="test/04-eph-alm.ubx">04-eph-alm</a> — plus almanac</li>
  <li><a href="test/05-eph-alm-ano1.ubx">05-eph-alm-ano1</a> — plus 1 day of predicted orbits</li>
  <li><a href="test/06-full.ubx">06-full</a> — plus all 3 days: same as latest.ubx</li>
</ul>
</body>
</html>
EOF

# ── Commit ──────────────────────────────────────────────────────────────────
if [ "$PUBLISH" != "1" ]; then
    echo "PUBLISH=0 — built but not committed"
    exit 0
fi

git config user.name "agps-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add docs/
if git diff --cached --quiet; then
    echo "no changes to commit"
    exit 0
fi
git commit -q -m "A-GPS refresh ${TS}"
# The caller loops for hours, so someone may have pushed meanwhile. Try first
# and only reconcile if refused: actions/checkout clones shallow, where an
# unconditional pull is the operation more likely to fail.
if ! git push 2>/dev/null; then
    branch=$(git rev-parse --abbrev-ref HEAD)
    git pull --rebase --quiet origin "$branch"
    git push
fi
echo "pushed A-GPS refresh ${TS}"
