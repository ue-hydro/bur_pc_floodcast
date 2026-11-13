#!/usr/bin/env bash

set -Eeuo pipefail

: "${FLUXOS_REPOSITORY:?FLUXOS_REPOSITORY is required}"
: "${FLUXOS_REVISION:?FLUXOS_REVISION is required}"
: "${BASE_IMAGE:?BASE_IMAGE is required}"
: "${APT_SNAPSHOT:?APT_SNAPSHOT is required}"

[[ "$FLUXOS_REPOSITORY" == https://github.com/ue-hydro/FLUXOS_cpp.git ]] || { echo 'Unreviewed FLUXOS source URL' >&2; exit 2; }
[[ "$FLUXOS_REVISION" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected a full Git commit' >&2; exit 2; }
[[ "${BUILD_JOBS:-1}" =~ ^[1-4]$ ]] || { echo 'BUILD_JOBS must be 1..4' >&2; exit 2; }
[[ "$BASE_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]] || { echo 'Base image must use a digest' >&2; exit 2; }

source_dir=/tmp/fluxos-source
build_dir=/tmp/fluxos-build
share_dir=/opt/floodcast/share/hydraulics

mkdir -p "$source_dir" "$build_dir" "$share_dir" /opt/floodcast/bin

git -C "$source_dir" init --quiet
git -C "$source_dir" remote add origin "$FLUXOS_REPOSITORY"

timeout 300 git -C "$source_dir" fetch --quiet --depth 1 origin "$FLUXOS_REVISION"

git -C "$source_dir" checkout --quiet --detach FETCH_HEAD
actual_revision="$(git -C "$source_dir" rev-parse HEAD)"
[[ "$actual_revision" == "$FLUXOS_REVISION" ]] || { echo 'Source revision mismatch' >&2; exit 3; }
# Retain corresponding source and changes; do not ship upstream scientific example data.
tar -C "$source_dir" -czf "$share_dir/upstream-source.tar.gz" CMakeLists.txt src
python3 /opt/floodcast/recipe/patch_source.py "$source_dir" "$share_dir/patches.json"
git -C "$source_dir" add --intent-to-add src/fluxos/floodcast_safety.h
git -C "$source_dir" diff --binary > "$share_dir/source.patch"
cmake -S "$source_dir" -B "$build_dir" \
  -DMODE_release=ON -DUSE_MPI=OFF -DUSE_CUDA=OFF \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
  -DCMAKE_RUNTIME_OUTPUT_DIRECTORY=/opt/floodcast/bin

if grep -Eq -- '-m(arch|tune)=native|-flto=auto' "$build_dir/compile_commands.json"; then
  echo 'Host-specific compilation flags remain' >&2; exit 4

fi
cmake --build "$build_dir" --parallel "$BUILD_JOBS"
test -x /opt/floodcast/bin/fluxos
ldd /opt/floodcast/bin/fluxos > "$share_dir/ldd.txt"
if grep -q 'not found' "$share_dir/ldd.txt"; then echo 'Missing runtime libraries' >&2; exit 5; fi

if grep -Eiq 'libmpi|libcuda|libcudart' "$share_dir/ldd.txt"; then echo 'Unexpected MPI/CUDA linkage' >&2; exit 5; fi
grep -q 'libgomp' "$share_dir/ldd.txt"
cp "$build_dir/CMakeCache.txt" "$build_dir/compile_commands.json" "$share_dir/"
cp /usr/share/common-licenses/GPL-3 "$share_dir/GPL-3"

python3 /opt/floodcast/recipe/toolchain.py "$share_dir"

# Only image-build scratch directories created above; no host bind mounts exist here.
rm -rf /tmp/fluxos-source /tmp/fluxos-build

