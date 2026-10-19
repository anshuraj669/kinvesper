"""CDK entry point. See infra/README.md for the deploy steps.

Context (cdk.json, or -c key=value):
  publicUrl       https://<id>.<region>.awsapprunner.com, from the first deploy's output
  careTz          the patient's IANA timezone (dose times are local to it)
  caregiverPhone  E.164 number subscribed to the alerts topic by SMS
  caregiverEmail  email subscribed to the alerts topic (confirm the link SNS sends)
  opsEmail        email for the 5xx alarm
  llmProvider     "bedrock" (default), "ollama" (needs ollamaUrl) or "none" (rule-based, no LLM)
  ollamaUrl       public URL of an Ollama server, e.g. a cloudflared tunnel to your laptop
  llmModel        Bedrock model ID
  callbackUrls    comma-separated OAuth redirect URLs for MCP clients
  alexaVendorId   your Alexa developer vendor ID: adds the Alexa+ account-linking OAuth client
  alexaSkillId    ID of the classic Alexa skill (alexa/skill): turns on the /alexa bridge
  voiceCallFrom   a voice-capable phone number or pool ID in AWS End User Messaging: urgent alerts ring the caregiver
  googleClientId  Google OAuth client ID: adds "Continue with Google" to the login (secret: scripts/set_google_login.sh)
  caregiverLogins comma-separated emails that may sign in with Google, besides the caregiver's and backup's
  alexaAccountLinking  "true": the Alexa skill answers only once a family login is linked in the Alexa app
  retainData      "false" deletes the table and user pool with the stack
"""

import aws_cdk as cdk

from kinvesper_stack import KinvesperStack

app = cdk.App()
ctx = app.node.try_get_context
KinvesperStack(
    app,
    "Kinvesper",
    public_url=ctx("publicUrl") or "",
    care_tz=ctx("careTz") or "UTC",
    caregiver_phone=ctx("caregiverPhone") or "",
    caregiver_email=ctx("caregiverEmail") or "",
    ops_email=ctx("opsEmail") or "",
    llm_provider=ctx("llmProvider") or "bedrock",
    ollama_url=ctx("ollamaUrl") or "",
    llm_model=ctx("llmModel") or "anthropic.claude-opus-5-5",
    callback_urls=[u for u in (ctx("callbackUrls") or "").split(",") if u],
    alexa_vendor_id=ctx("alexaVendorId") or "",
    alexa_skill_id=ctx("alexaSkillId") or "",
    voice_call_from=ctx("voiceCallFrom") or "",
    google_client_id=ctx("googleClientId") or "",
    caregiver_logins=ctx("caregiverLogins") or "",
    alexa_account_linking=str(ctx("alexaAccountLinking")).lower() == "true",
    retain_data=str(ctx("retainData")).lower() != "false",
    description="Kinvesper: voice-first eldercare MCP server for Alexa+",
)
app.synth()
