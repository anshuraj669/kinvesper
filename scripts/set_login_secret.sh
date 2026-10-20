#!/usr/bin/env bash
# Store the client secret for "Continue with Google" or "Login with Amazon" on the Kinvesper sign-in page.
#
#   ./scripts/set_login_secret.sh google    # uses GOOGLE_CLIENT_SECRET from .env, or asks for it
#   ./scripts/set_login_secret.sh amazon    # uses AMAZON_CLIENT_SECRET from .env, or asks for it
#
# Then deploy with -c googleClientId=<client ID> or -c amazonClientId=<client ID>. Google's OAuth client
# ("Web application") and Amazon's security profile (Web Settings) both need the stack output
# GoogleRedirectUri (Cognito's /oauth2/idpresponse) as a redirect / allowed return URL.
# Needs the AWS CLI signed in. Overrides: AWS_REGION.
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/load_env.sh
load_env .env

export AWS_REGION="${AWS_REGION:-us-east-1}"
die() { echo "error: $*" >&2; exit 1; }
command -v aws >/dev/null || die "the AWS CLI is not installed"

case "${1:-}" in  # names match infra/kinvesper_stack.py GOOGLE_SECRET / AMAZON_SECRET
  google) NAME="kinvesper/google-client-secret"; LABEL="Google OAuth"; secret="${GOOGLE_CLIENT_SECRET:-}"; var=GOOGLE_CLIENT_SECRET ;;
  amazon) NAME="kinvesper/amazon-client-secret"; LABEL="Login with Amazon"; secret="${AMAZON_CLIENT_SECRET:-}"; var=AMAZON_CLIENT_SECRET ;;
  *) die "usage: $0 google|amazon" ;;
esac

if [[ -n "$secret" ]]; then
  echo "Using $var from .env (ending ...${secret: -4})."
else
  read -rsp "$LABEL client secret: " secret; echo
fi
[[ ${#secret} -ge 20 && "$secret" != *" "* && "$secret" != *'"'* ]] || die "that doesn't look like a $LABEL client secret"

# Through a private temp file, so the secret never appears in the process list.
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
chmod 600 "$tmp"
if aws secretsmanager describe-secret --secret-id "$NAME" >/dev/null 2>&1; then
  printf '{"SecretId": "%s", "SecretString": "%s"}' "$NAME" "$secret" > "$tmp"
  aws secretsmanager put-secret-value --cli-input-json "file://$tmp" >/dev/null
else
  printf '{"Name": "%s", "Description": "Kinvesper: %s client secret for caregiver sign-in", "SecretString": "%s"}' \
    "$NAME" "$LABEL" "$secret" > "$tmp"
  aws secretsmanager create-secret --cli-input-json "file://$tmp" >/dev/null
fi
echo "Stored. Now deploy with -c ${1}ClientId=<your client ID> (cd infra && cdk deploy ...)."
