#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$HOME/.config/systemd/user"

# Which units to install depends on the configured backend: the rate-limit oracle and the
# usage feed only exist to keep a live reading of the subscription's weekly quota, so a
# metered provider installs neither. Ask the code that owns that rule rather than repeating it.
PROVIDER="$(python3 -c "
import sys; sys.path.insert(0, '$HOME/autobuild')
from pathlib import Path
from autobuild.config import load_config
print(load_config(Path('$HOME/autobuild/config/runner.toml'), root=Path('$HOME/autobuild')).provider)
")"
UNITS="$(python3 -c "
import sys; sys.path.insert(0, '$HOME/autobuild')
from autobuild.deploy import units_for_provider
print(' '.join(units_for_provider('$PROVIDER')))
")"
echo "install: provider=$PROVIDER units=$UNITS"

cp "$HOME/autobuild/deploy/autobuild.service" "$HOME/.config/systemd/user/autobuild.service"
for u in $UNITS; do
  [ "$u" = "autobuild.service" ] && continue
  cp "$HOME/autobuild/deploy/$u" "$HOME/.config/systemd/user/$u"
done
# A provider switch must also RETIRE the units that no longer apply, or a stale timer keeps
# pinging the API for a signal nothing reads.
python3 -c "
import sys; sys.path.insert(0, '$HOME/autobuild')
from autobuild.deploy import units_for_provider, seat_units
keep = set(units_for_provider('$PROVIDER'))
for u in seat_units():
    if u not in keep:
        print(u)
" | while read -r stale; do
  [ -n "$stale" ] || continue
  echo "install: retiring $stale (not used by provider $PROVIDER)"
  systemctl --user disable --now "$stale" 2>/dev/null || true
  rm -f "$HOME/.config/systemd/user/$stale"
done
# The docs lane writes into repos outside ~/autobuild, and ProtectSystem=strict makes $HOME
# read-only outside ReadWritePaths — this drop-in adds the documented targets (additive).
DROPIN="$HOME/autobuild/deploy/autodoc-targets.conf"
if [ ! -f "$DROPIN" ] && [ -f "$HOME/autobuild/config/autodoc-targets.toml" ]; then
  python3 "$HOME/autobuild/bin/seed-autodoc-briefs.py" --systemd > "$DROPIN"
fi
if [ -f "$DROPIN" ]; then
  mkdir -p "$HOME/.config/systemd/user/autobuild.service.d"
  cp "$DROPIN" "$HOME/.config/systemd/user/autobuild.service.d/autodoc-targets.conf"
fi
# Same for the quality lane, which additionally builds each repo's toolchain inside the
# worktree (uv interpreters, pnpm store) and so needs those cache paths writable too.
QDROPIN="$HOME/autobuild/deploy/improve-targets.conf"
if [ ! -f "$QDROPIN" ] && [ -f "$HOME/autobuild/config/improve-targets.toml" ]; then
  python3 "$HOME/autobuild/bin/seed-improve-briefs.py" --systemd > "$QDROPIN"
fi
if [ -f "$QDROPIN" ]; then
  mkdir -p "$HOME/.config/systemd/user/autobuild.service.d"
  cp "$QDROPIN" "$HOME/.config/systemd/user/autobuild.service.d/improve-targets.conf"
fi
loginctl enable-linger "$USER"          # run the user service without an active login
systemctl --user daemon-reload
systemctl --user enable --now autobuild.service
systemctl --user status autobuild.service --no-pager
