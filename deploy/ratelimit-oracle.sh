#!/usr/bin/env bash
# Rate-limit oracle: one cheap Haiku stream-json ping → capture the `rate_limit_event`
# → atomically write ~/.claude/usage-oracle.json in the shape autobuild/usage.py reads.
# Run on a ~7-minute systemd timer (see ratelimit-oracle.timer). The daemon's own builds
# also write this file; freshest wins. On any failure we DO NOT clobber the existing file.
set -u
command -v jq >/dev/null 2>&1 || exit 0
command -v claude >/dev/null 2>&1 || exit 0

DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
DST="$DIR/usage-oracle.json"
OUT="$(mktemp)"; trap 'rm -f "$OUT"' EXIT

# A trivial prompt on the cheapest model: the unified limit is model-weighted, so this
# barely moves the number it measures. stream-json (+ --verbose) emits the rate_limit_event.
timeout 60 claude -p 'ok' \
  --model claude-haiku-4-5-20251001 \
  --output-format stream-json --verbose > "$OUT" 2>/dev/null || exit 0

# Prefer the five_hour window; fall back to whatever rate_limit_event appeared.
info="$(jq -c 'select(.type=="rate_limit_event" and .rate_limit_info.rateLimitType=="five_hour") | .rate_limit_info' "$OUT" 2>/dev/null | tail -1)"
[ -z "$info" ] && info="$(jq -c 'select(.type=="rate_limit_event") | .rate_limit_info' "$OUT" 2>/dev/null | tail -1)"
[ -z "$info" ] && exit 0   # no signal this ping → leave the last good file untouched

status="$(printf '%s' "$info" | jq -r '.status // empty')"
reset="$(printf '%s' "$info" | jq -r '.resetsAt // empty')"
rltype="$(printf '%s' "$info" | jq -r '.rateLimitType // "five_hour"')"
[ -z "$status" ] && exit 0

now="$(date -u +%s)"
tmp="$DST.tmp.$$"
if jq -cn --arg s "$status" --argjson r "${reset:-null}" --arg t "$rltype" --argjson w "$now" \
      '{status:$s, reset_at:$r, rate_limit_type:$t, written_at:$w}' > "$tmp" 2>/dev/null; then
  mv -f "$tmp" "$DST"
else
  rm -f "$tmp"
fi
