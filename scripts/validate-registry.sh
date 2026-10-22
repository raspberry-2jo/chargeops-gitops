#!/bin/sh
set -eu

# Run from the repository root. Accept an already rendered manifest in CI.
if [ "$#" -gt 1 ]; then
  echo "Usage: sh scripts/validate-registry.sh [rendered-manifest]" >&2
  exit 1
fi
rendered="${1:-artifacts/chargeops-dev.yaml}"
if [ "$#" -eq 0 ]; then
  mkdir -p artifacts
  kubectl kustomize chargeops-chatops/overlays/dev > "$rendered"
fi
test -s "$rendered"

awk '
  $1 == "image:" {
    count++
    ref = $2
    gsub(/["\047\r]/, "", ref)
    if (ref != "registry.chargeops.lab:5000/chargeops/collector:0.2" &&
        ref !~ /^registry[.]chargeops[.]lab:5000\/chargeops\/[a-z0-9][a-z0-9._-]*(:dev|:dev@sha256:[a-f0-9]+|@sha256:[a-f0-9]+)$/) {
      print "Unexpected container image: " ref > "/dev/stderr"
      failed = 1
    }
    if (ref ~ /[-]v[0-9]+[-]/) {
      print "Version suffix is not allowed in the app image name: " ref > "/dev/stderr"
      failed = 1
    }
    if (ref ~ /@sha256:/) {
      digest = ref
      sub(/^.*@sha256:/, "", digest)
      if (length(digest) != 64) {
        print "Invalid SHA-256 digest: " ref > "/dev/stderr"
        failed = 1
      }
    }
  }
  END {
    if (count == 0) {
      print "No container images found" > "/dev/stderr"
      exit 1
    }
    if (failed) exit 1
    print "Registry validation passed for " count " container image references."
  }
' "$rendered"
