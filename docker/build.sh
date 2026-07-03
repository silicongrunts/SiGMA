#!/usr/bin/env bash
#
# Build the SiGMA Docker image.
#
# Usage:
#   ./docker/build.sh                              # default: CPU torch, tag sigma:local-cpu
#   ./docker/build.sh --variant gpu                # GPU torch (needs NVIDIA Container Toolkit), tag sigma:local-gpu
#   ./docker/build.sh --tag sigma:v0.1             # custom tag (suffix -cpu/-gpu still appended)
#   ./docker/build.sh --no-cache                   # bypass Docker layer cache
#   ./docker/build.sh --prune                      # prune dangling Docker layers before building
#   ./docker/build.sh --texlive-mirror URL         # use a faster CTAN mirror
#   ./docker/build.sh --texlive-year 2026          # pin TeX Live year
#
# Common CTAN mirrors:
#   https://mirror.ctan.org/systems/texlive/tlnet                (default, auto-redirect)
#   https://mirrors.tuna.tsinghua.edu.cn/CTAN/systems/texlive/tlnet   (Tsinghua, fast in CN)
#   https://mirrors.ircam.fr/pub/CTAN/systems/texlive/tlnet      (IRCAM, EU)
#
# GPU variant requirements (when --variant gpu):
#   - NVIDIA GPU + driver on host
#   - NVIDIA Container Toolkit installed (https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)
#   - Run with: docker run --gpus all ...
#
set -euo pipefail

# Always operate from the repository root, regardless of where the script is
# invoked from.
cd "$(dirname "$0")/.."

VARIANT="cpu"
TAG_BASE="sigma:local"
NO_CACHE=""
PRUNE="0"
BUILD_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant)
      VARIANT="$2"; shift 2 ;;
    --tag)
      TAG_BASE="$2"; shift 2 ;;
    --no-cache)
      NO_CACHE="--no-cache"; shift ;;
    --prune)
      PRUNE="1"; shift ;;
    --texlive-mirror)
      BUILD_ARGS+=(--build-arg "TEXLIVE_REPOSITORY=$2"); shift 2 ;;
    --texlive-year)
      BUILD_ARGS+=(--build-arg "TEXLIVE_YEAR=$2"); shift 2 ;;
    -h|--help)
      sed -n '2,28p' "$0"
      exit 0 ;;
    *)
      echo "[build] unknown option: $1" >&2
      exit 1 ;;
  esac
done

# Validate variant.
case "${VARIANT}" in
  cpu|gpu) ;;
  *) echo "[build] --variant must be 'cpu' or 'gpu', got: ${VARIANT}" >&2; exit 1 ;;
esac

# Tag suffix keeps CPU and GPU images distinguishable; skip suffix only when
# the user explicitly set --tag to something custom.
if [[ "${TAG_BASE}" == sigma:local ]]; then
  TAG="${TAG_BASE}-${VARIANT}"
else
  TAG="${TAG_BASE}"
fi

BUILD_ARGS+=(--build-arg "TORCH_VARIANT=${VARIANT}")

echo "[build] context:  $(pwd)"
echo "[build] tag:      ${TAG}"
echo "[build] variant:  ${VARIANT}"
[ -n "${NO_CACHE}" ]          && echo "[build] cache:    disabled"
[ "${PRUNE}" = "1" ]          && echo "[build] prune:    dangling images before build"
[ "${#BUILD_ARGS[@]}" -gt 0 ] && printf '[build] args:     %s\n' "${BUILD_ARGS[@]}"
echo ""

if [ "${PRUNE}" = "1" ]; then
  docker image prune -f --filter "dangling=true" >/dev/null
fi

# Build.
docker build \
  -t "${TAG}" \
  ${NO_CACHE} \
  "${BUILD_ARGS[@]}" \
  .

# Report.
echo ""
echo "[build] done"
docker images "${TAG}" --format "  {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}"
echo ""
if [ "${VARIANT}" = "gpu" ]; then
  echo "  run it:    docker compose -f docker-compose.gpu.yml up -d"
  echo "  or:        docker run --gpus all -d -p 3000:3000 -v \"\$(pwd)/userdata:/app/userdata\" ${TAG}"
else
  echo "  run it:    docker compose up -d"
  echo "  or:        docker run -d -p 3000:3000 -v \"\$(pwd)/userdata:/app/userdata\" ${TAG}"
fi
