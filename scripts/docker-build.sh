#!/usr/bin/env bash

set -euo pipefail
#DOCKER_REPOSITORY="${DOCKER_REPOSITORY:-quay.io/unstructured-io/unstructured-api}"
PIPELINE_PACKAGE=${PIPELINE_PACKAGE:-"general"}
#PIPELINE_FAMILY=${PIPELINE_FAMILY:-"general"}
#PIP_VERSION="${PIP_VERSION:-22.2.1}"
#DOCKER_IMAGE="${DOCKER_IMAGE:-pipeline-family-${PIPELINE_FAMILY}-dev}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-}"

GIT_COMMIT=$(git rev-parse HEAD)

DOCKER_REPOSITORY=ghcr.io/orq-ai/unstructured-api
DOCKER_IMAGE=ghcr.io/orq-ai/unstructured-api:${GIT_COMMIT}
DOCKER_BUILD_REPOSITORY=ghcr.io/orq-ai/unstructured-api
PACKAGE="unstructured-api"
PIPELINE_FAMILY="general"
PIP_VERSION="22.2.1"
PYTHON_VERSION="3.10"

DOCKER_BUILD_CMD=(
  docker buildx build --load -f Dockerfile
  --build-arg PIP_VERSION="$PIP_VERSION"
  --build-arg BUILDKIT_INLINE_CACHE=1
  --build-arg PIPELINE_PACKAGE="$PIPELINE_PACKAGE"
  --platform linux/amd64
  -t "$DOCKER_IMAGE"
  .
)

# only build for specific platform if DOCKER_PLATFORM is set
if [ -n "${DOCKER_PLATFORM:-}" ]; then
  DOCKER_BUILD_CMD+=("--platform=$DOCKER_PLATFORM")
fi

DOCKER_BUILDKIT=1 "${DOCKER_BUILD_CMD[@]}"
