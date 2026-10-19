# Kinvesper on Alexa+

Alexa+ reaches Kinvesper through an **MCP add-on**: Alexa+ does the listening, understanding and
speaking, and calls Kinvesper's MCP tools at `https://<service>/mcp` with the person's linked account.

```
addon-package/addon.json   store listing + MCP endpoint, deployed with `alexa-ai deploy`
make_media.py              draws the store icons and carousel images (served at /addon/*.png)
```

## What the server already provides

| Alexa+ requirement | Kinvesper |
|---|---|
| Streamable HTTP, MCP 2025-11-25 | `/mcp` |
| `401` without a token | yes, with a `WWW-Authenticate` pointing at the metadata |
| `/.well-known/oauth-protected-resource` at the root | yes (also at `/.well-known/oauth-protected-resource/mcp`) |
| Authorization server metadata with PKCE `S256` | `/.well-known/oauth-authorization-server`. Cognito's own document doesn't list S256, so Kinvesper serves Cognito's endpoints under its own identifier |
| Static OAuth client with a secret, Authorization Code grant | the `AlexaClient` Cognito app client (deploy with `-c alexaVendorId=...`) |
| All regional Alexa redirect URLs registered | `pitangui.amazon.com`, `layla.amazon.com`, `alexa.amazon.co.jp`, `alexa.amazon.com` |
| Privacy policy and terms of use | `/privacy`, `/terms` |
| Store icons (6 sizes) and carousel images | `/addon/icon-<size>.png`, `/addon/carousel-<n>.png` |

## 1. Get access to the Alexa AI CLI

The add-on tooling isn't public yet. Amazon allowlists an AWS account, then the CLI is installed
from Amazon's private npm registry. Ask on the hackathon Discord or at office hours, giving your
AWS account ID (`aws sts get-caller-identity --query Account --output text`).

