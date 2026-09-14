#!/bin/bash
# Daily DoorDash deal scan. Invoked by a systemd timer at 11:30 America/Chicago.
# See CLAUDE.md "Schedule: systemd, and why".
set -uo pipefail

BASE="${HOME:-/home/ubuntu}/deal-scanner"
STATE="$BASE/state"
SCAN="$STATE/scan"
SCANNER="$BASE/scanner"
LOGDIR="$BASE/logs"
export PATH="/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin"

AWS=/usr/local/bin/aws
CLAUDE=/usr/bin/claude

DATE_CHI="$(TZ=America/Chicago date +%Y-%m-%d)"
HOUR_CHI="$(TZ=America/Chicago date +%H)"
LOG="$LOGDIR/$DATE_CHI.log"
mkdir -p "$LOGDIR" "$SCAN" "$STATE"

log() { echo "[$(TZ=America/Chicago date '+%Y-%m-%d %H:%M:%S %Z')] $*" >> "$LOG"; }

# ---------------------------------------------------------------- gates

# The systemd timer uses OnCalendar with an explicit America/Chicago timezone,
# so it already fires at the right wall-clock time in both DST seasons and the
# box's own timezone (UTC) does not matter.
#
# The window is "10:00 to 19:59 Chicago" rather than "hour == 11" on purpose.
# Persistent=true makes systemd run a missed job after the machine was off; a
# strict hour test would throw that catch-up run away and the day would produce
# no report at all. A late report is better than none. The upper bound keeps a
# very late catch-up from mailing a lunch report at 3am.
if [ "$HOUR_CHI" -lt 10 ] || [ "$HOUR_CHI" -ge 20 ]; then
    exit 0
fi

# At most one SUCCESSFUL run per day. The marker is written at the very END of
# a good run, not here. Writing it up front meant a run that died on an expired
# token consumed the whole day: the token could be fixed an hour later and the
# scan would still refuse to run until tomorrow. That cost 2026-08-17 and the
# first attempt of 2026-08-18 outright.
#
# Attempts are counted separately so a persistently broken scan cannot mail an
# alert on every timer fire or manual retry.
MARKER="$STATE/last_run_$DATE_CHI"
ATTEMPTS="$STATE/attempts_$DATE_CHI"
MAX_ATTEMPTS=3
if [ -e "$MARKER" ]; then
    log "already ran successfully today, exiting"
    exit 0
fi
ATTEMPT_N=0
[ -f "$ATTEMPTS" ] && ATTEMPT_N=$(cat "$ATTEMPTS" 2>/dev/null || echo 0)
case "$ATTEMPT_N" in ''|*[!0-9]*) ATTEMPT_N=0 ;; esac
ATTEMPT_N=$((ATTEMPT_N + 1))
echo "$ATTEMPT_N" > "$ATTEMPTS"
if [ "$ATTEMPT_N" -gt "$MAX_ATTEMPTS" ]; then
    log "attempt $ATTEMPT_N today, already failed $MAX_ATTEMPTS times; exiting quietly"
    exit 0
fi
[ "$ATTEMPT_N" -gt 1 ] && log "retry: attempt $ATTEMPT_N of $MAX_ATTEMPTS today"
find "$STATE" -maxdepth 1 \( -name 'last_run_*' -o -name 'attempts_*' \) \
     -mtime +7 -delete 2>/dev/null

log "=== scan start (Chicago $DATE_CHI) ==="
log "system time: $(date '+%Y-%m-%d %H:%M:%S %Z')"

# Linux has no OS keychain, so dd-cli authenticates from the DD_CLI_ACCESS_TOKEN
# environment variable instead (headless mode, dd-cli >= 0.2.2). The token lives
# in ~/.dd_token, chmod 600, and is sourced here rather than relying on the
# systemd unit's environment so an interactive run behaves identically.
if [ -r /home/ubuntu/.dd_token ]; then
    # shellcheck disable=SC1091
    . /home/ubuntu/.dd_token
    if [ -n "${DD_CLI_ACCESS_TOKEN:-}" ]; then
        export DD_CLI_ACCESS_TOKEN
        log "dd-cli token loaded from ~/.dd_token"
    else
        log "WARN: ~/.dd_token did not set DD_CLI_ACCESS_TOKEN"
    fi
