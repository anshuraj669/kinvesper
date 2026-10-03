# Kinvesper: Project Phases

This is the build plan for Kinvesper, split into phases. Each phase ships something that runs on its
own, has a clear exit check, and leaves `uv run pytest` green. Phases run in order. Later phases
depend on earlier ones.

| Phase | Theme | Status |
|---|---|---|
| 1 | Core MCP server | ✅ Done |
| 2 | Advanced MCP spec features (Tasks, elicitation, icons, OAuth 2.1) | ✅ Done |
| 3 | Alexa+-style voice simulator | ✅ Done |
| 4 | Caregiver dashboard and proactive scheduler | ✅ Done |
| 5 | AWS deployment | 🟡 Built, not yet deployed |
| 6 | Demo polish and submission | ⬜ |

---

## AWS services

| Service | Role in Kinvesper | Phase | Priority |
|---|---|---|---|
| **Amazon Bedrock** | Claude for conversation, scam refinement and weekly summaries (`llm.py`) | 1, 3 | Core |
| **Amazon DynamoDB** | Single-table store for profile, meds, doses, alerts, check-ins, refills (`store.py`) | 1, 5 | Core |
| **Amazon SNS** | SMS alerts to the caregiver (`notify.py`) | 4 | Core |
| **Amazon Cognito** | OAuth 2.1 authorization server protecting `/mcp` | 2, 5 | Core |
| **AWS App Runner** (or ECS Fargate) | Hosts the MCP server container | 5 | Core |
| **Amazon ECR** | Stores the Docker image | 5 | Supporting |
| **AWS Secrets Manager** | API keys, service token, phone numbers | 5 | Supporting |
| **Amazon EventBridge Scheduler** | Triggers missed-dose, inactivity and refill checks | 4, 5 | Supporting |
| **Amazon CloudWatch** | Logs, metrics, alarms on `/health` | 5 | Supporting |
| **AWS CDK** | All infrastructure as code, one `cdk deploy` | 5 | Supporting |
| **AWS IAM** | Least-privilege role: Bedrock invoke, DynamoDB, SNS publish | 5 | Supporting |
| **Amazon S3 + CloudFront** | Static hosting for the dashboard and voice simulator | 5 | Nice to have |
| **Amazon Polly** | Natural speech in the voice simulator | 3 | Nice to have |
| **Amazon Transcribe** | More accurate speech-to-text in the simulator | 3 | Nice to have |
| **Amazon SES** | Weekly caregiver report by email | 4 | Nice to have |
| **Amazon Comprehend Medical** | Extract medications and symptoms from free-form check-ins | 4 | Nice to have |

Minimum solid setup for the hackathon: **Bedrock + DynamoDB + SNS + Cognito + App Runner**.

---

## Phase 1: Core MCP server ✅

**Goal:** a working MCP server (protocol 2025-11-25) that covers the three core care jobs:
medications, scam protection and staying in touch with family.

**Delivered**
- Streamable HTTP endpoint at `/mcp` and a `/health` check (`app.py`).
- 8 tools, each with a title, annotations, an `outputSchema` and `structuredContent` (`mcp_server.py`):
  `get_medication_schedule`, `log_medication_dose` (with the double-dose guard), `add_medication`,
  `check_for_scam`, `daily_check_in`, `notify_caregiver`, `emergency_help`, `get_care_summary`.
- Resources `care://profile`, `care://medications`, `care://today` and `care://alerts`. Prompts
  `morning_check_in` and `weekly_caregiver_report`.
- Care logic in `service.py`, rule-based scam detection with optional Claude refinement in `scam.py`,
  and a JSON/DynamoDB store (`store.py`).
- A seeded demo household (Margaret, 78, and her daughter Priya) with a week of dose history (`seed.py`).
- Works fully offline with `LLM_PROVIDER=none`.
- Unit tests and end-to-end MCP tests, plus `scripts/demo_client.py`.

**Exit check:** `uv run pytest` passes, and the MCP Inspector can list and call every tool.

---

## Phase 2: Advanced MCP spec features ✅

**Goal:** show the newer parts of the 2025-11-25 spec in real care scenarios, not only as checklist items.

**Scope**
- **Tasks (async):** a `request_pharmacy_refill` tool that runs as a long-running task. The client
  polls task status (working, then completed) and gets the result at the end. This uses the
  experimental Tasks API that ships in `mcp==1.30.0`. The version is pinned for that reason.
- **Elicitation:** before `notify_caregiver` sends a message, ask the user to confirm with a
  structured form ("Send this to Priya? yes/no"). If the client doesn't support elicitation, send
  as asked. Scam alerts and `emergency_help` stay automatic on purpose: scammers push victims to
  keep secrets, so the person is never asked whether family should be told.
- **Icons:** add icons to the server, the tools, the resources and the prompts.
- **OAuth 2.1:** protected-resource metadata, and bearer-token validation with PyJWT against a
  Cognito issuer (`OAUTH_ISSUER`, `OAUTH_AUDIENCE` and `OAUTH_SCOPES` in `config.py`). Auth stays
  off when the issuer is empty, so local development still works.