Until then, `aws sts assume-role --role-arn arn:aws:iam::372468808636:role/AddOn3PDeveloperToolsRead
--role-session-name check` fails with `AccessDenied`. Once it succeeds, follow
[Set up your development environment](https://developer.amazon.com/docs/alexaplus/add-ons/set-up-your-development-environment.html):

```bash
aws configure --profile alexa-ai-user            # an IAM user allowed to sts:AssumeRole the role above
aws configure set profile.alexa-ai.role_arn arn:aws:iam::372468808636:role/AddOn3PDeveloperToolsRead
aws configure set profile.alexa-ai.source_profile alexa-ai-user
aws configure set profile.alexa-ai.region us-west-2
aws codeartifact login --tool npm --domain alexa-ai --repository npm-packages \
    --domain-owner 372468808636 --region us-west-2 --namespace @alexa-ai --profile alexa-ai
nvm use 24                                       # the CLI needs Node.js 24+
npm install -g @alexa-ai/cli                     # only after the codeartifact login: the
alexa-ai configure                               # unscoped "alexa-ai" npm package is unrelated
```

## 2. Deploy the add-on

```bash
cd alexa
alexa-ai deploy                                  # prints the add-on ID
```

If the CLI's manifest format differs, scaffold a fresh one and copy the store listing across:
`alexa-ai new mcp --name Kinvesper --locale en-US --mcp-server-url https://<service>/mcp --requires-auth`.

## 3. Account linking

1. Find your **vendor ID** (the last part of the Alexa redirect URLs the developer console or CLI
   shows, e.g. `https://pitangui.amazon.com/api/skill/link/M3IHEJ1OZMSGZ3`).
2. Add the Alexa OAuth client: redeploy the stack with `-c alexaVendorId=<vendor ID>` (see
   [infra/README.md](../infra/README.md)). The output `AlexaClientId` is the client ID; the secret:
   ```bash
   aws cognito-idp describe-user-pool-client --user-pool-id <UserPoolId> --client-id <AlexaClientId> \
       --query UserPoolClient.ClientSecret --output text
   ```
3. Give them to Alexa (the secret is asked for in a masked prompt):
   ```bash
   alexa-ai configure-account-linking --addon-id <add-on ID> --config-file account-linking.json
   alexa-ai deploy
   ```
   Authorization URL, token URL and scopes (`openid email kinvesper/mcp`) come from the server's metadata.
   `email` lets a Google login link only when it's one of the family's emails.
4. Create the login the person signs in with when the Alexa app asks to link Kinvesper:
   ```bash
   make login EMAIL=margaret@example.com
   ```

## 4. Test

- `alexa-ai test` and the web simulator in the [developer console](https://developer.amazon.com/alexa/console/ask/addons).
- On an Echo: the add-on is US-only, so the Alexa app account must be on the US marketplace.
- Without Alexa+ access, the built-in voice simulator (`/voice`) runs the same tools the same way.

## Classic skill bridge (works on any Echo today)

While the add-on is allowlisted, a classic custom skill reaches Kinvesper from Echo devices, the
Alexa app and the developer console's simulator:

```
"Alexa, open kin vesper" → "did I take my pills?"
  → custom skill (catch-all intent, alexa/skill) → POST /alexa on Kinvesper
  → the voice agent (same as /voice) → the MCP tools → spoken reply
```

- `skill/skill-package/`: the skill manifest and the interaction model (one `CatchAllIntent` whose
  slot carries the whole phrase, plus Yes, No, Help, Stop and Cancel) for en-US, en-IN, en-GB,
  en-CA and en-AU.
- `/alexa` checks every request as Amazon requires (signing certificate chain, signature, a
  timestamp within 150 s, the skill ID) and is off until the stack is deployed with
  `-c alexaSkillId=amzn1.ask.skill...`.
- With Claude behind it (`llmProvider=anthropic` or `bedrock`), anything else said in the skill is
  answered too: facts, recipes, maths, stories, and with the Claude API, weather and news through
  web search. With `llmProvider=none`, only the care phrases the keyword router knows work.
- An answer that misses Alexa's 8-second limit isn't lost: Alexa says "Let me look into that. Say
  okay when you'd like the answer", and the next thing the person says gets it.
- Yes and No answer Kinvesper's confirmation questions; Stop or Cancel during "Send this to
  Priya?" cancels the message. "Alexa, ask kin vesper ..." answers and ends the session.

- **Echo reminders:** "set up my pill reminders" (`SetUpRemindersIntent`) asks for the reminders
  permission by voice (`Connections.SendRequest` AskFor), then creates one daily reminder per dose
  time with the Alexa Reminders API. The medicine list stays in Kinvesper, so when it changes in
  Settings, the reminders are rebuilt the next time the skill opens. "Turn off my reminders"
  (`RemoveRemindersIntent`) deletes them. Test on an Echo or the Alexa app: the console simulator
  doesn't play reminders.
- **One-off reminders and alarms:** "remind me to call Priya at 5" or "set an alarm for my doctor's
  appointment" go through the catch-all to Claude, which saves them with the `set_reminder` tool.
  The bridge then puts them on the Echo (asking for the reminders and timers permissions by voice
  the first time). An important alarm gets a heads-up reminder 30 minutes before and, when it's
  due within 2 hours, a timer that rings until "Alexa, stop".
- **Echo Show:** with the APL interface on, every reply also shows today's medicines in large print,
  with a "Taken" button (an `Alexa.Presentation.APL.UserEvent`) for each dose that's due. Turn on
  **Device Display** in the console's Test tab to see it there.
- **Family messages and the daily check-in:** messages typed in the dashboard are read out when the
  skill opens. With reminders allowed, the Echo also asks "please let me know you're okay" daily at
  the check-in time from Settings.
- **Hindi (hi-IN):** the same intents with Hindi samples. The bridge speaks Hindi for its own
  phrases, and Claude answers in Hindi (tool results are translated, so care answers take one more
  model call).

Set it up in the [developer console](https://developer.amazon.com/alexa/console/ask): create a
custom skill, paste `interactionModels/custom/en-US.json` into **Interaction Model → JSON Editor**,
build it, and set **Endpoint → HTTPS** to `https://<service>/alexa` ("My development endpoint is a
sub-domain of a domain that has a wildcard certificate": App Runner serves `*.<region>.awsapprunner.com`,
and with the "trusted certificate" option Alexa fails the TLS check and says "unable to reach the requested skill"). Then:

1. **Permissions** (left menu, under Tools): turn on **Reminders** and **Timers**.
1. **Interfaces** (left menu, under Build): turn on **Alexa Presentation Language** (tick the Echo
   Show hub sizes) and save, then **Build skill**. Without it, Echo Show devices get voice only.
2. **Hindi:** in the language dropdown at the top left, **Language settings → Add new language →
   Hindi (IN)**, save, then switch to Hindi, paste `interactionModels/custom/hi-IN.json` into its
   JSON Editor, build, and set the same endpoint. Test it by switching the Test tab to Hindi.

3. **Account linking** (optional, under Tools): the family links their Kinvesper login in the Alexa
   app before the skill answers. Needs the stack deployed with `-c alexaVendorId=<vendor ID>`.
   - **Auth Code Grant**, "Allow users to enable skill without account linking" **off**.
   - Web Authorization URI: stack output `OAuthLogin` + `/oauth2/authorize`; Access Token URI:
     `OAuthLogin` + `/oauth2/token`.
   - Client ID: `AlexaClientId`; Client Secret: from `describe-user-pool-client` (section 3 above);
     Authentication Scheme: **HTTP Basic**.
   - Scopes: `openid`, `email`, `kinvesper/mcp`.
   - Save, then redeploy the stack with `-c alexaAccountLinking=true`. In the Alexa app: **Skills →
     Kinvesper → Link account**, and sign in with a login from `make login` (or Google).

Or with the ASK CLI (`npm install -g ask-cli`, `ask configure`), which deploys all of it:

```bash
cd alexa/skill && ask deploy
```
