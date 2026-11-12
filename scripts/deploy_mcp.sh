#!/usr/bin/env bash
# Build the MCP server image (linux/arm64), push it to ECR, and create the
# AgentCore Runtime that hosts it. Idempotent for the ECR steps.
#
# Required: AGENTCORE_ROLE_ARN (execution role the runtime assumes; see README)
# Optional: AWS_REGION, ECR_REPO, RUNTIME_NAME, ECS_CLUSTER, CONTAINER_CLI
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ECR_REPO="${ECR_REPO:-halcyon-flight-ops-mcp}"
RUNTIME_NAME="${RUNTIME_NAME:-halcyonFlightOps}"
ECS_CLUSTER="${ECS_CLUSTER:-halcyon-flight-ops}"
CLI="${CONTAINER_CLI:-docker}"
: "${AGENTCORE_ROLE_ARN:?set AGENTCORE_ROLE_ARN to the runtime execution role ARN}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
ECR_URI="${REGISTRY}/${ECR_REPO}"
MCP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/mcp_server"
TAGS_KV="Key=Project,Value=halcyon-flight-ops Key=Owner,Value=portfolio Key=Environment,Value=demo"

echo "Account ${ACCOUNT_ID} | Region ${REGION} | Repo ${ECR_URI}"

echo "[1/4] ECR login"
aws ecr get-login-password --region "${REGION}" | "${CLI}" login --username AWS --password-stdin "${REGISTRY}"

echo "[2/4] Ensure ECR repository"
if ! aws ecr describe-repositories --region "${REGION}" --repository-names "${ECR_REPO}" >/dev/null 2>&1; then
  # shellcheck disable=SC2086
  aws ecr create-repository --region "${REGION}" \
    --repository-name "${ECR_REPO}" \
    --image-scanning-configuration scanOnPush=true \
    --image-tag-mutability IMMUTABLE \
    --encryption-configuration encryptionType=AES256 \
    --tags ${TAGS_KV} >/dev/null
fi

echo "[3/4] Build and push (linux/arm64)"
IMAGE_TAG="$(date +%Y%m%d-%H%M%S)"
REMOTE_TAG="${ECR_URI}:${IMAGE_TAG}"
"${CLI}" build --platform linux/arm64 -t "${REMOTE_TAG}" "${MCP_DIR}"
"${CLI}" push "${REMOTE_TAG}"

echo "[4/4] Create AgentCore Runtime ${RUNTIME_NAME}"
RUNTIME_ARN="$(aws bedrock-agentcore-control create-agent-runtime --region "${REGION}" \
  --agent-runtime-name "${RUNTIME_NAME}" \
  --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"${REMOTE_TAG}\"}}" \
  --role-arn "${AGENTCORE_ROLE_ARN}" \
  --network-configuration '{"networkMode":"PUBLIC"}' \
  --protocol-configuration '{"serverProtocol":"MCP"}' \
  --environment-variables "AWS_REGION=${REGION},ECS_CLUSTER=${ECS_CLUSTER}" \
  --tags "Project=halcyon-flight-ops,Owner=portfolio,Environment=demo" \
  --query agentRuntimeArn --output text)"

echo ""
echo "Runtime created. Put this in .env and run with DEMO_MODE=live:"
echo "  AGENT_RUNTIME_ARN=${RUNTIME_ARN}"
echo "To roll out a new image later, use: aws bedrock-agentcore-control update-agent-runtime"
