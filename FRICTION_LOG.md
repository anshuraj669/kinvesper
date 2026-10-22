# Friction log

What got in the way while building Kinvesper (an eldercare MCP server for Alexa+ on AWS), and how
we worked around it. Severity: **High** blocked a feature, **Medium** cost hours, **Low** cost minutes.

## 1. The Alexa+ add-on can't be built without an allowlist (High)

- **Task:** connect Kinvesper's MCP server to Alexa+ as an add-on.
- **Steps:** followed the Alexa+ add-on docs, looked for the CLI and the add-on package format.
- **Expected:** a public CLI or console flow to register an MCP server, like a custom skill.
- **Actual:** the tooling isn't public. Amazon has to allowlist the AWS account first, and there
  was no way to test an add-on end to end during the hackathon.
- **Workaround:** a classic custom skill (`alexa/skill`) that forwards each request to the same
  voice agent and MCP tools, plus a browser voice simulator. The add-on package is ready in
  `alexa/addon-package` for when the allowlist opens.
- **Suggestion:** a sandbox for hackathon participants: a test-only add-on registration limited to
  the developer's own devices, or an Alexa+ simulator in the developer console that accepts an
  MCP URL.

## 2. No way to add to an Amazon Wish List outside Alexa (High)

- **Task:** "Put a warm shawl on my wish list."
- **Expected:** an API (or a Login with Amazon scope) to add items to the user's Wish List.
- **Actual:** there's no Wish List API, and Login with Amazon gives no access to it. The only
  path, Alexa Shopping Actions (`AMAZON.AddToList`), is a developer preview Amazon turns on per
  skill through a business representative.
- **Workaround:** Kinvesper finds the product, keeps it on the family dashboard with its Amazon
  link and tells the family; the skill also asks Alexa to add it, which works once the preview is
  on.
- **Suggestion:** a self-serve opt-in for Shopping Actions in the developer console, or a Wish
  List scope for Login with Amazon.

## 3. Alexa's 8-second limit vs. tool-using AI (Medium)

- **Task:** answer with Claude, which may call several MCP tools (and web search) per turn.
- **Expected:** a way to say "still working" and answer later, like the progressive response.
- **Actual:** progressive responses keep the user informed but don't extend the deadline; a slow
  turn ends the session.
- **Workaround:** the bridge answers within the limit with "Let me look into that. Say okay when
  you'd like the answer", keeps working, and gives the answer on the next turn.
- **Suggestion:** an asynchronous response for skills (deliver the answer when ready, like a
  notification tied to the session).

## 4. Alexa fetches audio and images without our headers (Medium)

- **Task:** play a family voice note and show a photo on the Echo Show.
- **Actual:** Alexa fetches `<audio>` and APL image URLs itself, with no way to add an
  authorization header; private media needs a public URL. Telegram voice notes are OGG/Opus,
  which Alexa doesn't play.
- **Workaround:** short-lived HMAC-signed `/media/<id>` links, converting voice notes to MP3 on
  the fly.
- **Suggestion:** let skills attach a token to media fetches, and document the accepted audio
  formats next to the SSML `<audio>` reference.

## 5. Federated sign-in emails aren't in Cognito access tokens (Medium)

- **Task:** know which family a Login with Amazon user belongs to, from the access token Alexa
  account linking sends.
- **Expected:** the email in the access token, or a documented claim.
- **Actual:** Cognito access tokens carry no email; federated logins needed a call to the
  `oauth2/userInfo` endpoint per token.
- **Workaround:** call userInfo once per token and cache the result for five minutes.
- **Suggestion:** an option to add `email` to access tokens (without a pre-token-generation
  Lambda), and a note in the Alexa account-linking docs about it.

## 6. App Runner idles the CPU between requests (Medium)

- **Task:** run the proactive checks (missed doses, inactivity, escalations) every minute inside
  the server.
- **Actual:** App Runner throttles the CPU when no request is in flight, so the in-process
  scheduler stopped running, and local Whisper speech-to-text took about 8 s per request.
- **Workaround:** an EventBridge rule calls `/api/checks` every 5 minutes, and speech moved to
  Amazon Transcribe (about 1 s).
- **Suggestion:** call this out on the App Runner pricing and "how it works" pages, with a
  pointer to EventBridge for background work.

## 7. Bedrock model access before the first call (Low)

- **Task:** use Claude through Amazon Bedrock.
- **Actual:** the deployed stack needs model access granted in the console before the first call,
  which a CDK deploy can't do.
- **Workaround:** the server falls back to an offline keyword router (`LLM_PROVIDER=none`) and can
  use the Claude API directly (`LLM_PROVIDER=anthropic`).
- **Suggestion:** grant access through CloudFormation or the CDK.

## 8. SMS through Amazon SNS isn't usable on a new account (High)

- **Task:** text the caregiver when a dose is missed or a scam call is caught.
- **Steps:** `sns.publish(PhoneNumber=...)` to the caregiver's +91 number.
- **Expected:** a text arrives, as with email through the same topic.
- **Actual:** a new account's SMS is in a sandbox that only texts verified numbers. The account
  can also have a $0 SMS spend limit, so adding a number fails with `AuthorizationError`.
  India (+91) needs DLT sender registration. Spoken calls through AWS End User Messaging likewise
  wait on a voice spending-limit increase.
- **Workaround:** alerts go to Telegram (free, instant, no verification) and email through SNS;
  urgent phone calls use Twilio. The SMS code stays in place, and a failed text never stops the
  email.
- **Suggestion:** one checklist for production SMS (sandbox exit, spend limit, per-country
  registration such as India's DLT), and errors that name the missing step.
