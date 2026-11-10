#!/usr/bin/env bash
# Reproducible Athena gateway build (R5 F5 / R6): builds the committed HEAD of
# this checkout, labels the binary with the commit, and never overwrites one.
#
#   deploy/build-athena-gateway.sh v12
#     -> ~/.local/opt/athena-gateway-mvp/bin/skyeye-radio-v12-<shortsha>
#     -> <binary>.build.txt (build record: commit, whisper.cpp lib, sha256)
#
# Env: WHISPER_REPO = checkout holding the prebuilt third_party/whisper.cpp
#      (default: the main gateway repo); OUT_DIR = output directory.
# Deploying (pinning the binary in the launcher) is a separate, manual step.
set -euo pipefail
label=${1:?usage: $0 <label, e.g. v12>}
cd "$(git rev-parse --show-toplevel)"
# Untracked files count too: an untracked .go file would be compiled in.
if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
	echo "refusing to build: uncommitted or untracked files (build only committed code)" >&2
	git status --short --untracked-files=all >&2
	exit 1
fi
sha=$(git rev-parse --short HEAD)
G=${WHISPER_REPO:-$HOME/Ai-Workspace/projects/athena-audio-gateway}
W="$G/third_party/whisper.cpp"
[ -f "$W/build_go/src/libwhisper.a" ] || { echo "missing $W/build_go/src/libwhisper.a (run make whisper in $G)" >&2; exit 1; }
out="${OUT_DIR:-$HOME/.local/opt/athena-gateway-mvp/bin}/skyeye-radio-$label-$sha"
[ ! -e "$out" ] && [ ! -e "$out.build.txt" ] || { echo "refusing to overwrite $out" >&2; exit 1; }
mkdir -p "$(dirname "$out")"
CGO_ENABLED=1 GOARCH=amd64 GOFLAGS=-mod=mod GOPROXY=off GOTOOLCHAIN=local \
	C_INCLUDE_PATH="$W/ggml/include:$W/include" \
	LIBRARY_PATH="$W/build_go/src:$W/build_go/ggml/src" \
	go build -trimpath -tags nolibopusfile -ldflags "-X main.Version=$label-$sha" -o "$out" ./cmd/skyeye/
if ! go version -m "$out" | grep -q 'vcs.modified=false'; then
	echo "refusing: binary does not record vcs.modified=false; removing $out" >&2
	rm -f "$out"
	exit 1
fi
{
	echo "binary: $out"
	echo "version: $label-$sha"
	echo "go: $(go version)"
	go version -m "$out" | grep -E 'vcs\.(revision|time|modified)|-trimpath|-tags' | sed 's/^[[:space:]]*/build: /'
	# whisper.cpp is linked statically from $W; record which code that was.
	if git -C "$W" rev-parse --show-toplevel 2>/dev/null | grep -qx "$(cd "$W" && pwd -P)"; then
		echo "whisper.cpp: $(git -C "$W" rev-parse HEAD) ($(git -C "$W" describe --tags --always 2>/dev/null)); dirty files: $(git -C "$W" status --porcelain --untracked-files=no | wc -l)"
	else
		echo "whisper.cpp: not a git checkout ($W)"
	fi
	for lib in "$W/build_go/src/libwhisper.a" "$W"/build_go/ggml/src/libggml*.a; do
		[ -f "$lib" ] && echo "lib sha256: $(sha256sum "$lib" | cut -d' ' -f1)  ${lib#"$W"/}"
	done
	echo "sha256: $(sha256sum "$out" | cut -d' ' -f1)"
} > "$out.build.txt"
cat "$out.build.txt"
echo "built $out (version $label-$sha)"
