#!/usr/bin/env bash
# Store the Twilio credentials for urgent-alert phone calls, then restart the service to pick them up.
#
#   ./scripts/set_twilio.sh       # uses TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM from .env, or asks
#
# Needs an upgraded Twilio account: the free trial's calls only play Twilio's sample scripts.
# Overrides: STACK, AWS_REGION.
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/load_env.sh
load_env .env

STACK="${STACK:-Kinvesper}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
die() { echo "error: $*" >&2; exit 1; }
output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null
}

command -v aws >/dev/null || die "the AWS CLI is not installed"
secret=$(output TwilioSecret) || die "stack '$STACK' not found in $AWS_REGION"
[[ -n "$secret" && "$secret" != "None" ]] || die "redeploy the stack first: it has no TwilioSecret output"
service=$(output ServiceArn)

sid="${TWILIO_ACCOUNT_SID:-}" token="${TWILIO_AUTH_TOKEN:-}" from="${TWILIO_FROM:-}"
[[ -n "$sid" ]] || read -rp "Twilio Account SID (AC...): " sid
[[ -n "$token" ]] || { read -rsp "Twilio Auth Token: " token; echo; }
[[ -n "$from" ]] || read -rp "Twilio phone number (+1...): " from
from="${from//[ -]/}"
[[ "$sid" =~ ^AC[0-9a-f]{32}$ ]] || die "the Account SID starts with AC and has 32 more characters"
[[ "$token" =~ ^[0-9a-f]{32}$ ]] || die "the Auth Token is 32 letters and digits (Twilio console, Account info)"
[[ "$from" =~ ^\+[1-9][0-9]{7,14}$ ]] || die "the Twilio number needs + and the country code, like +15551234567"
echo "Using Twilio account ...${sid: -4}, calling from $from."

# Through a private temp file, so the token never appears in the process list.
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
chmod 600 "$tmp"
printf '{"SecretId": "%s", "SecretString": "%s:%s:%s"}' "$secret" "$sid" "$token" "$from" > "$tmp"
aws secretsmanager put-secret-value --cli-input-json "file://$tmp" >/dev/null
echo "Stored. Restarting the service (takes about 3 minutes)..."
aws apprunner start-deployment --service-arn "$service" --query OperationId --output text >/dev/null
echo "Done. Urgent alerts will ring the caregiver's phone once the service is running again."
