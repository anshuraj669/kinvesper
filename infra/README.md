# Deploying Kinvesper to AWS

One CDK stack (`kinvesper_stack.py`) creates everything:

| Service | What it does here |
|---|---|
| **AWS App Runner** | Runs the container (1 vCPU, 2 GB, exactly one instance) with an HTTPS URL and `/health` checks |
| **Amazon ECR** | Holds the image that `cdk deploy` builds from the repo's `Dockerfile` |
| **Amazon DynamoDB** | Single table (`pk` = patient, `sk` = `<kind>#<id>`), on-demand, point-in-time recovery |
| **Amazon Bedrock** | Claude (`anthropic.claude-opus-5-5`) through the Messages API endpoint (`bedrock-mantle:CreateInference`) |
| **Amazon Cognito** | OAuth 2.1 authorization server for `/mcp`: user pool, `kinvesper/mcp` scope, public PKCE client, hosted login |
| **Amazon SNS** | Caregiver alert texts; a second topic for ops alarms |
| **AWS Secrets Manager** | Generated `SERVICE_TOKEN`, `SIMULATOR_KEY`, `DASHBOARD_KEY` (never in the template or image) |
| **Amazon EventBridge** | Calls `POST /api/checks` every 5 minutes (missed doses, inactivity, low stock) |
| **Amazon CloudWatch** | App Runner logs, and an alarm on 5xx responses |
| **AWS IAM** | Least-privilege instance role: the table, the alerts topic, the three secrets, Bedrock inference |

Why one instance: MCP Streamable HTTP sessions and voice-simulator sessions live in memory.
Why EventBridge instead of the in-app timer: App Runner only gives an instance CPU while it serves
requests, so the app's scheduler is off (`SCHEDULER_INTERVAL_S=0`) and EventBridge drives the checks.

## Before you start

- An AWS account and the [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), signed in (`aws configure` or `aws sso login`). Check with `aws sts get-caller-identity`.
- Docker running (the image is built locally and pushed to ECR).
- Node.js 22 or 24 for the CDK CLI (18 works but warns), and `uv`.
- **Bedrock model access:** in the Bedrock console for your region, request access to Claude Opus 5.5.
  To use another model, pass `-c llmModel=anthropic.claude-...`.
- **SMS sandbox:** new accounts can only text verified numbers. Add the caregiver's number under
  SNS → Text messaging (SMS) → Sandbox destination phone numbers.

## Deploy (two steps)

The app needs its own public URL for OAuth metadata, and App Runner only assigns it on creation.

```bash
cd infra
export AWS_REGION=us-east-1
npx aws-cdk@2 bootstrap                                   # once per account and region

# 1. Create everything. Note the ServiceUrl output.
npx aws-cdk@2 deploy \
  -c careTz=America/New_York \
  -c caregiverPhone=+15551234567 \
  -c opsEmail=you@example.com

# 2. Tell the app its URL (same flags plus publicUrl).
npx aws-cdk@2 deploy -c careTz=America/New_York -c caregiverPhone=+15551234567 \
  -c opsEmail=you@example.com -c publicUrl=https://<id>.us-east-1.awsapprunner.com
```

The first boot seeds the demo household into DynamoDB.

**No Bedrock access yet?** Add `-c llmProvider=none` to both deploys. The app runs with
rule-based scam checks, template summaries and the keyword router in the voice simulator.
Once Bedrock works, deploy again without it to switch Claude on.

## Use it

```bash
key() { aws secretsmanager get-secret-value --secret-id "$1" --query SecretString --output text; }
key <SimulatorKeySecret>     # → open <ServiceUrl>/voice?key=<value>
key <DashboardKeySecret>     # → open <ServiceUrl>/dashboard?key=<value>
key <ServiceTokenSecret>     # → bearer token for MCP clients that skip OAuth
```

- **MCP clients:** `<McpUrl>`. A client that does OAuth discovers Cognito from
  `/.well-known/oauth-protected-resource/mcp`. Cognito has no dynamic client registration, so give
  the client `OAuthClientId` (in mcp-voice-simulator: `MCP_CLIENT_ID`) and add its redirect URL with
  `-c callbackUrls=...`. Or use the service token (`MCP_BEARER_TOKEN`).
- **Caregiver logins** (self sign-up is off):
  `aws cognito-idp admin-create-user --user-pool-id <UserPoolId> --username priya@example.com`
- **Reset the demo data:**
  `STORE=dynamodb DYNAMODB_TABLE=<TableName> AWS_REGION=us-east-1 uv run kinvesper --seed`
- **Logs:** CloudWatch → Log groups → `/aws/apprunner/kinvesper/...`

## Tests

```bash
uv run --group infra pytest infra/tests     # synthesizes the template; no AWS account needed
npx aws-cdk@2 synth                         # writes cdk.out/Kinvesper.template.json
```

## Cost and teardown

The App Runner instance runs all the time, so it is most of the bill; DynamoDB, EventBridge,
Cognito and SNS cost little at demo scale, and Secrets Manager charges per secret per month. Bedrock
bills per token. Check current prices on each service's pricing page before leaving it running.

```bash
npx aws-cdk@2 destroy
```

The table and user pool are kept by default (health data). Deploy once with `-c retainData=false`
before destroying to delete them too.
