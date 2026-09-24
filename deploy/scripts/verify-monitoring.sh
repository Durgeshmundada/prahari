#!/usr/bin/env bash
# Validate alert syntax and simulations with a locally approved promtool binary.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
monitoring_dir="$(cd -- "$script_dir/../monitoring" && pwd)"
promtool_binary="${1:-promtool}"
if [[ "$promtool_binary" == */* ]]; then
  promtool_binary="$(realpath -- "$promtool_binary")"
else
  promtool_binary="$(command -v "$promtool_binary")"
fi
"$promtool_binary" check rules "$monitoring_dir/prahari.rules.yaml"
cd -- "$monitoring_dir"
"$promtool_binary" test rules prahari.rules.test.yaml
