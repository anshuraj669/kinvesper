"""CDK entry point. See infra/README.md for the deploy steps.

Context (cdk.json, or -c key=value):
  publicUrl       https://<id>.<region>.awsapprunner.com, from the first deploy's output
  careTz          the patient's IANA timezone (dose times are local to it)
  caregiverPhone  E.164 number subscribed to the alerts topic by SMS
  opsEmail        email for the 5xx alarm
  llmProvider     "bedrock" (default), "ollama" (needs ollamaUrl) or "none" (rule-based, no LLM)
  ollamaUrl       public URL of an Ollama server, e.g. a cloudflared tunnel to your laptop
  llmModel        Bedrock model ID
  callbackUrls    comma-separated OAuth redirect URLs for MCP clients
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
    ops_email=ctx("opsEmail") or "",
    llm_provider=ctx("llmProvider") or "bedrock",
    ollama_url=ctx("ollamaUrl") or "",
    llm_model=ctx("llmModel") or "anthropic.claude-opus-5-5",
    callback_urls=[u for u in (ctx("callbackUrls") or "").split(",") if u],
    retain_data=str(ctx("retainData")).lower() != "false",
    description="Kinvesper: voice-first eldercare MCP server for Alexa+",
)
app.synth()
