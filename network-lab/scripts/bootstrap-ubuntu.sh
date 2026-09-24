#!/usr/bin/env bash
# Installs the host tooling required by the isolated PRAHARI Containerlab lab.
# Run as root on Ubuntu 24.04. It does not deploy the lab or inject faults.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  printf 'ERROR: run this script as root (sudo bash %s)\n' "$0" >&2
  exit 1
fi

if [[ "$(. /etc/os-release && printf '%s' "$ID")" != "ubuntu" ]]; then
  printf 'ERROR: this bootstrap currently supports Ubuntu only\n' >&2
  exit 1
fi

lab_user="${SUDO_USER:-hp}"
if ! id "$lab_user" >/dev/null 2>&1; then
  printf 'ERROR: target lab user does not exist: %s\n' "$lab_user" >&2
  exit 1
fi

apt-get update
apt-get install -y ca-certificates curl iproute2 jq

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

architecture="$(dpkg --print-architecture)"
ubuntu_codename="$(. /etc/os-release && printf '%s' "${UBUNTU_CODENAME:-$VERSION_CODENAME}")"
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $ubuntu_codename
Components: stable
Architectures: $architecture
Signed-By: /etc/apt/keyrings/docker.asc
EOF

apt-get update
apt-get install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin
systemctl enable --now docker
usermod -aG docker "$lab_user"

# Containerlab's official Debian repository currently documents trusted=yes.
# Keep it isolated in its own source file so it can be replaced with a signed
# repository without changing other apt trust configuration.
printf '%s\n' 'deb [trusted=yes] https://netdevops.fury.site/apt/ /' \
  > /etc/apt/sources.list.d/netdevops.list
apt-get update
apt-get install -y containerlab
usermod -aG clab_admins "$lab_user"

modprobe sch_netem
printf '%s\n' sch_netem > /etc/modules-load.d/prahari-netem.conf

printf '%s\n' 'PRAHARI lab host tools installed.'
docker version --format 'Docker Server {{.Server.Version}}'
docker compose version
containerlab version
printf 'User %s was added to the docker and clab_admins groups. Start a new WSL session before deploying.\n' \
  "$lab_user"
