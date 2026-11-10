#!/usr/bin/env bash
# Reproducible Athena gateway build (R5 F5 / R6): builds the committed HEAD of
# this checkout, labels the binary with the commit, and never overwrites one.
#
#   deploy/build-athena-gateway.sh v12
#     -> ~/.local/opt/athena-gateway-mvp/bin/skyeye-radio-v12-<shortsha>
#
# Env: WHISPER_REPO = checkout holding the prebuilt third_party/whisper.cpp
#      (default: the main gateway repo); OUT_DIR = output directory.
# Deploying (pinning the binary in the launcher) is a separate, manual step.
set -euo pipefail

label=${1:?usage: $0 <label, e.g. v12>}
cd "$(git rev-parse --show-toplevel)"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
	echo "refusing to build: uncommitted changes (build only committed code)" >&2
	exit 1
fi
sha=$(git rev-parse --short HEAD)
G=${WHISPER_REPO:-$HOME/Ai-Workspace/projects/athena-audio-gateway}
W="$G/third_party/whisper.cpp"
[ -f "$W/build_go/src/libwhisper.a" ] || { echo "missing $W/build_go/src/libwhisper.a (run make whisper in $G)" >&2; exit 1; }
out="${OUT_DIR:-$HOME/.local/opt/athena-gateway-mvp/bin}/skyeye-radio-$label-$sha"
[ ! -e "$out" ] || { echo "refusing to overwrite $out" >&2; exit 1; }
mkdir -p "$(dirname "$out")"

CGO_ENABLED=1 GOARCH=amd64 GOFLAGS=-mod=mod GOPROXY=off GOTOOLCHAIN=local \
	C_INCLUDE_PATH="$W/ggml/include:$W/include" \
	LIBRARY_PATH="$W/build_go/src:$W/build_go/ggml/src" \
	go build -tags nolibopusfile -ldflags "-X main.Version=$label-$sha" -o "$out" ./cmd/skyeye/

go version -m "$out" | grep -E 'vcs\.(revision|modified)'
sha256sum "$out"
echo "built $out (version $label-$sha)"
