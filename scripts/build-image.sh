#!/usr/bin/env bash
# Dev-only: build the image locally from the approved base and push a dev tag to ACR.
# Usage: build-image.sh --acr-name <acr> [--tag <tag>] [--image-name <name>] [--push]
set -euo pipefail
ACR_NAME=""; TAG="dev-$(git rev-parse --short HEAD)"; PUSH=false; IMAGE_NAME=o11y-alerting
while [[ $# -gt 0 ]]; do
  case "$1" in
    --acr-name) ACR_NAME="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --image-name) IMAGE_NAME="$2"; shift 2 ;;
    --push) PUSH=true; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$ACR_NAME" ]] || { echo "--acr-name is required" >&2; exit 2; }
cd "$(dirname "$0")/.."
BASE_IMAGE=$(grep -v '^#' build/base-image.txt | tr -d '[:space:]')
LOGIN_SERVER=$(az acr show -n "$ACR_NAME" --query loginServer -o tsv)
REF="$LOGIN_SERVER/$IMAGE_NAME:$TAG"
docker build --build-arg BASE_IMAGE="$BASE_IMAGE" -t "$REF" .
if [[ "$PUSH" == true ]]; then
  az acr login -n "$ACR_NAME"
  docker push "$REF"
fi
echo "IMAGE_TAG=$TAG"
