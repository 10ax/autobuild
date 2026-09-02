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
loginctl enable-linger "$USER"          # run the user service without an active login
systemctl --user daemon-reload
systemctl --user enable --now autobuild.service
systemctl --user status autobuild.service --no-pager
