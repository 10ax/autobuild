#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$HOME/.config/systemd/user"
cp "$HOME/autobuild/deploy/autobuild.service" "$HOME/.config/systemd/user/autobuild.service"
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
