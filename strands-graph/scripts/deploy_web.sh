#!/usr/bin/env bash
# Host the web UI on AWS App Runner, pointed at the deployed AgentCore Runtime.
#
# No local Docker needed: CodeBuild clones the public GitHub repo (GitRef, default main), builds the
# image and pushes it to ECR; App Runner serves it with an HTTPS URL. Everything is one
# CloudFormation stack (infra/web.yaml). Re-run after pushing to rebuild; App Runner auto-deploys
# the new image and this script waits until that rollout is live.
#
# Every long step prints a progress line every 15 s (phase + elapsed), so a quiet terminal
# always means "done", never "hung". Typical total: 8-12 minutes.
#
# Requires: AWS CLI with credentials for the target account, the AgentCore runtime already
# deployed (scripts/deploy_agentcore.sh), and the commit you want to serve pushed to GitHub.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=chaser
RUNTIME_NAME=Chaser_ChaserAgent
STACK="${PROJECT}-web"
REGION="${AWS_REGION:-us-east-1}"
GIT_REF="${GIT_REF:-main}"
T0=$(date +%s)

elapsed() { local s=$(( $(date +%s) - T0 )); printf '%d:%02d' $((s / 60)) $((s % 60)); }
say() { echo "[$(elapsed)] $*"; }

# Run a blocking command in the background and print a heartbeat while it runs.
with_heartbeat() {
  local label=$1; shift
  "$@" & local pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    sleep 15
    kill -0 "$pid" 2>/dev/null && say "  ... ${label} still running: $(stack_activity)"
  done
  wait "$pid"
}

stack_activity() {
  aws cloudformation describe-stack-events --region "$REGION" --stack-name "$STACK" \
    --query 'StackEvents[0].[LogicalResourceId,ResourceStatus]' --output text 2>/dev/null | tr '\t' ' ' || echo "creating stack"
}

ARN="${AGENT_RUNTIME_ARN:-}"
if [ -z "$ARN" ]; then
  ARN=$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
    --query "agentRuntimes[?agentRuntimeName=='${RUNTIME_NAME}'].agentRuntimeArn | [0]" --output text)
fi
if [ -z "$ARN" ] || [ "$ARN" = "None" ]; then
  echo "No AgentCore runtime named ${RUNTIME_NAME} in ${REGION}; deploy it first or set AGENT_RUNTIME_ARN." >&2
  exit 1
fi
say "Runtime: ${ARN}"

deploy() {
  aws cloudformation deploy --region "$REGION" --stack-name "$STACK" \
    --template-file infra/web.yaml --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
    --parameter-overrides AgentRuntimeArn="$ARN" GitRef="$GIT_REF" CreateService="$1"
}

# The App Runner service can only be created once an image exists, so the very first run creates
# the stack without it (CreateService=false), builds, then adds it. On re-runs the service already
# exists and must be kept: passing false again would delete it and its URL.
HAS_SERVICE=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Parameters[?ParameterKey=='CreateService'].ParameterValue | [0]" --output text 2>/dev/null || echo false)
[ "$HAS_SERVICE" = "true" ] || HAS_SERVICE=false

say "1/4  ECR repository and CodeBuild project (stack ${STACK}, service present: ${HAS_SERVICE})"
with_heartbeat "CloudFormation" deploy "$HAS_SERVICE"

say "2/4  Building the image in CodeBuild from ${GIT_REF} (usually 4-6 min)"
BUILD_ID=$(aws codebuild start-build --region "$REGION" --project-name "${PROJECT}-web-build" \
  --query build.id --output text)
while :; do
  read -r STATUS PHASE < <(aws codebuild batch-get-builds --region "$REGION" --ids "$BUILD_ID" \
    --query 'builds[0].[buildStatus,currentPhase]' --output text)
  case "$STATUS" in
    IN_PROGRESS) say "  ... CodeBuild phase: ${PHASE}"; sleep 15 ;;
    SUCCEEDED) say "  image pushed"; break ;;
    *) echo "CodeBuild ${BUILD_ID}: ${STATUS} (see CloudWatch log group /aws/codebuild/${PROJECT}-web-build)" >&2; exit 1 ;;
  esac
done

say "3/4  App Runner service (first create takes 3-5 min; later runs are a no-op)"
with_heartbeat "CloudFormation" deploy true

URL=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ServiceUrl'].OutputValue" --output text)
SERVICE_ARN=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ServiceArn'].OutputValue" --output text)

say "4/4  Waiting for App Runner to roll the new image out"
while :; do
  SVC_STATUS=$(aws apprunner describe-service --region "$REGION" --service-arn "$SERVICE_ARN" \
    --query 'Service.Status' --output text)
  read -r OP_TYPE OP_STATUS < <(aws apprunner list-operations --region "$REGION" --service-arn "$SERVICE_ARN" \
    --max-results 1 --query 'OperationSummaryList[0].[Type,Status]' --output text)
  if [ "$SVC_STATUS" = "RUNNING" ] && [ "$OP_STATUS" != "IN_PROGRESS" ] && [ "$OP_STATUS" != "PENDING" ]; then
    break
  fi
  say "  ... service ${SVC_STATUS}, last operation ${OP_TYPE} ${OP_STATUS}"
  sleep 15
done
if [ "$OP_STATUS" != "SUCCEEDED" ]; then
  echo "App Runner operation ${OP_TYPE} ended ${OP_STATUS}; check the service in the console." >&2
  exit 1
fi

say "Done in $(elapsed). Web UI: ${URL}"
