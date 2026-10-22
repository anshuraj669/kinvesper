#!/usr/bin/env bash
# Run Kinvesper on this machine against the deployed AWS stack: the same DynamoDB table and
# SNS topic as the cloud app. The brain is Claude with the stack's stored API key when there is one
# (make claude-key), otherwise a local Ollama model.
#
#   ./scripts/run_local_aws.sh               # voice simulator at http://127.0.0.1:8000/voice
#   LLM_PROVIDER=none ./scripts/run_local_aws.sh
#
# Needs the AWS CLI signed in (aws configure). Overrides: STACK, AWS_REGION, LLM_PROVIDER,
# LLM_MODEL, OLLAMA_URL, CARE_TZ, PORT. Proactive checks stay with EventBridge in the cloud.
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/load_env.sh
load_env .env  # e.g. ANTHROPIC_API_KEY; the stack's table and topic below replace STORE and DATA_FILE

STACK="${STACK:-Kinvesper}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
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

# Sign-in as in the cloud: the dashboard asks for Google/Amazon (the stack's Cognito) or its ?key= link.
# Cognito only returns to http://localhost:8000, so open the app at localhost, not 127.0.0.1.
secret() { [[ -n "$1" ]] && aws secretsmanager get-secret-value --secret-id "$1" --query SecretString --output text 2>/dev/null; }
export PORT="${PORT:-8000}"
if [[ -n "$(output DashboardLoginClientId)" && "$PORT" == 8000 ]]; then
  export OAUTH_ISSUER="$(output OAuthIssuer)" LOGIN_CLIENT_ID="$(output DashboardLoginClientId)"
  OAUTH_AUDIENCE=$(printf '%s\n' "$(output OAuthClientId)" "$(output AlexaClientId)" "$(output DashboardLoginClientId)" | paste -sd, | sed 's/,,*/,/g; s/,$//')
  export OAUTH_AUDIENCE OAUTH_SCOPES="${OAUTH_SCOPES:-kinvesper/mcp}" PUBLIC_URL="http://localhost:$PORT"
fi
[[ -n "${DASHBOARD_KEY:-}" ]] || export DASHBOARD_KEY="$(secret "$(output DashboardKeySecret)")"
[[ -n "${SIMULATOR_KEY:-}" ]] || export SIMULATOR_KEY="$(secret "$(output SimulatorKeySecret)")"
[[ -n "${SERVICE_TOKEN:-}" ]] || export SERVICE_TOKEN="$(secret "$(output ServiceTokenSecret)")"

# Claude: the key from .env, else the one stored in the stack (make claude-key); otherwise Ollama.
if [[ "${LLM_PROVIDER:-anthropic}" == anthropic && -z "${ANTHROPIC_API_KEY:-}" && -n "$(output ClaudeApiKeySecret)" ]]; then
  key=$(aws secretsmanager get-secret-value --secret-id "$(output ClaudeApiKeySecret)" \
    --query SecretString --output text 2>/dev/null) || key=""
  [[ "$key" == sk-ant-* ]] && export ANTHROPIC_API_KEY="$key"
  unset key
fi
if [[ -z "${LLM_PROVIDER:-}" ]]; then
  [[ -n "${ANTHROPIC_API_KEY:-}" ]] && LLM_PROVIDER=anthropic || LLM_PROVIDER=ollama
fi
[[ "$LLM_PROVIDER" != anthropic || -n "${ANTHROPIC_API_KEY:-}" ]] \
  || die "LLM_PROVIDER=anthropic needs ANTHROPIC_API_KEY: put your sk-ant-... key in .env"
export LLM_PROVIDER

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
  Dashboard      : http://localhost:$PORT/dashboard (sign in, or add ?key=<DashboardKey>)
EOF

cd "$(dirname "$0")/.."
exec uv run kinvesper "$@"
