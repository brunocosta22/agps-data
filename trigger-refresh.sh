#!/usr/bin/env bash
# Ask GitHub to run the A-GPS refresh now.
#
# GitHub's own scheduler will not do it hourly. This repo's cron asked for
# hourly and got it -- 23 runs a day -- until the Actions incidents of
# 2026-08-24 and 2026-08-26; since then the same cron delivers a run every 2 to
# 6 hours, averaging 4.4. The runs all succeed in under half a minute, the
# workflow is active, and scheduled events are documented as best-effort, so
# there is nothing in the repository to fix. An external trigger is the only
# thing that keeps time.
#
# It matters because broadcast ephemeris is valid for its ~4 h fit interval:
# past that the receiver rejects it and only the predicted orbits still help.
#
# Run this from a machine that does keep time:
#
#   GH_TOKEN=ghp_... ./trigger-refresh.sh
#
# The token needs one permission -- Actions: write on this repository -- and
# nothing else. A fine-grained token scoped to this repo alone is the right
# shape. In crontab, hourly, with the token in a file only root can read:
#
#   5 * * * * GH_TOKEN=$(cat /etc/agps-gh-token) /opt/agps/trigger-refresh.sh >> /var/log/agps-trigger.log 2>&1
#
# Nothing else has to change: the workflow keeps its hourly cron as a fallback,
# and a dispatch and a schedule do exactly the same work.
set -euo pipefail

REPO=${REPO:-brunocosta22/agps-data}
WORKFLOW=${WORKFLOW:-agps.yml}
REF=${REF:-main}

: "${GH_TOKEN:?set GH_TOKEN to a token with Actions: write on ${REPO}}"

code=$(curl -sS -o /tmp/agps-trigger-body -w '%{http_code}' \
    -X POST \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches" \
    -d "{\"ref\":\"${REF}\"}")

# 204 No Content is success and carries no body.
if [ "$code" = "204" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) dispatched ${WORKFLOW} on ${REF}"
    exit 0
fi
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) dispatch failed: HTTP ${code}" >&2
cat /tmp/agps-trigger-body >&2
echo >&2
exit 1
