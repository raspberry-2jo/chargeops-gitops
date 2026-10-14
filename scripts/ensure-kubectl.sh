#!/bin/sh
set -eu

if command -v kubectl >/dev/null 2>&1; then
  kubectl version --client
  exit 0
fi

version="${KUBECTL_VERSION:-v1.36.1}"
if ! printf '%s\n' "$version" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo 'KUBECTL_VERSION must be a release version such as v1.36.1.' >&2
  exit 1
fi

if [ "$(uname -s)" != Linux ]; then
  echo 'Automatic kubectl installation requires a Linux agent.' >&2
  exit 1
fi
case "$(uname -m)" in
  x86_64) arch=amd64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo 'Unsupported agent architecture. Install kubectl on the agent.' >&2; exit 1 ;;
esac

for tool in curl sha256sum; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "kubectl is missing; automatic installation also requires $tool on the agent." >&2
    exit 1
  fi
done

mkdir -p .ci-bin
download_dir="$(mktemp -d .ci-bin/download.XXXXXX)"
trap 'rm -rf -- "$download_dir"' EXIT HUP INT TERM
url="https://dl.k8s.io/release/$version/bin/linux/$arch/kubectl"
curl --fail --silent --show-error --location --retry 3 \
  --connect-timeout 15 --max-time 180 "$url" -o "$download_dir/kubectl"
curl --fail --silent --show-error --location --retry 3 \
  --connect-timeout 15 --max-time 60 "$url.sha256" -o "$download_dir/kubectl.sha256"
(
  cd "$download_dir"
  printf '%s  kubectl\n' "$(cat kubectl.sha256)" | sha256sum --check -
)
chmod 0755 "$download_dir/kubectl"
mv "$download_dir/kubectl" .ci-bin/kubectl
.ci-bin/kubectl version --client