else
    log "WARN: /home/ubuntu/.dd_token missing or unreadable; dd-cli will fail"
fi

# Stage 4 needs its own long-lived Claude credential. The interactive Claude Code
# login on this box uses a refresh token that expired on 2026-09-11 and silently
# emptied ~/.claude/.credentials.json, so stage 4 failed with "OAuth session
# expired" while the script still marked the day done. A token from
# `claude setup-token` does not expire on its own; keep it in
# ~/.claude_oauth_token, chmod 600, and source it the same way as the dd token.
if [ -r /home/ubuntu/.claude_oauth_token ]; then
    # shellcheck disable=SC1091
    . /home/ubuntu/.claude_oauth_token
    if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
        export CLAUDE_CODE_OAUTH_TOKEN
        log "claude oauth token loaded from ~/.claude_oauth_token"
    else
        log "WARN: ~/.claude_oauth_token did not set CLAUDE_CODE_OAUTH_TOKEN; stage 4 will fall back to the box's own Claude login, which expires"
    fi
else
    log "WARN: /home/ubuntu/.claude_oauth_token missing or unreadable; stage 4 will fall back to the box's own Claude login, which expires"
fi

# Pre-flight on the token. An expired token is the single most common cause of
# a failed scan and it is silent: every one of the 136 fetches fails the same
# way, eight minutes in. Decode the JWT expiry up front instead - fail fast and
# name the cause, and warn while there is still time to renew.
TOKEN_WARN=""
if [ -n "${DD_CLI_ACCESS_TOKEN:-}" ]; then
    TOKEN_EXP=$(python3 -c 'import base64, json, os
t = os.environ.get("DD_CLI_ACCESS_TOKEN", "")
try:
    p = t.split(".")[1]
    p += "=" * (-len(p) % 4)
    print(json.loads(base64.urlsafe_b64decode(p))["exp"])
except Exception:
    print("")' 2>/dev/null)
    case "$TOKEN_EXP" in ''|*[!0-9]*) TOKEN_EXP="" ;; esac
    if [ -n "$TOKEN_EXP" ]; then
        NOW_EPOCH=$(date +%s)
        HOURS_LEFT=$(( (TOKEN_EXP - NOW_EPOCH) / 3600 ))
        log "token expires $(date -u -d "@$TOKEN_EXP" '+%Y-%m-%d %H:%M UTC') (${HOURS_LEFT}h left)"
        if [ "$TOKEN_EXP" -le "$NOW_EPOCH" ]; then
            log "FATAL: token already expired; not attempting any fetch"
            BODY_TOK="$STATE/email_body_${DATE_CHI}_token.txt"
            {
                echo "DoorDash deal scan could not run for $DATE_CHI."
                echo
                echo "The dd-cli access token in ~/.dd_token expired on"
                echo "$(date -u -d "@$TOKEN_EXP" '+%Y-%m-%d %H:%M UTC')."
                echo
                echo "No fetches were attempted, so nothing was scored against"
                echo "stale prices and no data was touched."
                echo
                echo "To renew, on a machine with a browser:"
                echo "  ssh -L 4180:localhost:4180 dealbox"
                echo "  ~/mint_token.sh"
                echo
                echo "The scan will retry by itself later today once the token"
                echo "is renewed - a failed attempt no longer consumes the day."
            } > "$BODY_TOK"
            "$BASE/send_mail.sh" \
                "[dealbox] DoorDash deals - $DATE_CHI - TOKEN EXPIRED" "$BODY_TOK" >/dev/null 2>&1
            log "=== scan end (token expired) ==="
            exit 1
        fi
        if [ "$HOURS_LEFT" -le 72 ]; then
            TOKEN_WARN="NOTE: the dd-cli token expires in ${HOURS_LEFT}h, on $(date -u -d "@$TOKEN_EXP" '+%a %Y-%m-%d %H:%M UTC'). Renew it with ~/mint_token.sh on dealbox or the next scan after that time will not run."
            log "$TOKEN_WARN"
        fi
    else
        log "WARN: could not decode token expiry"
    fi
