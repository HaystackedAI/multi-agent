#!/usr/bin/env bash
# Deploy Chaser to Amazon Bedrock AgentCore Runtime with the AgentCore CLI.
#
# Prerequisites:
#   npm i -g @aws/agentcore
#   AWS credentials for the target account (aws login / aws configure / SSO), region us-east-1
#   Anthropic Claude enabled in the Bedrock console for that region
#   agentcore/aws-targets.json: replace <ACCOUNT_ID> with your 12-digit account id
#
# Usage:
#   ./scripts/deploy_agentcore.sh            # validate + deploy
#   ./scripts/deploy_agentcore.sh --validate # validate only
set -euo pipefail

cd "$(dirname "$0")/.."  # the CLI must run from the project root

if grep -q "<ACCOUNT_ID>" agentcore/aws-targets.json; then
  echo "error: fill in your account id in agentcore/aws-targets.json first" >&2
  exit 1
fi

echo "== agentcore validate"
agentcore validate

if [[ "${1:-}" == "--validate" ]]; then
  exit 0
fi

echo "==> agentcore deploy -y   (a CDK deployment: CodeZip upload + CloudFormation, usually 3-6 min; CDK prints each resource as it changes)"
agentcore deploy -y

cat <<'EOF'

Deployed. Next steps:
  1. Copy the runtime ARN printed above (also in agentcore/.cli/).
  2. Smoke test:
       agentcore invoke ChaserAgent '{"action": "status"}'
       agentcore invoke ChaserAgent '{"action": "sweep"}'
  3. Point the web UI at it:
       AGENT_BACKEND=agentcore AGENT_RUNTIME_ARN=<arn> make serve
EOF
