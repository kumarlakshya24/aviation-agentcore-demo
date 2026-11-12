#!/usr/bin/env bash
# setup_demo_infra.sh: provisions synthetic AWS resources so a sandbox account
# looks like the (fictional) Halcyon Aero flight-ops environment.
# Idempotent: safe to run multiple times. Run only in a sandbox account.
#
# Optional env: AWS_REGION, ECS_CLUSTER, ECS_TASK_EXECUTION_ROLE_ARN
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECS_CLUSTER="${ECS_CLUSTER:-halcyon-flight-ops}"
EXEC_ROLE_ARN="${ECS_TASK_EXECUTION_ROLE_ARN:-arn:aws:iam::${ACCOUNT_ID}:role/ecsTaskExecutionRole}"

AWS="aws --region ${REGION}"

echo "=============================================="
echo "  Halcyon Flight Ops: demo infrastructure setup"
echo "=============================================="
echo ""
echo "Account : ${ACCOUNT_ID}"
echo "Region  : ${REGION}"
echo "Cluster : ${ECS_CLUSTER}"
echo ""

###############################################################################
# Step 1: Tag running EC2 instances with flight-ops service names
###############################################################################
echo "----------------------------------------------"
echo "  Step 1/5: Tagging EC2 instances"
echo "----------------------------------------------"

INSTANCE_NAMES=(
  "flight-data-processor"
  "sim-scheduler-01"
  "telemetry-gateway"
  "maintenance-api"
  "avionics-pipeline"
  "ground-control-relay"
  "battery-monitor-svc"
  "airspace-coordinator"
  "preflight-validator"
  "fleet-health-aggregator"
  "route-optimizer"
  "weather-ingestion"
)

# Collect all running instance IDs
INSTANCE_IDS=($( ${AWS} ec2 describe-instances \
  --filters "Name=instance-state-name,Values=running" \
  --query "Reservations[].Instances[].InstanceId" \
  --output text ))