fi

INTENT='Summary: Help the user find the best-value meal deals near their saved address.
user prompt/purpose: "i want it as a research tool that will help me find the absolute best deals for lunch and dinner for me"'

RATE_LIMITED=0
PROMO_OK=0
PROMO_FAIL=0
MENU_OK=0
MENU_FAIL=0

# Treat a rate limit as "stop cleanly and continue tomorrow", not as a failure.
is_rate_limited() {
    grep -qi 'rate limit' "$1" 2>/dev/null
}

fetch() {
    # fetch <kind> <store_id>  -> writes $SCAN/<kind>_<id>.json, returns 2 if rate limited
    local kind="$1" sid="$2"
    local out="$SCAN/${kind}_${sid}.json" err="$SCAN/${kind}_${sid}.err"
    local tmp="$out.tmp"

    if [ "$kind" = "menu" ]; then
        dd-cli --json-output menu --store-id "$sid" --intent "$INTENT" > "$tmp" 2> "$err"
    else
        dd-cli --json-output promo list --store-id "$sid" --intent "$INTENT" > "$tmp" 2> "$err"
    fi
    local rc=$?

    if is_rate_limited "$err"; then
        rm -f "$tmp"
        log "RATE LIMIT hit on $kind $sid: $(tr -d '\n' < "$err" | cut -c1-160)"
        return 2
    fi
    # Only replace good data with good data. A failed call must not destroy
    # yesterday's usable menu.
    if [ $rc -eq 0 ] && [ -s "$tmp" ]; then
        mv "$tmp" "$out"
        return 0
    fi
    rm -f "$tmp"
    return 1
}

# ------------------------------------------------- stage 1: promo refresh

# Rotating cursor: resume where the last (possibly rate limited) run stopped,
# so coverage rotates through the census instead of always re-fetching the head.
cp -f "$SCANNER/stores.tsv" "$STATE/stores.tsv"
# Stores the user has permanently ruled out (scanner/excluded_stores.tsv).
# Filtering here, before stage 1, stops an excluded store being FETCHED. That is
# only half the job and must not be mistaken for the whole one: the stores.tsv
# copied to state/ above is unfiltered, and any menu_*.json / promo_*.json cached
# in state/scan from before the ruling is still there. extract.py globs that
# directory whole, so those stale files kept feeding items.tsv every run - and
# since an excluded store is never re-fetched, they never aged out. 658055
# reached rank 2 of ranked.tsv that way.
# score.py applies the AUTHORITATIVE filter, reading the same file and dropping
# excluded stores before ranking. That is what actually keeps them out of the
# report. This skip just avoids wasting fetch calls on them.
EXCLUDED_IDS=""
if [ -f "$SCANNER/excluded_stores.tsv" ]; then
    EXCLUDED_IDS=$(tail -n +2 "$SCANNER/excluded_stores.tsv" | cut -f1)
fi
STORE_IDS=()
while IFS= read -r sid; do
    [ -z "$sid" ] && continue
    if [ -n "$EXCLUDED_IDS" ] && printf '%s\n' "$EXCLUDED_IDS" | grep -qxF "$sid"; then
        log "skipping excluded store $sid"
        continue
    fi
    STORE_IDS+=("$sid")
done < <(tail -n +2 "$SCANNER/stores.tsv" | cut -f1)
N=${#STORE_IDS[@]}
CURSOR=0
[ -f "$STATE/promo_cursor" ] && CURSOR=$(cat "$STATE/promo_cursor" 2>/dev/null || echo 0)
case "$CURSOR" in ''|*[!0-9]*) CURSOR=0 ;; esac
[ "$CURSOR" -ge "$N" ] && CURSOR=0

