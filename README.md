# Kinvesper

A voice-first eldercare companion for Alexa+, built as a self-hosted **MCP server (protocol 2025-11-25)**.
It helps an older adult who lives alone manage medications, avoid scams and stay connected to family,
and keeps the family caregiver informed.

## Quick start

```bash
uv sync
uv run kinvesper                          # MCP endpoint at http://127.0.0.1:8000/mcp
                                          # voice simulator at http://127.0.0.1:8000/voice
                                          # caregiver dashboard at http://127.0.0.1:8000/dashboard
uv run python scripts/demo_client.py      # in another terminal: walk through demo scenarios
uv run pytest                             # unit + end-to-end MCP tests
```

The first run seeds a demo household (Margaret, 78, and her daughter Priya) with a week of dose
history. Reset it any time with `uv run kinvesper --seed`.

Inspect it interactively with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector      # transport: Streamable HTTP, URL: http://127.0.0.1:8000/mcp
```

## MCP surface

| Tools | What it does |
|---|---|
| `get_medication_schedule` | Today's doses with status (taken / due / missed / upcoming); ask about one med by name or purpose |
| `log_medication_dose` | Logs a dose, with a **double-dose guard** |
| `add_medication` | Adds a prescription |
| `check_for_scam` | Elder-fraud red-flag check; alerts family on high risk |
| `daily_check_in` | Mood and symptoms; escalates falls, dizziness and chest pain |
| `notify_caregiver` | Sends a message to the caregiver |
| `emergency_help` | Sends an urgent alert immediately |
| `get_care_summary` | Adherence stats plus a written weekly summary |
| `request_pharmacy_refill` | Orders a refill; runs as an async **task** with progress updates |

**Resources:** `care://profile`, `care://medications`, `care://today`, `care://alerts`, `care://refills`.
**Prompts:** `morning_check_in`, `weekly_caregiver_report`.
Every tool has annotations, a title, an icon and an `outputSchema`, and returns `structuredContent`.

**2025-11-25 spec features:**

- **Tasks:** `request_pharmacy_refill` declares `execution.taskSupport: "optional"`. Call it with a
  `task` field and poll `tasks/get` to watch it move from requested to ready. A plain call finishes at once.
- **Elicitation:** `notify_caregiver` asks the person to confirm before it messages family. Clients
  without elicitation send as asked. Scam and emergency alerts never ask, because scammers push secrecy.
- **Icons:** on the server, every tool, every resource and every prompt.
- **OAuth 2.1:** set `OAUTH_ISSUER` (Amazon Cognito) and `/mcp` requires a bearer JWT. The server
  publishes `/.well-known/oauth-protected-resource/mcp` and answers 401 with a `WWW-Authenticate`
  challenge. It checks signature, expiry, issuer, audience and scopes.

## Voice simulator

Open `/voice` in Chrome or Edge to talk to Kinvesper the way you would to Alexa+: tap the mic
(Web Speech API for speech-to-text and text-to-speech) or type. Each reply shows the MCP tool calls
behind it. Scenario buttons cover the demo flows.

- **Brain:** with `LLM_PROVIDER=bedrock` or `anthropic`, Claude runs a tool-use loop over the tools
  it discovers from `/mcp`. With `LLM_PROVIDER=none`, a keyword router picks one tool per utterance,
  so the demo works offline.
- **Real MCP client:** every conversation opens its own Streamable HTTP session to `/mcp`, using
  `SERVICE_TOKEN` when OAuth is on.
- **Elicitation by voice:** "Tell Priya…" makes the server ask "Send this to Priya?". The question is
  spoken, and your next answer ("yes" / "no") goes back as the elicitation response.
- **Tasks by voice:** a refill runs as an MCP task. The page announces the result when it finishes,
  and Claude hears about it on the next turn.
- **Access:** open when OAuth is off. With OAuth on it needs `SERVICE_TOKEN` and `SIMULATOR_KEY`
  (`/voice?key=...`), otherwise it is switched off.

### Testing with an external Alexa+ simulator

