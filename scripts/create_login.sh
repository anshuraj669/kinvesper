#!/usr/bin/env bash
# Create a family login: for signing in to the caregiver dashboard and linking Kinvesper in the Alexa app.
#
#   ./scripts/create_login.sh margaret@example.com      # asks for a password (12+ characters)
#
# The deployed user pool doesn't allow self sign-up, so logins are made here. Needs the AWS CLI
# signed in (aws configure). Overrides: STACK, AWS_REGION.
set -euo pipefail

STACK="${STACK:-Kinvesper}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
die() { echo "error: $*" >&2; exit 1; }

email="${1:-}"
[[ "$email" == *@*.* ]] || die "usage: $0 <email>"
command -v aws >/dev/null || die "the AWS CLI is not installed"

pool=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text 2>/dev/null) \
  || die "stack '$STACK' not found in $AWS_REGION"
[[ -n "$pool" && "$pool" != "None" ]] || die "stack '$STACK' has no UserPoolId output"

read -rsp "Password for $email (12+ characters): " password; echo
read -rsp "Repeat it: " again; echo
[[ "$password" == "$again" ]] || die "the passwords don't match"
(( ${#password} >= 12 )) || die "the password must be at least 12 characters"

if aws cognito-idp admin-get-user --user-pool-id "$pool" --username "$email" >/dev/null 2>&1; then
  echo "$email already exists: setting a new password."
else
  aws cognito-idp admin-create-user --user-pool-id "$pool" --username "$email" --message-action SUPPRESS \
    --user-attributes Name=email,Value="$email" Name=email_verified,Value=true >/dev/null
fi
# Through a private temp file, so the password never appears in the process list
request=$(mktemp)
trap 'rm -f "$request"' EXIT
printf %s "$password" | python3 -c 'import json, sys; print(json.dumps({"UserPoolId": sys.argv[1],
  "Username": sys.argv[2], "Password": sys.stdin.read(), "Permanent": True}))' "$pool" "$email" >"$request"
aws cognito-idp admin-set-user-password --cli-input-json "file://$request"

echo "Done. Sign in as $email on the dashboard, or when the Alexa app asks you to link Kinvesper."