log "stage 1: promo refresh, $N stores, starting at cursor $CURSOR"
i=0
while [ $i -lt $N ]; do
    idx=$(( (CURSOR + i) % N ))
    sid="${STORE_IDS[$idx]}"
    fetch promo "$sid"
    rc=$?
    if [ $rc -eq 2 ]; then
        RATE_LIMITED=1
        CURSOR=$idx      # resume on this store tomorrow
        break
    fi
    [ $rc -eq 0 ] && PROMO_OK=$((PROMO_OK+1)) || PROMO_FAIL=$((PROMO_FAIL+1))
    i=$((i+1))
    sleep 3
done
if [ $RATE_LIMITED -eq 0 ]; then
    CURSOR=0             # full pass completed, start fresh next time
fi
echo "$CURSOR" > "$STATE/promo_cursor"
log "stage 1 done: ok=$PROMO_OK fail=$PROMO_FAIL rate_limited=$RATE_LIMITED next_cursor=$CURSOR"

# ------------------------------------------- stage 2: 20 stalest menus

if [ $RATE_LIMITED -eq 0 ]; then
    log "stage 2: menu refresh for 20 stalest stores"
    # Stalest first: stores with no menu file at all rank ahead of old ones.
    # GNU stat uses -c %Y where BSD/macOS stat used -f %m.
    STALE=$(
        for sid in "${STORE_IDS[@]}"; do
            f="$SCAN/menu_${sid}.json"
            if [ -s "$f" ]; then
                echo "$(stat -c %Y "$f") $sid"
            else
                echo "0 $sid"
            fi
        done | sort -n | head -20 | awk '{print $2}'
    )
    for sid in $STALE; do
        fetch menu "$sid"
        rc=$?
        if [ $rc -eq 2 ]; then
            RATE_LIMITED=1
            break
        fi
        [ $rc -eq 0 ] && MENU_OK=$((MENU_OK+1)) || MENU_FAIL=$((MENU_FAIL+1))
        sleep 3
    done
    log "stage 2 done: ok=$MENU_OK fail=$MENU_FAIL rate_limited=$RATE_LIMITED"
else
    log "stage 2 skipped: already rate limited in stage 1"
fi

# ------------------------------------------- stage 3: extract + score

# If nothing at all was fetched, the tables in state/ still hold whatever the
# previous run left behind. Scoring them produces a confident, ordinary-looking
# report describing prices nobody checked today, which is worse than no report:
# the failure is invisible to the reader. Stop here and mail a plain alert
# instead. A partial sweep (some stores fetched) is still worth reporting and
# falls through normally.
if [ $PROMO_OK -eq 0 ] && [ $MENU_OK -eq 0 ]; then
    log "FATAL: zero successful fetches (promo_fail=$PROMO_FAIL menu_fail=$MENU_FAIL)"
    BODY_FAIL="$STATE/email_body_${DATE_CHI}_failed.txt"
    ERR_SAMPLE=$(cat "$SCAN"/promo_*.err 2>/dev/null | sort -u | head -3)
    log "error sample: $ERR_SAMPLE"
    {
        echo "DoorDash deal scan FAILED for $DATE_CHI."
        echo
        echo "Every fetch failed: $PROMO_FAIL promo calls, $MENU_FAIL menu calls."
        echo "No report was produced. Today's tables were left untouched, so"
        echo "nothing downstream was scored against stale prices."
        echo
        echo "Error reported by dd-cli:"
        echo "$ERR_SAMPLE"
        echo
        echo "A likely cause is an expired DD_CLI_ACCESS_TOKEN in ~/.dd_token."
        echo "See logs/$DATE_CHI.log on dealbox."
    } > "$BODY_FAIL"
    ALERT_OUT=$("$BASE/send_mail.sh" \
        "[dealbox] DoorDash deals - $DATE_CHI - SCAN FAILED" "$BODY_FAIL" 2>&1)
    ALERT_RC=$?
    log "alert send rc=$ALERT_RC:"$'\n'"$ALERT_OUT"
    log "=== scan end (aborted) ==="
    exit 1
