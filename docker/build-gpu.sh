#!/usr/bin/env bash
#
# Convenience wrapper: build the GPU variant of the SiGMA image.
# Image size ~7.5GB (includes nvidia-* CUDA libs + triton).
#
# Requirements (not enforced by this script):
#   - NVIDIA GPU + driver on host
#   - NVIDIA Container Toolkit (nvidia-docker2)
#   - Run container with --gpus all
#
# All additional flags are forwarded to build.sh, e.g.:
#   ./docker/build-gpu.sh --no-cache
#
set -euo pipefail
exec "$(dirname "$0")/build.sh" --variant gpu "$@"
