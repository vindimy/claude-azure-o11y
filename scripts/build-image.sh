#!/usr/bin/env bash
# Manual counterpart of the GitLab CI `build` job: build the function image from the approved base and
# push it to ACR, optionally followed by scripts/deploy.sh with the new tag.
#
# Usage: build-image.sh [--param-file deploy.env] [--acr-name <acr>] [--image-name <name>] [--tag <tag>]
#                       [--builder docker|acr] [--no-push] [--allow-dirty] [--force] [--deploy]
#
#   --param-file   read ACR_NAME / IMAGE_NAME from the deploy.sh parameter file (flags win)
#   --builder      docker (default): local Docker daemon, linux/amd64
#                  acr: `az acr build` in the registry; no local Docker needed
#   --tag          default: full commit SHA, same as CI. A dirty tree needs --allow-dirty and gets
#                  dev-<short sha>-<UTC timestamp> so a SHA tag always means that exact commit
#   --force        overwrite a tag that already exists in the ACR (tags are meant to be immutable)
#   --deploy       after the push, run deploy.sh --param-file <file> --image-tag <tag>
#
# The last line of output is IMAGE_TAG=<tag>.
set -euo pipefail

PARAM_FILE=""; TAG=""; BUILDER=docker; PUSH=true; ALLOW_DIRTY=false; FORCE=false; DEPLOY=false
ARG_ACR_NAME=""; ARG_IMAGE_NAME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --param-file) PARAM_FILE="$2"; shift 2 ;;
    --acr-name) ARG_ACR_NAME="$2"; shift 2 ;;
    --image-name) ARG_IMAGE_NAME="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --builder) BUILDER="$2"; shift 2 ;;
    --no-push) PUSH=false; shift ;;
    --allow-dirty) ALLOW_DIRTY=true; shift ;;
    --force) FORCE=true; shift ;;
    --deploy) DEPLOY=true; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Resolve the param file before moving to the repo root, so relative paths keep working.
[[ -z "$PARAM_FILE" ]] || PARAM_FILE="$(cd "$(dirname "$PARAM_FILE")" && pwd)/$(basename "$PARAM_FILE")"
cd "$(dirname "$0")/.."
# shellcheck source-path=SCRIPTDIR source=lib/params.sh
source scripts/lib/params.sh
if [[ -n "$PARAM_FILE" ]]; then load_params "$PARAM_FILE"; fi
ACR_NAME=${ARG_ACR_NAME:-${ACR_NAME:-}}
IMAGE_NAME=${ARG_IMAGE_NAME:-${IMAGE_NAME:-o11y-alerting}}

fail() { echo "$*" >&2; exit 2; }
log() { echo "==> $*" >&2; }
[[ -n "$ACR_NAME" ]] || fail "--acr-name (or ACR_NAME in --param-file) is required"
[[ "$BUILDER" == docker || "$BUILDER" == acr ]] || fail "--builder must be docker or acr"
[[ "$PUSH" == true || "$BUILDER" == docker ]] || fail "--no-push only applies to --builder docker"
[[ "$DEPLOY" != true || -n "$PARAM_FILE" ]] || fail "--deploy needs --param-file"
[[ "$DEPLOY" != true || "$PUSH" == true ]] || fail "--deploy needs a pushed image; drop --no-push"

BASE_IMAGE=$(grep -v '^#' build/base-image.txt | tr -d '[:space:]')
[[ -n "$BASE_IMAGE" && "$BASE_IMAGE" != *"<"* ]] \
  || fail "build/base-image.txt still holds a placeholder; set the approved base image first"

DIRTY=false
[[ -z "$(git status --porcelain)" ]] || DIRTY=true
if [[ -z "$TAG" ]]; then
  if [[ "$DIRTY" == true ]]; then
    [[ "$ALLOW_DIRTY" == true ]] || fail "working tree has uncommitted changes; commit, or pass --allow-dirty for a dev tag"
    TAG="dev-$(git rev-parse --short HEAD)-$(date -u +%Y%m%d%H%M%S)"
  else
    TAG=$(git rev-parse HEAD)
  fi
fi

LOGIN_SERVER=$(az acr show -n "$ACR_NAME" --query loginServer -o tsv)
REF="$LOGIN_SERVER/$IMAGE_NAME:$TAG"
if [[ "$PUSH" == true && "$FORCE" != true ]] \
  && az acr repository show-tags -n "$ACR_NAME" --repository "$IMAGE_NAME" -o tsv 2>/dev/null | grep -qx "$TAG"; then
  fail "$REF already exists; tags are immutable (use --force to overwrite, or a new commit)"
fi

# Functions on Linux run amd64; building on Apple Silicon without --platform gives an image that won't start.
if [[ "$BUILDER" == acr ]]; then
  log "Building $REF in ACR $ACR_NAME (az acr build)"
  az acr build -r "$ACR_NAME" -t "$IMAGE_NAME:$TAG" --platform linux/amd64 \
    --build-arg BASE_IMAGE="$BASE_IMAGE" . >&2
else
  log "Building $REF locally (base $BASE_IMAGE)"
  docker build --platform linux/amd64 --build-arg BASE_IMAGE="$BASE_IMAGE" -t "$REF" . >&2
  if [[ "$PUSH" == true ]]; then
    log "Pushing $REF"
    az acr login -n "$ACR_NAME" >&2
    docker push "$REF" >&2
  fi
fi

if [[ "$DEPLOY" == true ]]; then
  log "Deploying $REF with scripts/deploy.sh"
  scripts/deploy.sh --param-file "$PARAM_FILE" --image-tag "$TAG" >&2
fi
echo "IMAGE_TAG=$TAG"
