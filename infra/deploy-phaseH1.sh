#!/usr/bin/env bash
# The hot store (docs/hot-store.md):
#  1. obs-phaseH1 without a task: VPC, endpoints, the obs-hot stream, ECR and CodeBuild.
#  2. The image (services/hot/Dockerfile + server.py + the query modules it reuses), built in
#     CodeBuild unless an image of the same source is already in ECR. No Docker needed here.
#  3. obs-phaseH1 again with the image: the Fargate task, found at hot.obs.internal:8080.
# Needs obs-phase0 redeployed first. Then redeploy obs-phase4 (it reads this stack's outputs) and
# obs-phaseT2 with HotStream=obs-hot HotAccounts=<accounts>.
# Extra arguments go to the last `cloudformation deploy` (e.g. DesiredCount=0 to stop the task).
set -euo pipefail
cd "$(dirname "$0")/.."
: "${AWS_DEFAULT_REGION:?set AWS_DEFAULT_REGION}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ARTIFACTS="obs-artifacts-${ACCOUNT}-${AWS_DEFAULT_REGION}"
out() { aws cloudformation describe-stacks --stack-name "$1" --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text; }
deploy() {
  aws cloudformation deploy --stack-name obs-phaseH1 --template-file infra/phaseH1-hot.yaml \
    --capabilities CAPABILITY_NAMED_IAM --tags project=obs phase=H1 --no-fail-on-empty-changeset "$@"
}

# 1. Everything but the task (keeps the image already running, if any).
if aws cloudformation describe-stacks --stack-name obs-phaseH1 >/dev/null 2>&1; then
  deploy --parameter-overrides ImageUri="$(aws cloudformation describe-stacks --stack-name obs-phaseH1 \
    --query "Stacks[0].Parameters[?ParameterKey=='ImageUri'].ParameterValue" --output text)"
else
  deploy
fi
PROJECT="$(out obs-phaseH1 Project)"
REPO="$(out obs-phaseH1 RepositoryUri)"

# 2. The image, tagged by its source.
CTX="$(mktemp -d)"
cp services/hot/Dockerfile services/hot/requirements.txt services/hot/server.py "$CTX/"
cp services/compaction/{hotstore,compact,bloom,dayfilter,lookup,layout,query,promql,sqlscan}.py services/shared/limits.py "$CTX/"
TAG="src-$(cd "$CTX" && cat $(ls | sort) | sha256sum | cut -c1-16)"
if ! aws ecr describe-images --repository-name obs-hot --image-ids imageTag="$TAG" >/dev/null 2>&1; then
  ZIP="$(mktemp -d)/hot.zip"
  (cd "$CTX" && zip -q "$ZIP" ./*)
  aws s3 cp --quiet "$ZIP" "s3://${ARTIFACTS}/builds/hot-${TAG}.zip"
  BUILD="$(aws codebuild start-build --project-name "$PROJECT" \
    --source-location-override "${ARTIFACTS}/builds/hot-${TAG}.zip" \
    --environment-variables-override "name=IMAGE_TAG,value=${TAG},type=PLAINTEXT" \
    --query build.id --output text)"
  echo "Building the hot store image ${TAG} (about 3 minutes): ${BUILD}"
  while true; do
    STATUS="$(aws codebuild batch-get-builds --ids "$BUILD" --query 'builds[0].buildStatus' --output text)"
    [[ "$STATUS" != IN_PROGRESS ]] && break
    sleep 15
  done
  if [[ "$STATUS" != SUCCEEDED ]]; then
    echo "The image build ended ${STATUS}. Its log: CloudWatch Logs group /aws/codebuild/obs-hot" >&2
    exit 1
  fi
fi
DIGEST="$(aws ecr describe-images --repository-name obs-hot --image-ids imageTag="$TAG" \
  --query 'imageDetails[0].imageDigest' --output text)"
echo "Hot store image: ${TAG}"

# 3. The task.
deploy --parameter-overrides "ImageUri=${REPO}@${DIGEST}" "$@"
echo "Hot store: $(out obs-phaseH1 HotUrl). Next: infra/deploy-phase4.sh HotAccounts=<accounts>, then"
echo "infra/deploy-phaseT2.sh HotStream=obs-hot HotAccounts=<accounts>."
