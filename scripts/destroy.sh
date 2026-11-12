#!/usr/bin/env bash
# Tear down everything deploy_mcp.sh and setup_demo_infra.sh created.
# Prompts before deleting; set CONFIRM=yes to skip the prompt.
#
# Optional env: AWS_REGION, AGENT_RUNTIME_ARN, ECR_REPO, ECS_CLUSTER, DELETE_CLUSTER (yes/no)
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ECR_REPO="${ECR_REPO:-halcyon-flight-ops-mcp}"
ECS_CLUSTER="${ECS_CLUSTER:-halcyon-flight-ops}"
DELETE_CLUSTER="${DELETE_CLUSTER:-yes}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
AWS="aws --region ${REGION}"

BUCKETS=(
  "halcyon-flight-telemetry-raw-${ACCOUNT_ID}"
  "halcyon-simulation-results-${ACCOUNT_ID}"
  "halcyon-maintenance-logs-${ACCOUNT_ID}"
)
ALARMS=(halcyon-flight-data-latency halcyon-telemetry-queue-depth halcyon-battery-health-anomaly)
SERVICE_NAME="halcyon-telemetry-processor-svc"
TASK_FAMILY="halcyon-telemetry-processor"

echo "This deletes Halcyon demo resources in account ${ACCOUNT_ID}, region ${REGION}:"
echo "  AgentCore runtime: ${AGENT_RUNTIME_ARN:-<none set>}"
echo "  ECR repo:          ${ECR_REPO} (all images)"
echo "  ECS:               ${SERVICE_NAME} on ${ECS_CLUSTER} (delete cluster: ${DELETE_CLUSTER})"
echo "  Alarms:            ${ALARMS[*]}"
echo "  S3 buckets:        ${BUCKETS[*]} (all objects)"
if [[ "${CONFIRM:-}" != "yes" ]]; then
  read -r -p "Type 'yes' to continue: " answer
  [[ "${answer}" == "yes" ]] || { echo "Aborted."; exit 1; }
fi

if [[ -n "${AGENT_RUNTIME_ARN:-}" ]]; then
  echo "Deleting AgentCore runtime ${AGENT_RUNTIME_ARN##*/}"
  ${AWS} bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "${AGENT_RUNTIME_ARN##*/}" >/dev/null
fi

if ${AWS} ecr describe-repositories --repository-names "${ECR_REPO}" >/dev/null 2>&1; then
  echo "Deleting ECR repository ${ECR_REPO}"
  ${AWS} ecr delete-repository --repository-name "${ECR_REPO}" --force >/dev/null
fi

if ${AWS} ecs describe-services --cluster "${ECS_CLUSTER}" --services "${SERVICE_NAME}" \
    --query "services[?status=='ACTIVE'].serviceName" --output text 2>/dev/null | grep -q .; then
  echo "Deleting ECS service ${SERVICE_NAME}"
  ${AWS} ecs delete-service --cluster "${ECS_CLUSTER}" --service "${SERVICE_NAME}" --force >/dev/null
  ${AWS} ecs wait services-inactive --cluster "${ECS_CLUSTER}" --services "${SERVICE_NAME}"
fi
for td in $(${AWS} ecs list-task-definitions --family-prefix "${TASK_FAMILY}" --query 'taskDefinitionArns[]' --output text); do
  ${AWS} ecs deregister-task-definition --task-definition "${td}" >/dev/null
done
if [[ "${DELETE_CLUSTER}" == "yes" ]]; then
  ${AWS} ecs delete-cluster --cluster "${ECS_CLUSTER}" >/dev/null 2>&1 || echo "Cluster ${ECS_CLUSTER} not deleted (missing or still has services)."
fi
${AWS} logs delete-log-group --log-group-name "/ecs/${TASK_FAMILY}" 2>/dev/null || true

echo "Deleting CloudWatch alarms"
${AWS} cloudwatch delete-alarms --alarm-names "${ALARMS[@]}"

for b in "${BUCKETS[@]}"; do
  if ${AWS} s3api head-bucket --bucket "${b}" 2>/dev/null; then
    echo "Deleting bucket s3://${b}"
    ${AWS} s3 rb "s3://${b}" --force >/dev/null
  fi
done

echo "Done. EC2 Name/Project tags added by setup_demo_infra.sh are left in place;"
echo "remove them with: aws ec2 delete-tags --resources <instance-ids> --tags Key=Project Key=Environment Key=Owner"
