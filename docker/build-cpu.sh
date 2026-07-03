#!/usr/bin/env bash
#
# Convenience wrapper: build the CPU variant of the SiGMA image.
# Image size ~3.8GB. No NVIDIA GPU required.
#
# All additional flags are forwarded to build.sh, e.g.:
#   ./docker/build-cpu.sh --no-cache
#   ./docker/build-cpu.sh --texlive-mirror https://mirrors.tuna.tsinghua.edu.cn/CTAN/systems/texlive/tlnet
#
set -euo pipefail
exec "$(dirname "$0")/build.sh" --variant cpu "$@"