if [[ ${#INSTANCE_IDS[@]} -eq 0 ]]; then
  echo "  [WARN] No running EC2 instances found. Skipping tagging."
else
  echo "  Found ${#INSTANCE_IDS[@]} running instance(s)."
  for i in "${!INSTANCE_IDS[@]}"; do
    # Cycle through names if we have more instances than names
    NAME_INDEX=$(( i % ${#INSTANCE_NAMES[@]} ))
    NAME="${INSTANCE_NAMES[$NAME_INDEX]}"
    INSTANCE="${INSTANCE_IDS[$i]}"

    echo "  Tagging ${INSTANCE} -> ${NAME}"
    ${AWS} ec2 create-tags \
      --resources "${INSTANCE}" \
      --tags \
        "Key=Name,Value=${NAME}" \
        "Key=Project,Value=halcyon-flight-ops" \
        "Key=Environment,Value=demo" \
        "Key=Owner,Value=portfolio"
  done
  echo "  [OK] Tagged ${#INSTANCE_IDS[@]} instance(s)."
fi
echo ""

###############################################################################
# Step 2: Create CloudWatch alarms on custom Halcyon/FlightOps metrics
###############################################################################
echo "----------------------------------------------"
echo "  Step 2/5: Creating CloudWatch alarms"
echo "----------------------------------------------"

# Helper: create or update a CloudWatch alarm (put-metric-alarm is idempotent)
create_alarm() {
  local alarm_name="$1"
  local description="$2"
  local metric_name="$3"
  local threshold="$4"
  local comparison="$5"

  echo "  Creating alarm: ${alarm_name}"
  ${AWS} cloudwatch put-metric-alarm \
    --alarm-name "${alarm_name}" \
    --alarm-description "${description}" \
    --namespace "Halcyon/FlightOps" \
    --metric-name "${metric_name}" \
    --statistic "Average" \
    --period 60 \
    --evaluation-periods 1 \
    --threshold "${threshold}" \
    --comparison-operator "${comparison}" \
    --treat-missing-data "breaching" \
    --tags Key=Project,Value=halcyon-flight-ops Key=Owner,Value=portfolio Key=Environment,Value=demo
}

create_alarm \
  "halcyon-flight-data-latency" \
  "Flight data processing latency exceeds 500ms: investigate pipeline bottlenecks in the telemetry ingest path." \
  "FlightDataProcessingLatency" \
  500 \
  "GreaterThanThreshold"

create_alarm \
  "halcyon-telemetry-queue-depth" \
  "Telemetry queue depth exceeds 1000 messages: consumers may be falling behind real-time vehicle data." \
  "TelemetryQueueDepth" \
  1000 \
  "GreaterThanThreshold"

create_alarm \
  "halcyon-battery-health-anomaly" \
  "Battery health score dropped below 80: potential cell degradation detected across fleet." \
  "BatteryHealthScore" \
  80 \
  "LessThanThreshold"

echo "  [OK] 3 CloudWatch alarms created/updated."
echo ""

###############################################################################
# Step 3: Create S3 buckets with realistic sample data
###############################################################################
echo "----------------------------------------------"
echo "  Step 3/5: Creating S3 buckets + sample data"
echo "----------------------------------------------"

BUCKET_TELEMETRY="halcyon-flight-telemetry-raw-${ACCOUNT_ID}"
BUCKET_SIMULATION="halcyon-simulation-results-${ACCOUNT_ID}"
BUCKET_MAINTENANCE="halcyon-maintenance-logs-${ACCOUNT_ID}"

# Helper: create bucket if it does not already exist
create_bucket() {
  local bucket="$1"
  if ${AWS} s3api head-bucket --bucket "${bucket}" 2>/dev/null; then
    echo "  Bucket s3://${bucket} already exists."
  else
    echo "  Creating bucket s3://${bucket}"
    ${AWS} s3api create-bucket --bucket "${bucket}"
    ${AWS} s3api put-bucket-tagging --bucket "${bucket}" --tagging "TagSet=[{Key=Project,Value=halcyon-flight-ops},{Key=Environment,Value=demo},{Key=Owner,Value=portfolio}]"
    ${AWS} s3api put-public-access-block --bucket "${bucket}" \
      --public-access-block-configuration \
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
  fi
}

create_bucket "${BUCKET_TELEMETRY}"
create_bucket "${BUCKET_SIMULATION}"
create_bucket "${BUCKET_MAINTENANCE}"

# --- Telemetry bucket: 3 flight JSON files ---
echo "  Uploading sample flight telemetry data..."

${AWS} s3 cp - "s3://${BUCKET_TELEMETRY}/flights/2026-03-31/flight-HAL-4201.json" <<'FLIGHT1'
{
  "flight_id": "HAL-4201",
  "aircraft": "H-0042",
  "route": {"origin": "KJFK-VP3", "destination": "KEWR-VP1"},
  "departure_utc": "2026-03-31T14:22:00Z",
  "arrival_utc": "2026-03-31T14:35:00Z",
  "passengers": 4,
  "telemetry_samples": 780,
  "battery_soc_start": 94.2,
  "battery_soc_end": 61.7,
  "max_altitude_ft": 1500,
  "avg_ground_speed_kts": 135,
  "status": "completed"
}
FLIGHT1

${AWS} s3 cp - "s3://${BUCKET_TELEMETRY}/flights/2026-03-31/flight-HAL-4202.json" <<'FLIGHT2'
{
  "flight_id": "HAL-4202",
  "aircraft": "H-0038",
  "route": {"origin": "KLAX-VP2", "destination": "KSMO-VP1"},
  "departure_utc": "2026-03-31T16:05:00Z",
  "arrival_utc": "2026-03-31T16:18:00Z",
  "passengers": 3,
  "telemetry_samples": 624,
  "battery_soc_start": 98.1,
  "battery_soc_end": 72.3,
  "max_altitude_ft": 1200,
  "avg_ground_speed_kts": 128,
  "status": "completed"
}
FLIGHT2

${AWS} s3 cp - "s3://${BUCKET_TELEMETRY}/flights/2026-03-31/flight-HAL-4203.json" <<'FLIGHT3'
{
  "flight_id": "HAL-4203",
  "aircraft": "H-0055",
  "route": {"origin": "KEWR-VP1", "destination": "KJFK-VP3"},
  "departure_utc": "2026-03-31T18:45:00Z",
  "arrival_utc": "2026-03-31T18:57:00Z",
  "passengers": 2,
  "telemetry_samples": 540,
  "battery_soc_start": 88.6,
  "battery_soc_end": 55.1,
  "max_altitude_ft": 1400,
  "avg_ground_speed_kts": 142,
  "status": "completed"
}
FLIGHT3

# --- Simulation bucket: 2 CSV files ---
echo "  Uploading sample simulation results..."

${AWS} s3 cp - "s3://${BUCKET_SIMULATION}/sim-runs/2026-Q1/noise-profile-JFK-corridor.csv" <<'SIM1'
waypoint_id,lat,lon,altitude_ft,noise_dba,timestamp_utc
WP-001,40.6413,-73.7781,500,62.3,2026-03-15T10:00:00Z
WP-002,40.6550,-73.7500,1000,54.1,2026-03-15T10:02:00Z
WP-003,40.6700,-73.7200,1500,48.7,2026-03-15T10:04:00Z
WP-004,40.6900,-73.6900,1500,47.2,2026-03-15T10:06:00Z
WP-005,40.7100,-73.6600,1200,51.8,2026-03-15T10:08:00Z
WP-006,40.7300,-73.6300,800,57.4,2026-03-15T10:10:00Z
WP-007,40.7413,-73.6100,400,63.9,2026-03-15T10:12:00Z
SIM1

${AWS} s3 cp - "s3://${BUCKET_SIMULATION}/sim-runs/2026-Q1/battery-degradation-model-v3.csv" <<'SIM2'
cycle_number,soc_start,soc_end,capacity_ah,internal_resistance_mohm,temp_avg_c,degradation_pct
1,100.0,20.0,148.2,12.1,28.3,0.00
50,99.8,20.0,147.6,12.3,29.1,0.41
100,99.5,20.0,146.9,12.6,28.7,0.88
200,99.1,20.0,145.4,13.2,29.5,1.89
300,98.6,20.0,143.8,13.9,30.1,2.97
400,97.9,20.0,141.7,14.8,29.8,4.39
500,97.0,20.0,139.2,16.1,30.4,6.07
SIM2

# --- Maintenance bucket: 2 log files ---
echo "  Uploading sample maintenance logs..."

${AWS} s3 cp - "s3://${BUCKET_MAINTENANCE}/logs/2026-03/H-0042-inspection-2026-03-28.txt" <<'MAINT1'
MAINTENANCE INSPECTION REPORT
Aircraft: H-0042 | Date: 2026-03-28 | Inspector: M. Chen (cert HAL-I-0091)
Type: 100-hour inspection

[PASS] Propulsion system: all 6 tilt-rotors within spec
[PASS] Battery pack #1: 146.8 Ah capacity (min 140 Ah required)
[PASS] Battery pack #2: 147.1 Ah capacity
[PASS] Flight control surfaces: actuator response within 2ms tolerance
[PASS] Avionics suite: firmware v4.2.1, all self-tests passed
[NOTE] Landing gear strut #3: minor surface corrosion observed, scheduled for treatment
[PASS] Emergency systems: parachute deployment mechanism verified
[PASS] Passenger cabin: seats, restraints, and lighting operational

Result: AIRWORTHY: cleared for revenue service
Next inspection due: 2026-04-28 or 100 flight hours, whichever comes first
MAINT1

${AWS} s3 cp - "s3://${BUCKET_MAINTENANCE}/logs/2026-03/H-0038-battery-swap-2026-03-30.txt" <<'MAINT2'
MAINTENANCE WORK ORDER
Aircraft: H-0038 | Date: 2026-03-30 | Technician: R. Patel (Cert #HAL-T-0152)
Type: Battery pack replacement (scheduled)

Work performed:
  1. Removed battery pack #2 (S/N BP-2024-08812): 512 cycles, 6.1% degradation
  2. Installed new battery pack #2 (S/N BP-2026-01447): 0 cycles, factory fresh
  3. Ran full charge-discharge-charge calibration cycle (4.2 hours)
  4. Updated BMS firmware to v2.8.3
  5. Performed ground-run power verification at 25%, 50%, 75%, 100% thrust
  6. All parameters nominal: battery integration test PASSED

Parts used:
  - 1x Halcyon Gen4 Battery Pack (P/N HAL-BP-G4-001): $42,500
  - 1x BMS connector harness (P/N HAL-BMS-CH-003): $1,200

Total labor: 6.5 hours
Result: Aircraft returned to service
MAINT2

echo "  [OK] 3 S3 buckets created with sample data."
echo ""

###############################################################################
# Step 4: Create ECS task definition + Fargate service
###############################################################################
echo "----------------------------------------------"
echo "  Step 4/5: Creating ECS task def + service"
echo "----------------------------------------------"

TASK_FAMILY="halcyon-telemetry-processor"
SERVICE_NAME="halcyon-telemetry-processor-svc"

# create-cluster is idempotent for an existing ACTIVE cluster name
${AWS} ecs create-cluster --cluster-name "${ECS_CLUSTER}" \
  --tags "key=Project,value=halcyon-flight-ops" "key=Environment,value=demo" "key=Owner,value=portfolio" >/dev/null

# Register task definition (always registers a new revision; ECS handles dedup by family)
echo "  Registering task definition: ${TASK_FAMILY}"
TASK_DEF_ARN=$(${AWS} ecs register-task-definition \
  --family "${TASK_FAMILY}" \
  --requires-compatibilities FARGATE \
  --network-mode awsvpc \
  --cpu "256" \
  --memory "512" \
  --execution-role-arn "${EXEC_ROLE_ARN}" \
  --container-definitions '[
    {
      "name": "telemetry-processor",
      "image": "nginx:alpine",
      "essential": true,
      "portMappings": [{"containerPort": 80, "protocol": "tcp"}],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/halcyon-telemetry-processor",
          "awslogs-region": "'"${REGION}"'",
          "awslogs-stream-prefix": "ecs",
          "awslogs-create-group": "true"
        }
      }
    }
  ]' \
  --tags "key=Project,value=halcyon-flight-ops" "key=Environment,value=demo" "key=Owner,value=portfolio" \
  --query "taskDefinition.taskDefinitionArn" \
  --output text)

echo "  Registered: ${TASK_DEF_ARN}"

# Create service if it does not exist
SERVICE_EXISTS=$(${AWS} ecs describe-services \
  --cluster "${ECS_CLUSTER}" \
  --services "${SERVICE_NAME}" \
  --query "services[?status=='ACTIVE'].serviceName" \
  --output text 2>/dev/null || true)

if [[ -n "${SERVICE_EXISTS}" ]]; then
  echo "  Service ${SERVICE_NAME} already exists. Updating to latest task def..."
  ${AWS} ecs update-service \
    --cluster "${ECS_CLUSTER}" \
    --service "${SERVICE_NAME}" \
    --task-definition "${TASK_DEF_ARN}" \
    --desired-count 2 \
    --query "service.serviceName" \
    --output text > /dev/null
  echo "  [OK] Service updated."
else
  echo "  Creating service: ${SERVICE_NAME}"

  # Discover default VPC subnets for Fargate networking
  DEFAULT_VPC_ID=$(${AWS} ec2 describe-vpcs \
    --filters "Name=isDefault,Values=true" \
    --query "Vpcs[0].VpcId" \
    --output text)

  SUBNET_IDS=$(${AWS} ec2 describe-subnets \
    --filters "Name=vpc-id,Values=${DEFAULT_VPC_ID}" \
    --query "Subnets[].SubnetId" \
    --output text | tr '\t' ',')

  DEFAULT_SG=$(${AWS} ec2 describe-security-groups \
    --filters "Name=vpc-id,Values=${DEFAULT_VPC_ID}" "Name=group-name,Values=default" \
    --query "SecurityGroups[0].GroupId" \
    --output text)

  ${AWS} ecs create-service \
    --cluster "${ECS_CLUSTER}" \
    --service-name "${SERVICE_NAME}" \
    --task-definition "${TASK_DEF_ARN}" \
    --desired-count 2 \
    --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[${SUBNET_IDS}],securityGroups=[${DEFAULT_SG}],assignPublicIp=ENABLED}" \
    --tags "key=Project,value=halcyon-flight-ops" "key=Environment,value=demo" "key=Owner,value=portfolio" \
    --query "service.serviceName" \
    --output text > /dev/null

  echo "  [OK] Service created with 2 desired tasks."
fi
echo ""

###############################################################################
# Step 5: Summary
###############################################################################
echo "=============================================="
echo "  Setup Complete"
echo "=============================================="
echo ""
echo "  EC2 instances  : ${#INSTANCE_IDS[@]} tagged with flight-ops service names"
echo "  CloudWatch     : 3 alarms (all in ALARM state due to missing data)"
echo "  S3 buckets     : 3 created with sample flight/sim/maintenance data"
echo "    - s3://${BUCKET_TELEMETRY}"
echo "    - s3://${BUCKET_SIMULATION}"
echo "    - s3://${BUCKET_MAINTENANCE}"
echo "  ECS            : Task def ${TASK_FAMILY} + service ${SERVICE_NAME}"
echo "                   on cluster ${ECS_CLUSTER} (2 Fargate tasks)"
echo ""
echo "  Demo environment is ready."
echo "=============================================="
