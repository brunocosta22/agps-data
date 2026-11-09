#!/usr/bin/env python3
"""Ask GitHub to run the A-GPS refresh now.

GitHub's own scheduler will not do it hourly. This repo's cron asked for hourly
and got it -- 23 runs a day -- until the Actions incidents of 2026-08-24 and
2026-08-26; since then the same cron delivers a run every 2 to 6 hours,
averaging 4.4. The runs all succeed in under half a minute, the workflow is
active, and scheduled events are documented as best-effort, so there is nothing
in the repository to fix. An external trigger is the only thing that keeps time.

It matters because broadcast ephemeris is valid for its ~4 h fit interval: past
that the receiver rejects it and only the predicted orbits still help.

Run it from a machine that does keep time:

    GH_TOKEN=github_pat_... ./trigger-refresh.sh

or point it at a file, which is what the crontab line does so the token never
appears in a process listing:

    GH_TOKEN_FILE=~/.config/agps/gh-token ./trigger-refresh.sh

The token needs one permission -- Actions: write on this repository -- and
nothing else; a fine-grained token scoped to this repo alone is the right shape.

Written in Python with nothing but the standard library, because python3 is
already required to build the file and curl is not always installed.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# Warn this many days before the token expires. A fine-grained token defaults to
# 30 days, and when the first one lapsed the only symptom was an hourly "Bad
# credentials" in a log nobody was reading -- the refresh quietly fell back to
# GitHub's own scheduling for 14 days before anyone noticed.
EXPIRY_WARNING_DAYS = 10

# Retry a transient failure rather than losing the hour. Over seven days of
# hourly runs the only three misses were one DNS lookup that fired two minutes
# after the machine booted, before the resolver was up, and two 502s from
# GitHub -- all of them gone by the next attempt, none of them helped by waiting
# a full hour for the next cron tick. A wrong token or a missing permission is
# not transient and is not retried.
RETRY_DELAYS_S = (20, 60)
RETRYABLE_STATUS = (500, 502, 503, 504, 429)

REPO = os.environ.get("REPO", "brunocosta22/agps-data")
WORKFLOW = os.environ.get("WORKFLOW", "agps.yml")
REF = os.environ.get("REF", "main")


def read_token() -> str:
    token = os.environ.get("GH_TOKEN", "").strip()
    if token:
        return token
    path = os.environ.get("GH_TOKEN_FILE", "")
    if path:
        # Say what is wrong in one line rather than raising: this runs from cron
        # every hour, and a missing or unreadable token file would otherwise put
        # a traceback in the log each time.
        try:
            with open(os.path.expanduser(path)) as f:
                token = f.read().strip()
        except OSError as exc:
            raise SystemExit(f"cannot read the token from {path}: {exc.strerror}")
        if not token:
            raise SystemExit(f"the token file {path} is empty")
        return token
    raise SystemExit("set GH_TOKEN, or GH_TOKEN_FILE to a file holding it "
                     f"(needs Actions: write on {REPO})")


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
        data=json.dumps({"ref": REF}).encode(),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {read_token()}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    for attempt, delay in enumerate((0,) + RETRY_DELAYS_S):
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                code, headers = r.status, r.headers
        except urllib.error.HTTPError as exc:
            # Keep it to one line: this runs hourly, and the first token to
            # lapse put a four-line JSON body in the log 24 times a day for two
            # weeks.
            detail = {
                401: "the token is invalid or has expired",
                403: f"the token lacks Actions: write on {REPO}",
                404: f"no workflow named {WORKFLOW}, or no access to {REPO}",
            }.get(exc.code, exc.read().decode(errors="replace")[:120].replace("\n", " "))
            problem, retryable = f"HTTP {exc.code} -- {detail}", exc.code in RETRYABLE_STATUS
        except urllib.error.URLError as exc:
            # Includes the resolver not being up yet, which is what a cron tick
            # a couple of minutes after boot runs into.
            problem, retryable = str(exc.reason), True
        else:
            # A successful dispatch is 204 No Content.
            if attempt:
                print(f"{stamp} dispatched {WORKFLOW} on {REF} (HTTP {code}) "
                      f"after {attempt} retry(s)")
            else:
                print(f"{stamp} dispatched {WORKFLOW} on {REF} (HTTP {code})")
            warn_if_expiring(headers)
            return 0

        if not retryable or attempt == len(RETRY_DELAYS_S):
            print(f"{stamp} dispatch failed: {problem}", file=sys.stderr)
            return 1
        print(f"{stamp} dispatch failed: {problem} -- retrying in "
              f"{RETRY_DELAYS_S[attempt]}s", file=sys.stderr)
    return 1


def warn_if_expiring(headers) -> None:
    """Say so while there is still time to act.

    GitHub returns the expiry of a fine-grained token in a response header, so
    the warning costs no extra request. Absent header, or one in a shape not
    recognised, means nothing is said -- a classic PAT has no expiry to report.
    """
    raw = (headers.get("github-authentication-token-expiration") or "").strip()
    if not raw:
        return
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            expires = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    else:
        return
    left = expires - datetime.now(timezone.utc)
    if left <= timedelta(days=EXPIRY_WARNING_DAYS):
        print(f"WARNING: the token expires in {left.days} day(s), on "
              f"{expires.date()} -- renew it or the refresh silently falls back "
              f"to GitHub's own scheduling, which is every 4 to 7 hours",
              file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
