#!/usr/bin/env bash
set -euo pipefail
forge_project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
forge_runtime_dir="${FORGE_ROOTLESS_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/forge-rootless}"
# dockerd-rootless.sh expands its internal state-dir argument without quoting.
if [[ "$forge_runtime_dir" =~ [[:space:]] ]]; then
  printf 'Forge rootless runtime directory must not contain whitespace.\n' >&2
  exit 2
fi
mkdir -p -- "$forge_runtime_dir"
chmod 700 -- "$forge_runtime_dir"
systemd-run --user --unit=forge-rootless --collect --property=Delegate=yes \
  --setenv="XDG_RUNTIME_DIR=$forge_runtime_dir" \
  /usr/bin/dockerd-rootless.sh \
  "--host=unix://$forge_runtime_dir/docker.sock" \
  "--data-root=$forge_project_dir/.forge/docker" \
  "--exec-root=$forge_runtime_dir/exec" \
  "--pidfile=$forge_runtime_dir/dockerd.pid" --iptables=false
printf 'Forge Docker endpoint: unix://%s/docker.sock\n' "$forge_runtime_dir"
