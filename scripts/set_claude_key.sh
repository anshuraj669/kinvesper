#!/usr/bin/env bash
# Store the Claude API key the deployed voice agent uses, then restart the service to pick it up.
#
#   ./scripts/set_claude_key.sh          # asks for the key (from platform.claude.com, starts sk-ant-)
#
# Needs the stack deployed with -c llmProvider=anthropic and the AWS CLI signed in.
# Overrides: STACK, AWS_REGION.
set -euo pipefail

STACK="${STACK:-Kinvesper}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
die() { echo "error: $*" >&2; exit 1; }
output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null
}

command -v aws >/dev/null || die "the AWS CLI is not installed"
secret=$(output ClaudeApiKeySecret) || die "stack '$STACK' not found in $AWS_REGION"
[[ -n "$secret" && "$secret" != "None" ]] || die "deploy the stack with -c llmProvider=anthropic first"
service=$(output ServiceArn)
[[ -n "$service" && "$service" != "None" ]] || die "stack '$STACK' has no ServiceArn output: redeploy it"

read -rsp "Claude API key: " key; echo
[[ "$key" == sk-ant-* ]] || die "that doesn't look like a Claude API key (sk-ant-...)"

# Through a private temp file, so the key never appears in the process list.
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
chmod 600 "$tmp"
printf '{"SecretId": "%s", "SecretString": "%s"}' "$secret" "$key" > "$tmp"
aws secretsmanager put-secret-value --cli-input-json "file://$tmp" >/dev/null
echo "Key stored. Restarting the service (takes about 3 minutes)..."
aws apprunner start-deployment --service-arn "$service" --query OperationId --output text >/dev/null
echo "Done. Kinvesper answers with Claude once the service is running again."
