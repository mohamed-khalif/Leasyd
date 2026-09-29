#!/usr/bin/env bash
# Synthetic checks (S1): HTTP and browser checks.
#  1. obs-phaseS1-build: the ECR repository and CodeBuild project for the browser image.
#  2. The browser image (services/synthetics/Dockerfile), built in CodeBuild unless an image of the
#     same source is already in ECR. No Docker needed here.
#  3. obs-phaseS1: the API, scheduler, runner and browser functions (bundle: synthetics + ingest's
#     OTLP-to-Firehose code).
# Needs obs-phase0 redeployed first (its boundary allows the checks' KMS key and the image build).
# Deploy it before obs-phaseT2, whose /v1/app/checks routes invoke obs-synthetics-api.
# Extra arguments go to `cloudformation deploy` (of obs-phaseS1).
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ARTIFACTS="obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}"
out() { aws cloudformation describe-stacks --stack-name "$1" --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text; }

# 1. Where the browser image is built and kept.
aws cloudformation deploy --stack-name obs-phaseS1-build --template-file infra/phaseS1-build.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=S1 --no-fail-on-empty-changeset
REPO="$(out obs-phaseS1-build RepositoryUri)"
PROJECT="$(out obs-phaseS1-build Project)"

# 2. The image, tagged by its source: rebuilt only when the source changes.
SRC=(services/synthetics/Dockerfile services/synthetics/.dockerignore services/synthetics/safety.py services/synthetics/browser.py)
TAG="src-$(cat "${SRC[@]}" | sha256sum | cut -c1-16)"
if ! aws ecr describe-images --repository-name obs-synthetics-browser --image-ids imageTag="$TAG" >/dev/null 2>&1; then
  ZIP="$(mktemp -d)/browser.zip"
  (cd services/synthetics && zip -q "$ZIP" Dockerfile .dockerignore safety.py browser.py)
  aws s3 cp --quiet "$ZIP" "s3://${ARTIFACTS}/builds/browser-${TAG}.zip"
  BUILD="$(aws codebuild start-build --project-name "$PROJECT" \
    --source-location-override "${ARTIFACTS}/builds/browser-${TAG}.zip" \
    --environment-variables-override "name=IMAGE_TAG,value=${TAG},type=PLAINTEXT" \
    --query build.id --output text)"
  echo "Building the browser image ${TAG} (about 5 minutes): ${BUILD}"
  while true; do
    STATUS="$(aws codebuild batch-get-builds --ids "$BUILD" --query 'builds[0].buildStatus' --output text)"
    [[ "$STATUS" != IN_PROGRESS ]] && break
    sleep 15
  done
  if [[ "$STATUS" != SUCCEEDED ]]; then
    echo "The image build ended ${STATUS}. Its log: CloudWatch Logs group /aws/codebuild/obs-synthetics-browser" >&2
    exit 1
  fi
fi
DIGEST="$(aws ecr describe-images --repository-name obs-synthetics-browser --image-ids imageTag="$TAG" \
  --query 'imageDetails[0].imageDigest' --output text)"
IMAGE="${REPO}@${DIGEST}"
echo "Browser image: ${TAG} (${IMAGE})"

# Screenshots expire after 30 days (the data bucket's rules).
infra/data-bucket-rules.sh

# 3. The functions.
rm -rf services/synthetics/build && mkdir -p services/synthetics/build
pip install -q --target services/synthetics/build --only-binary=:all: --implementation cp --python-version 3.12 \
  --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 \
  -r services/ingest/requirements.txt -r services/synthetics/requirements.txt
cp services/synthetics/synthetics.py services/synthetics/safety.py services/ingest/ingest.py services/synthetics/build/
aws cloudformation package \
  --template-file infra/phaseS1-synthetics.yaml \
  --s3-bucket "$ARTIFACTS" --s3-prefix phaseS1 \
  --output-template-file infra/phaseS1-synthetics.packaged.yaml
aws cloudformation deploy --stack-name obs-phaseS1 \
  --template-file infra/phaseS1-synthetics.packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=S1 \
  --parameter-overrides "BrowserImageUri=${IMAGE}" "$@"
