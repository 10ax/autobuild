#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$HOME/.config/systemd/user"
cp "$HOME/autobuild/deploy/autobuild.service" "$HOME/.config/systemd/user/autobuild.service"
loginctl enable-linger "$USER"          # run the user service without an active login
systemctl --user daemon-reload
systemctl --user enable --now autobuild.service
systemctl --user status autobuild.service --no-pager
