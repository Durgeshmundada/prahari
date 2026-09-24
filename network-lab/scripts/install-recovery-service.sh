#!/usr/bin/env bash
# Install the checked-in recovery unit for this exact workspace path.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/../.." && pwd)"
template="$project_root/deploy/systemd/prahari-lab-recovery.service.in"
target="/etc/systemd/system/prahari-lab-recovery.service"
[[ "$(id -u)" -eq 0 ]] || { printf 'ERROR: run this installer as root\n' >&2; exit 1; }
[[ "$project_root" != *$'\n'* && "$project_root" != *'@'* ]] || { printf 'ERROR: unsupported project path\n' >&2; exit 1; }
escaped_root="${project_root//\/\\}"
escaped_root="${escaped_root//&/\\&}"
sed "s|@PROJECT_ROOT@|$escaped_root|g" "$template" > "$target"
chmod 0644 "$target"
systemctl daemon-reload
systemctl enable prahari-lab-recovery.service
printf 'Installed %s for %s\n' "$target" "$project_root"