Community simulators such as [mcp-voice-simulator](https://github.com/AlSayedGamal/alexa-ai-web-simulator)
act as Alexa+'s MCP client. To reach Kinvesper from another machine or a hosted tool, open a tunnel
and set `PUBLIC_URL` to it. Hosts not in `PUBLIC_URL` are refused (421) by DNS-rebinding protection.

```bash
cloudflared tunnel --url http://127.0.0.1:8000        # or: ngrok http 8000
PUBLIC_URL=https://<tunnel-host> uv run kinvesper    # MCP URL: https://<tunnel-host>/mcp
```

To skip account linking while OAuth is on, give the simulator `SERVICE_TOKEN` as its bearer token
(`MCP_BEARER_TOKEN` in mcp-voice-simulator).

## Caregiver dashboard and proactive checks

`/dashboard` is the family caregiver's view: 7-day adherence, today's doses, alerts (with
acknowledge), mood at check-in, medications with low-stock warnings and pharmacy refills. It
refreshes every 15 seconds, so doses logged by voice show up on their own.

A scheduler inside the server runs every `SCHEDULER_INTERVAL_S` seconds:

| Check | Alert | Texted |
|---|---|---|
| Dose not logged `MISSED_DOSE_GRACE_MIN` after its time | warning | yes |
| No interaction for `INACTIVITY_HOURS` (08:00-21:00 only) | warning | yes |
| `LOW_STOCK_THRESHOLD` pills or fewer, no refill underway | info | no |

Every alert carries a de-duplication key, so a missed dose texts the caregiver once, however
often the checks run. `uv run kinvesper --check` runs them once, for cron or EventBridge.
With OAuth on, the dashboard needs `DASHBOARD_KEY` (`/dashboard?key=...`).

## Deploying to AWS

`infra/` holds a CDK stack that deploys Kinvesper on App Runner with DynamoDB, Bedrock, Cognito
(OAuth 2.1), SNS, Secrets Manager, EventBridge and CloudWatch. Steps: [infra/README.md](infra/README.md).

```bash
docker build -t kinvesper .            # the same image App Runner runs
docker run -p 8080:8080 kinvesper      # http://127.0.0.1:8080/voice
```

## Configuration

See `.env.example`. Everything runs offline with `LLM_PROVIDER=none` (rule-based scam detection
and template summaries). Set `LLM_PROVIDER=bedrock` to use Claude on Amazon Bedrock, or
`LLM_PROVIDER=anthropic` for the Claude API.

To use a free local model instead, run [Ollama](https://ollama.com) (it speaks the same Messages API):

```bash
ollama pull llama3.1
LLM_PROVIDER=ollama uv run kinvesper     # LLM_MODEL=<any tool-capable model>, OLLAMA_URL=... to change
```

## Layout

```
src/kinvesper/
  app.py          HTTP entry point (Starlette + uvicorn), /mcp and /health
  mcp_server.py   MCP tools, resources, prompts, task-augmented tool calls
  auth.py         OAuth 2.1 resource server: JWT verification against Cognito
  icons.py        SVG icons for tools, resources and prompts
  voice.py        voice agent: Claude tool-use loop over MCP, offline router, spoken elicitation
  web.py          voice simulator routes (/voice, /api/voice/...)
  dashboard.py    caregiver dashboard routes (/dashboard, /api/dashboard, ...)
  scheduler.py    proactive checks on a timer (missed doses, inactivity, low stock)
  static/         simulator and dashboard pages
  service.py      care logic (schedule, dose guard, alerts, check-ins, summaries)
  scam.py         fraud red-flag rules + optional Claude refinement
  store.py        JSON-file or DynamoDB document store
  llm.py          Claude via Bedrock (Mantle) or the Claude API
  notify.py       caregiver SMS via Amazon SNS
  seed.py         demo household
```

## Roadmap

Details and the AWS services per phase are in [PHASES.md](PHASES.md).

- [x] **Phase 1:** core MCP server, tools, resources and prompts, tests
- [x] **Phase 2:** Tasks (async pharmacy refill), elicitation (confirm before messaging family), icons, OAuth 2.1
- [x] **Phase 3:** Alexa+-style voice simulator web app with a Claude agent
- [x] **Phase 4:** caregiver dashboard, missed-dose and inactivity scheduler, SNS SMS
- [ ] **Phase 5:** AWS deployment (DynamoDB, Docker, CDK). Built and tested locally; `cdk deploy` pending
- [ ] **Phase 6:** demo polish (architecture diagram, spec-compliance table, demo script)

## License

[MIT](LICENSE)