fi

log "stage 3: extract + score"
EXTRACT_OUT=$(cd "$SCANNER" && python3 extract.py "$SCAN" -o "$STATE" 2>&1)
log "extract:"$'\n'"$EXTRACT_OUT"

SCORE_OUT=$(cd "$SCANNER" && python3 score.py --dir "$STATE" \
    --calibration "$SCANNER/calibration/previews.tsv" 2>&1)
log "score:"$'\n'"$SCORE_OUT"

# ------------------------------------------- stage 4: verify + compose

BODY="$STATE/email_body_$DATE_CHI.txt"
log "stage 4: claude -p (opus) verify + compose"

read -r -d '' PROMPT <<EOF
Run today's deal scan report. Date: $DATE_CHI (America/Chicago).

Read CLAUDE.md in this directory first and follow it exactly, especially the
safety rules and the output contract. deal-hunt-prompt.md is the mission spec.

The deterministic stages already ran. Fresh tables are in state/.

extract.py output:
$EXTRACT_OUT

score.py output:
$SCORE_OUT

Today's fetch stats: promo_ok=$PROMO_OK promo_fail=$PROMO_FAIL
menu_ok=$MENU_OK menu_fail=$MENU_FAIL rate_limited=$RATE_LIMITED
(rate_limited=1 means the sweep stopped early and resumes tomorrow from the
saved cursor; report it as a coverage note, not an error.)

PICKUP LANE - new today.

state/verify_list.tsv now carries a trailing fulfillment column, one of:

  delivery  verify as before, with an ordinary delivery preview.
  pickup    verify with a REAL pickup preview, procedure below.
  both      the same cart cleared the bar on both lanes. Build ONE cart and
            preview it twice, delivery first, then pickup.

score.py also writes state/ranked_pickup.tsv, the full pickup lane. Pickup is
only ever valid for a store within 4.0 km of home (e-bike range); those files are
already filtered to that radius, so do not add pickup stores of your own.

Pickup preview procedure, in this exact order:

  1. Run cart list --store-id with the store id FIRST. If a cart already exists
     for that store, SKIP the store entirely. Do not reuse it, do not delete it.
  2. Build ONE cart for the candidate.
  3. Run order preview --cart-uuid with the cart uuid and --fulfillment delivery.
  4. Run order preview --cart-uuid with the same uuid and --fulfillment pickup.
     The --fulfillment flag MUTATES the cart, so the order of steps 3 and 4
     matters, and the flag must be passed EXPLICITLY on every preview call
     including the delivery one. Never rely on a default.
  5. In the pickup quote, delivery_availability.asap_pickup_available must be
     true. If it is not, that store is not offering pickup right now: drop the
     pickup candidate and say so in the report.
  6. Record BOTH previews as rows in scanner/calibration/previews.tsv, each with
     the right value in the fulfillment column.
  7. Delete the cart immediately afterwards, with the existing retry loop.

The pickup totals in those files are ESTIMATES, not prices. A pickup order has no
service fee, no delivery fee and tax on food only, but the promo math is the
delivery lane's, and many DoorDash promos are DELIVERY-ONLY: they do not error on
a pickup cart, they just silently fail to attach. score.py drops the discount
only for a promo whose description literally says "delivery only" - the rest it
cannot tell apart. So a pickup preview can come back HIGHER than the estimate,
and sometimes higher than the same cart delivered. That is a valid finding to
report, not an error, not a reason to retry, and not a reason to drop the row.
Only the previews are authoritative.

The email body must now contain BOTH a delivery deals list AND a pickup deals
list. The exact format of both is defined in CLAUDE.md - follow that contract,
it is authoritative and it has been updated for this.

