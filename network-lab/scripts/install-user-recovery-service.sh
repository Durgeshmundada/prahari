#!/usr/bin/env bash
# Install a no-root user timer that repairs the lab after Docker/WSL restarts.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/../.." && pwd)"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p -- "$unit_dir"
[[ "$project_root" != *$'\n'* && "$project_root" != *'@'* ]] || { printf 'ERROR: unsupported project path\n' >&2; exit 1; }
escaped_root="${project_root//\/\\}"
escaped_root="${escaped_root//&/\\&}"
sed "s|@PROJECT_ROOT@|$escaped_root|g" \
  "$project_root/deploy/systemd/prahari-lab-recovery-user.service.in" \
  > "$unit_dir/prahari-lab-recovery.service"
install -m 0644 "$project_root/deploy/systemd/prahari-lab-recovery-user.timer" \
  "$unit_dir/prahari-lab-recovery.timer"
sed "s|@PROJECT_ROOT@|$escaped_root|g" \
  "$project_root/deploy/systemd/prahari-router-collector.service.in" \
  > "$unit_dir/prahari-router-collector.service"
install -m 0644 "$project_root/deploy/systemd/prahari-router-collector.timer" \
  "$unit_dir/prahari-router-collector.timer"
sed "s|@PROJECT_ROOT@|$escaped_root|g" \
  "$project_root/deploy/systemd/prahari-path-collector.service.in" \
  > "$unit_dir/prahari-path-collector.service"
install -m 0644 "$project_root/deploy/systemd/prahari-path-collector.timer" \
  "$unit_dir/prahari-path-collector.timer"
sed "s|@PROJECT_ROOT@|$escaped_root|g" \
  "$project_root/deploy/systemd/prahari-action-agent.service.in" \
  > "$unit_dir/prahari-action-agent.service"
systemctl --user daemon-reload
systemctl --user enable --now prahari-lab-recovery.timer
systemctl --user enable --now prahari-router-collector.timer
systemctl --user enable --now prahari-path-collector.timer
systemctl --user enable --now prahari-action-agent.service
systemctl --user start prahari-lab-recovery.service
printf 'Installed and started the PRAHARI user recovery timer.\n'
