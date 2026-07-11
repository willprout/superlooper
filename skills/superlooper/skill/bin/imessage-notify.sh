#!/usr/bin/env bash
# Send ONE iMessage via the Mac's own Messages.app (plan Task 11, notify.imessage_to channel).
# The agent-boundary rule keeps every macOS/Apple specific here + in notify.py's precedence, not
# in the runner: swapping notifiers never touches loop logic.
#
# Usage: imessage-notify.sh <recipient> <title> [<body>]
#   <recipient>  a phone number (+1…) or Apple ID email registered with iMessage
#   <title>      the headline (the report/alert reason)
#   <body>       optional detail line
#
# It calls `osascript` by BARE NAME (so a stub on PATH intercepts it in tests) and drives Messages
# via AppleScript. A nonzero exit is fine — notify.py turns it into a journaled outcome, never a
# raise. The FIRST real send triggers a one-time macOS automation-permission prompt ("Terminal
# wants to control Messages" → Allow); the launchd-started nightly needs that permission granted to
# whatever user it runs under too (references/runner-ops.md).
set -u

recipient="${1:-}"
title="${2:-}"
body="${3:-}"

if [ -z "$recipient" ]; then
    echo "imessage-notify.sh: no recipient" >&2
    exit 2
fi

# One message: the title, and the body on its own line when present.
if [ -n "$body" ]; then
    message="$title
$body"
else
    message="$title"
fi

# argv-passed (never string-interpolated into the script body) so a recipient/message containing
# quotes or AppleScript metacharacters can neither break the script nor inject — the same
# untrusted-input discipline the rest of the machinery uses.
osascript - "$recipient" "$message" <<'APPLESCRIPT'
on run {targetRecipient, targetMessage}
	tell application "Messages"
		set targetService to 1st account whose service type = iMessage
		set targetBuddy to participant targetRecipient of targetService
		send targetMessage to targetBuddy
	end tell
end run
APPLESCRIPT