Do the verification previews for the top 8 unconfirmed candidates in
verify_list.tsv, honouring each row's fulfillment column (a "both" row is one
cart previewed twice, and counts as one candidate). Append every verified preview
to scanner/calibration/previews.tsv, delete every cart you
created, then write ONLY the finished plain-text email body to state/email_body_$DATE_CHI.txt
using the Write tool. No markdown, no commentary outside that file.
EOF

cd "$BASE" || exit 1
CLAUDE_OUT=$($CLAUDE -p --model claude-opus-5 --permission-mode bypassPermissions "$PROMPT" 2>&1)
CLAUDE_RC=$?
log "claude rc=$CLAUDE_RC"
log "claude output:"$'\n'"$CLAUDE_OUT"

# Did stage 4 actually produce a report? Tracked separately from "did we send
# mail", because a fallback body is still worth mailing but must not count as a
# good run - see the marker at the end.
STAGE4_OK=1

# The model writes the body to a file. If it did not, fall back to its stdout so
# the run still delivers something rather than silently sending nothing. Lead
# with an explicit failure line so the reader is not left to guess why the mail
# looks like raw model output instead of a report.
if [ ! -s "$BODY" ]; then
    log "WARN: no email body file, falling back to claude stdout"
    STAGE4_OK=0
    {
        printf 'SCAN FAILED at the model step (stage 4), claude exit code %s.\n\n' "$CLAUDE_RC"
        if [ -n "$CLAUDE_OUT" ]; then
            printf '%s\n' "$CLAUDE_OUT"
        else
            printf 'Deal scan for %s produced no report.\nclaude exit code: %s\nSee logs/%s.log\n' \
                "$DATE_CHI" "$CLAUDE_RC" "$DATE_CHI"
        fi
    } > "$BODY"
fi

# A nonzero exit is a failed run even when a body file exists: the model may have
# written a partial report before dying. Mail whatever it wrote, but do not let
# the day be marked done on it.
if [ "$CLAUDE_RC" -ne 0 ]; then
    STAGE4_OK=0
    log "WARN: claude exited $CLAUDE_RC; sending the body anyway but not counting the run as good"
fi

# ------------------------------------------- stage 5: send

log "stage 5: send via gmail smtp"
# Surface an imminent token expiry in the report he already reads, rather than
# as one more separate mail.
# Prepend at most once. A re-run on the same day can reach here with a body that
# already carries the NOTE - stage 4 only overwrites the body when the model
# actually writes one, so a failed re-run leaves the previous run's file in place
# and the NOTE was being stacked on top of itself (state/email_body_2026-08-24.txt
# ended up with it on lines 1 and 3).
# The test matches the fixed prefix rather than "$TOKEN_WARN" itself, because the
# "expires in Nh" count differs between runs and an exact compare would never
# match. The cost of that is that a re-run keeps the earlier run's hour count,
# which is a few hours stale at worst - much better than printing it twice.
if [ -n "$TOKEN_WARN" ] && [ -f "$BODY" ] \
   && ! grep -q '^NOTE: the dd-cli token expires' "$BODY"; then
    { echo "$TOKEN_WARN"; echo; cat "$BODY"; } > "$BODY.warn" && mv "$BODY.warn" "$BODY"
fi
MAIL_OUT=$("$BASE/send_mail.sh" "[dealbox] DoorDash deals - $DATE_CHI" "$BODY" 2>&1)
MAIL_RC=$?
log "send rc=$MAIL_RC:"$'\n'"$MAIL_OUT"

# Only a run that got a real report out of stage 4 is genuinely done. See the
# MARKER comment at the top: the marker blocks every further attempt today, so a
# stage 4 failure must leave it unwritten and let the attempts counter decide how
# many more retries the day gets.
if [ "$STAGE4_OK" -eq 1 ]; then
    : > "$MARKER"
else
    log "stage 4 failed; not writing the day marker so a later attempt today can retry (attempt $ATTEMPT_N of $MAX_ATTEMPTS)"
fi
log "=== scan end ==="
exit 0
