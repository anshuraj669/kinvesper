#!/usr/bin/env bash
# Run Kinvesper on this machine against the deployed AWS stack: the same DynamoDB table and
# SNS topic as the cloud app, with a local Ollama model as the brain.
#
#   ./scripts/run_local_aws.sh               # voice simulator at http://127.0.0.1:8000/voice
#   LLM_PROVIDER=none ./scripts/run_local_aws.sh
#
# Needs the AWS CLI signed in (aws configure). Overrides: STACK, AWS_REGION, LLM_PROVIDER,
# LLM_MODEL, OLLAMA_URL, CARE_TZ, PORT. Proactive checks stay with EventBridge in the cloud.
set -euo pipefail

STACK="${STACK:-Kinvesper}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export LLM_PROVIDER="${LLM_PROVIDER:-ollama}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
LLM_MODEL="${LLM_MODEL:-llama3.1}"

die() { echo "error: $*" >&2; exit 1; }

command -v aws >/dev/null || die "the AWS CLI is not installed"
aws sts get-caller-identity >/dev/null 2>&1 || die "AWS credentials are missing or expired; run: aws configure"

outputs=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output text 2>/dev/null) \
  || die "stack '$STACK' not found in $AWS_REGION; deploy it first (see infra/README.md)"
output() { awk -v k="$1" '$1 == k { print $2 }' <<<"$outputs"; }

export STORE=dynamodb
export DYNAMODB_TABLE="$(output TableName)"
export SNS_TOPIC_ARN="$(output AlertsTopic)"
export SCHEDULER_INTERVAL_S=0  # EventBridge already runs the checks against the same table
[[ -n "$DYNAMODB_TABLE" && -n "$SNS_TOPIC_ARN" ]] || die "stack '$STACK' is missing the TableName or AlertsTopic output"

if [[ "$LLM_PROVIDER" == "ollama" ]]; then
  export OLLAMA_URL LLM_MODEL
  models=$(curl -sf --max-time 3 "$OLLAMA_URL/api/tags") \
    || die "Ollama is not reachable at $OLLAMA_URL; start it (ollama serve) or set LLM_PROVIDER=none"
  grep -q "\"$LLM_MODEL" <<<"$models" || die "model '$LLM_MODEL' is not pulled; run: ollama pull $LLM_MODEL"
  llm="ollama ($LLM_MODEL at $OLLAMA_URL)"
else
  llm="$LLM_PROVIDER"
fi

cat <<EOF
Kinvesper (local) -> AWS stack '$STACK' in $AWS_REGION
  DynamoDB table : $DYNAMODB_TABLE
  SNS topic      : $SNS_TOPIC_ARN
  LLM            : $llm
EOF

cd "$(dirname "$0")/.."
exec uv run kinvesper "$@"