**Exit check:** tests cover the task lifecycle, both elicitation paths (accept and decline) and
rejection of a request without a token. The Inspector shows the icons and runs the refill task.

**AWS:** Amazon Cognito (user pool, app client, resource server with a `kinvesper/mcp` scope).

---

## Phase 3: Alexa+-style voice simulator ✅

**Goal:** let judges hear and see the real experience without an Alexa device.

**Scope**
- A small web app served by the same Starlette process. It uses the browser's speech-to-text and
  text-to-speech (Web Speech API) and shows a chat transcript.
- A Claude agent (Bedrock or the Claude API, through `llm.py`) that acts as the Alexa+ brain. It
  connects to `/mcp` as an MCP client, using the simulator bearer token when OAuth is on.
- Visible tool calls in the UI, so viewers can see which MCP tool each spoken request triggered.
- Scripted scenario buttons: "Did I take my pills?", a scam phone call, "I feel dizzy", and a refill request.
- Spoken confirmations: the server's elicitation ("Send this to Priya?") is read aloud and the next
  answer ("yes" / "no") is sent back. Task results (refills) are announced when they finish.
- Offline mode: with `LLM_PROVIDER=none`, a keyword router picks the tool, so the demo needs no credentials.

**Exit check:** each scenario works end to end by voice in Chrome, and the same flows work typed
when there is no microphone.

**AWS:** Amazon Bedrock (the agent's model). Optional: Amazon Polly (voice), Amazon Transcribe
(speech-to-text).

---

## Phase 4: Caregiver dashboard and proactive scheduler ✅

**Goal:** close the loop for the family caregiver, and make Kinvesper act on its own instead of
only when someone asks.

**Scope**
- **Caregiver dashboard:** today's doses, a 7-day adherence chart, recent alerts, check-in mood
  history and a medication list with low-stock warnings. It is backed by a small JSON API on top of `service.py`.
- **Background scheduler** (`SCHEDULER_INTERVAL_S`):
  - missed-dose detection, which alerts the caregiver after a grace period
  - inactivity detection (no check-in or dose logged by a set time)
  - low-stock refill reminders
- **SMS delivery** through Amazon SNS (`notify.py`), either a topic or a direct number. With no
  settings it only logs.
- De-duplicated alerts, so one missed dose doesn't cause repeated texts.
- `kinvesper --check` runs the checks once, so EventBridge can drive them in Phase 5 (set
  `SCHEDULER_INTERVAL_S=0` when running more than one instance).
- With OAuth on, the dashboard needs `DASHBOARD_KEY`, because it shows health data.

**Exit check:** move the clock forward in a test and see the missed-dose alert fire exactly once.
The dashboard reflects doses logged from the voice simulator.

**AWS:** Amazon SNS (SMS), Amazon EventBridge Scheduler (periodic checks once deployed). Optional:
Amazon SES (weekly email report), Amazon Comprehend Medical (symptom extraction).

---

## Phase 5: AWS deployment 🟡

**Goal:** a public, secure deployment that Alexa+ (or any remote MCP client) can reach.

**Scope**
- A Dockerfile (uv-based and slim) with the health check wired to `/health`.
- AWS CDK stack: the container on ECS Fargate or App Runner, a DynamoDB single table (`pk`/`sk`
  as in `store.py`), a Cognito user pool for OAuth, an SNS topic, and an IAM role limited to
  Bedrock invoke, DynamoDB and SNS publish.
- Config through environment variables and Secrets Manager, with no secrets in the image.
- A seed script that loads the demo household into DynamoDB.

**Built:** Dockerfile (non-root, health check), CDK stack in `infra/` (App Runner, ECR, DynamoDB,
Cognito, SNS, Secrets Manager, EventBridge, CloudWatch alarm, least-privilege IAM), 10 template
tests, and a deploy guide in `infra/README.md`. Changes from the plan: App Runner instead of
Fargate, with exactly one instance (sessions live in memory); EventBridge calls `/api/checks`
every 5 minutes because App Runner idles the CPU between requests. **Not done yet:** a real
`cdk deploy`, which needs an AWS account.

**Exit check:** `cdk deploy` from a clean account works, the public `/mcp` URL passes an Inspector
session with OAuth, and state persists across container restarts.

**AWS:** AWS App Runner or ECS Fargate, Amazon ECR, Amazon DynamoDB, Amazon Cognito, Amazon SNS,
AWS Secrets Manager, Amazon CloudWatch, AWS IAM, AWS CDK. Optional: Amazon S3 + CloudFront.

---

## Phase 6: Demo polish and submission

**Goal:** make the project easy to judge in 3 minutes.

**Scope**
- An architecture diagram (Alexa+ or the simulator, then MCP, then CareService, then DynamoDB, SNS and Bedrock).
- A spec-compliance table that maps each MCP 2025-11-25 feature to where it is used.
- A demo script and a recorded video that follows Margaret's day: morning check-in, the pills,
  the scam call, a dizzy spell that alerts Priya, and the weekly report.
- A final README pass, screenshots, a license, and cleanup (remove `.idea/` and `__pycache__`, and
  check `.gitignore`).

**Exit check:** someone new can clone the repo, follow the README and run the demo locally in
under 5 minutes.
