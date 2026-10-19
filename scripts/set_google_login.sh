#!/usr/bin/env bash
# Store the Google OAuth client secret for "Continue with Google" on the Kinvesper sign-in page.
#
#   ./scripts/set_google_login.sh       # uses GOOGLE_CLIENT_SECRET from .env, or asks for it
#
# Then deploy with -c googleClientId=<the client ID>. In Google Cloud Console, the OAuth client
# (type "Web application") needs the stack output GoogleRedirectUri as an authorized redirect URI.
# Needs the AWS CLI signed in. Overrides: AWS_REGION.
set -euo pipefail

cd "$(dirname "$0")/.."
source scripts/load_env.sh
load_env .env

NAME="kinvesper/google-client-secret"  # infra/kinvesper_stack.py GOOGLE_SECRET
export AWS_REGION="${AWS_REGION:-us-east-1}"
die() { echo "error: $*" >&2; exit 1; }
command -v aws >/dev/null || die "the AWS CLI is not installed"

secret="${GOOGLE_CLIENT_SECRET:-}"
if [[ -n "$secret" ]]; then
  echo "Using GOOGLE_CLIENT_SECRET from .env (ending ...${secret: -4})."
else
  read -rsp "Google OAuth client secret: " secret; echo
fi
[[ ${#secret} -ge 20 && "$secret" != *" "* ]] || die "that doesn't look like a Google client secret (GOCSPX-...)"

# Through a private temp file, so the secret never appears in the process list.
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
chmod 600 "$tmp"
if aws secretsmanager describe-secret --secret-id "$NAME" >/dev/null 2>&1; then
  printf '{"SecretId": "%s", "SecretString": "%s"}' "$NAME" "$secret" > "$tmp"
  aws secretsmanager put-secret-value --cli-input-json "file://$tmp" >/dev/null
else
  printf '{"Name": "%s", "Description": "Kinvesper: Google OAuth client secret for caregiver sign-in", "SecretString": "%s"}' \
    "$NAME" "$secret" > "$tmp"
  aws secretsmanager create-secret --cli-input-json "file://$tmp" >/dev/null
fi
echo "Stored. Now deploy with -c googleClientId=<your client ID> (cd infra && cdk deploy ...)."
