#!/bin/bash
# Send a deal-scanner mail. usage: send_mail.sh <subject> <body-file>
#
# Primary path: Gmail SMTP, authenticated as the account itself.
#
# Why not SES: the reports are From and To the user's own gmail.com address.
# Sent through SES they arrived from a host that no gmail.com SPF record
# authorises and carried no gmail.com DKIM signature, so From did not align
# with either. That is exactly the shape of a spoof, and Gmail treats mail
# claiming to be from its own domain strictly, so the reports were silently
# dropped or spam-foldered. Authenticating to smtp.gmail.com as the account
# makes the envelope, SPF and DKIM all agree with the From header.
#
# Fallback path: the original SES send, tagged [SES-FALLBACK] in the subject so
# that a degraded delivery is visible in the inbox and not only in a log file.
#
# Linux/dealbox counterpart of the Mac script. Differences: aws lives in
# /usr/local/bin, and mktemp is GNU so it needs an explicit XXXXXX template
# rather than the BSD "-t <name>" form.

set -uo pipefail

SUBJECT="${1:-}"
BODY_FILE="${2:-}"
ADDR="${DEALSCAN_EMAIL:?set DEALSCAN_EMAIL to the gmail address that sends and receives the report}"
AWS=/usr/local/bin/aws
# systemd runs this with User=ubuntu but no explicit Environment=HOME, so fall
# back the same way daily_scan.sh does rather than trusting $HOME to be set.
PW_FILE="${HOME:-/home/ubuntu}/.gmail_app_pw"

if [ -z "$SUBJECT" ] || [ -z "$BODY_FILE" ]; then
    echo "send_mail: usage: send_mail.sh <subject> <body-file>" >&2
    exit 2
fi
if [ ! -s "$BODY_FILE" ]; then
    echo "send_mail: body file missing or empty: $BODY_FILE" >&2
    exit 2
fi

send_ses() {
    local out rc
    out=$($AWS ses send-email \
        --profile mailer --region us-east-1 \
        --from "$ADDR" --to "$ADDR" \
        --subject "$1" \
        --text "file://$BODY_FILE" 2>&1)
    rc=$?
    echo "send_mail: path=ses rc=$rc: $out"
    return $rc
}

# Google shows the 16-character app password in four groups of four, and
# copying it out of the browser brings the spaces along. AUTH rejects those, so
# strip all whitespace before use.
PW=""
if [ -r "$PW_FILE" ]; then
    PW=$(tr -d '[:space:]' < "$PW_FILE")
fi

if [ -z "$PW" ]; then
    echo "send_mail: no app password in $PW_FILE, falling back to SES"
    send_ses "$SUBJECT [SES-FALLBACK]"
    exit $?
fi

# The password is interpolated into a curl config file, where double quotes and
# backslashes are metacharacters. A real app password is 16 alphanumerics, so
# anything else means the file holds something unexpected and we should not try
# to quote our way around it.
case "$PW" in
    *[!A-Za-z0-9]*)
        echo "send_mail: app password has unexpected characters, falling back to SES"
        send_ses "$SUBJECT [SES-FALLBACK]"
        exit $?
        ;;
esac

# Build the RFC 5322 message. SMTP requires CRLF line endings and the body is
# written by ordinary unix tools with bare LF, so convert. Stripping any
# trailing CR first keeps this correct if the input is already CRLF.
MSG=$(mktemp "${TMPDIR:-/tmp}/dealscan_mail.XXXXXX") || exit 1
trap 'rm -f "$MSG"' EXIT

{
    echo "From: $ADDR"
    echo "To: $ADDR"
    echo "Subject: $SUBJECT"
    echo "Date: $(date -R)"
    echo "Message-ID: <$(date +%Y%m%d%H%M%S).$$@gmail.com>"
    echo "MIME-Version: 1.0"
    echo "Content-Type: text/plain; charset=UTF-8"
    echo
    cat "$BODY_FILE"
} | awk '{ sub(/\r$/, ""); printf "%s\r\n", $0 }' > "$MSG"

# Credentials go in via a config file on stdin rather than argv, so the
# password never appears in `ps` output.
#
# Deliberately no -v here: verbose mode prints the SMTP conversation, and the
# AUTH line in it is the base64-encoded password. The scanner redirects this
# output to a log file, so -v would write the credential to disk.
CURL_OUT=$(curl -sS --config - 2>&1 <<EOF
url = "smtps://smtp.gmail.com:465"
ssl-reqd
mail-from = "$ADDR"
mail-rcpt = "$ADDR"
user = "$ADDR:$PW"
upload-file = "$MSG"
EOF
)
CURL_RC=$?

if [ $CURL_RC -eq 0 ]; then
    echo "send_mail: path=gmail-smtp rc=0 accepted by smtp.gmail.com"
    exit 0
fi

echo "send_mail: gmail smtp failed rc=$CURL_RC: $CURL_OUT"
echo "send_mail: falling back to SES"
send_ses "$SUBJECT [SES-FALLBACK]"
exit $?
