"""Infrastructure checks on the synthesized CloudFormation template (no AWS account needed).

    uv run --group infra pytest infra/tests
"""

import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kinvesper_stack import KinvesperStack  # noqa: E402


def synth(**kwargs) -> Template:
    app = cdk.App()
    stack = KinvesperStack(app, "Test", env=cdk.Environment(account="123456789012", region="us-east-1"), **kwargs)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    return synth(public_url="https://abc.us-east-1.awsapprunner.com", caregiver_phone="+15551234567")


def _env(template, kind="RuntimeEnvironmentVariables"):
    svc = next(iter(template.find_resources("AWS::AppRunner::Service").values()))
    return {e["Name"]: e["Value"] for e in svc["Properties"]["SourceConfiguration"]["ImageRepository"]["ImageConfiguration"][kind]}


def test_app_runner_runs_one_instance_with_health_check(template):
    template.has_resource_properties("AWS::AppRunner::AutoScalingConfiguration", {"MinSize": 1, "MaxSize": 1})
    template.has_resource_properties("AWS::AppRunner::Service", {
        "HealthCheckConfiguration": Match.object_like({"Protocol": "HTTP", "Path": "/health"}),
    })


def test_cloud_configuration(template):
    env = _env(template)
    assert env["STORE"] == "dynamodb"
    assert env["LLM_PROVIDER"] == "bedrock"
    assert env["SCHEDULER_INTERVAL_S"] == "0"
    assert env["STT_PROVIDER"] == "transcribe"
    assert env["OAUTH_SCOPES"] == "kinvesper/mcp"
    assert env["PUBLIC_URL"] == "https://abc.us-east-1.awsapprunner.com"


def test_secrets_never_plaintext(template):
    secrets = _env(template, "RuntimeEnvironmentSecrets")
    assert set(secrets) == {"SERVICE_TOKEN", "SIMULATOR_KEY", "DASHBOARD_KEY", "TWILIO_CREDENTIALS", "TELEGRAM_BOT_TOKEN"}
    assert all("Ref" in v for v in secrets.values())  # secret ARNs, resolved by App Runner
    assert "SERVICE_TOKEN" not in _env(template)


def test_table_keys_and_backups(template):
    template.has_resource_properties("AWS::DynamoDB::GlobalTable", {
        "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        "Replicas": [Match.object_like({"PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True}})],
    })
    template.has_resource("AWS::DynamoDB::GlobalTable", {"DeletionPolicy": "Retain"})


def test_instance_role_least_privilege(template):
    policies = template.find_resources("AWS::IAM::Policy")
    actions = set()
    for p in policies.values():
        for st in p["Properties"]["PolicyDocument"]["Statement"]:
            acts = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
            actions.update(acts)
    assert "bedrock-mantle:CreateInference" in actions
    assert "sns:Publish" in actions
    assert "polly:SynthesizeSpeech" in actions
    assert "transcribe:StartStreamTranscription" in actions
    assert "sms-voice:SendVoiceMessage" in actions
    assert not any(a.endswith(":*") or a == "*" for a in actions)


def test_app_manages_the_caregiver_email_subscription(template):
    statements = [st for p in template.find_resources("AWS::IAM::Policy").values()
                  for st in p["Properties"]["PolicyDocument"]["Statement"]]
    email = next(st for st in statements if st.get("Sid") == "CaregiverEmail")
    assert set(email["Action"]) == {"sns:Subscribe", "sns:Unsubscribe", "sns:ListSubscriptionsByTopic"}
    assert email["Resource"] != "*"
    sms = next(st for st in statements if st.get("Sid") == "CaregiverSms")
    assert sms["Action"] == "sns:Publish"


def test_oauth_client_is_public_pkce(template):
    template.has_resource_properties("AWS::Cognito::UserPoolClient", {
        "GenerateSecret": False,
        "AllowedOAuthFlows": ["code"],
        "AllowedOAuthScopes": Match.array_with(["openid"]),
    })
    template.has_resource_properties("AWS::Cognito::UserPoolResourceServer", {"Identifier": "kinvesper"})


def test_checks_run_every_five_minutes_with_secret_key(template):
    template.has_resource_properties("AWS::Events::Rule", {"ScheduleExpression": "rate(5 minutes)"})
    conn = next(iter(template.find_resources("AWS::Events::Connection").values()))
    key = conn["Properties"]["AuthParameters"]["ApiKeyAuthParameters"]
    assert key["ApiKeyName"] == "X-Dashboard-Key"
    assert "resolve:secretsmanager" in str(key["ApiKeyValue"])


def test_sms_subscription_only_when_phone_given(template):
    template.resource_count_is("AWS::SNS::Subscription", 1)
    synth().resource_count_is("AWS::SNS::Subscription", 0)


def test_email_subscription_when_email_given():
    t = synth(caregiver_email="care@example.com")
    t.resource_count_is("AWS::SNS::Subscription", 1)
    t.has_resource_properties("AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "care@example.com"})


def test_first_deploy_without_public_url():
    assert "PUBLIC_URL" not in _env(synth())


def _actions(template) -> set[str]:
    actions = set()
    for p in template.find_resources("AWS::IAM::Policy").values():
        for st in p["Properties"]["PolicyDocument"]["Statement"]:
            actions.update(st["Action"] if isinstance(st["Action"], list) else [st["Action"]])
    return actions


def test_llm_provider_can_be_switched_off():
    off = synth(llm_provider="none")
    assert _env(off)["LLM_PROVIDER"] == "none"
    assert "bedrock-mantle:CreateInference" not in _actions(off)


def test_ollama_provider_without_bedrock():
    local = synth(llm_provider="ollama", ollama_url="https://gpu.example.com")
    env = _env(local)
    assert env["LLM_PROVIDER"] == "ollama"
    assert env["OLLAMA_URL"] == "https://gpu.example.com"
    assert "LLM_MODEL" not in env
    assert not any(a.startswith("bedrock") for a in _actions(local))


def test_claude_api_key_comes_from_secrets_manager():
    cloud = synth(llm_provider="anthropic")
    env = _env(cloud)
    assert env["LLM_PROVIDER"] == "anthropic"
    assert "LLM_MODEL" not in env  # the Bedrock model ID default doesn't apply; the app picks its own
    assert "ANTHROPIC_API_KEY" in _env(cloud, "RuntimeEnvironmentSecrets")
    assert "ANTHROPIC_API_KEY" not in env
    assert not any(a.startswith("bedrock") for a in _actions(cloud))
    assert "ClaudeApiKeySecret" in cloud.find_outputs("*")
    assert _env(synth(llm_provider="anthropic", llm_model="claude-sonnet-5-5"))["LLM_MODEL"] == "claude-sonnet-5-5"
    assert "ClaudeApiKeySecret" not in synth(llm_provider="none").find_outputs("*")


def test_destroy_mode():
    synth(retain_data=False).has_resource("AWS::DynamoDB::GlobalTable", {"DeletionPolicy": "Delete"})


def _clients(template) -> dict:
    """App clients by their CDK name (McpClient, AlexaClient, DashboardClient)."""
    found = template.find_resources("AWS::Cognito::UserPoolClient")
    return {next(n for n in ("McpClient", "AlexaClient", "DashboardClient") if n in k): (k, v["Properties"]) for k, v in found.items()}


def test_alexa_client_only_with_vendor_id(template):
    assert set(_clients(template)) == {"McpClient", "DashboardClient"}

    linked = synth(alexa_vendor_id="M3IHEJ1OZMSGZ3")
    clients = linked.find_resources("AWS::Cognito::UserPoolClient")
    alexa = next(c["Properties"] for c in clients.values() if c["Properties"].get("GenerateSecret"))
    callbacks = alexa["CallbackURLs"]
    assert "https://pitangui.amazon.com/api/skill/link/M3IHEJ1OZMSGZ3" in callbacks
    assert len(callbacks) == 4  # every regional Alexa redirect URL
    assert alexa["AllowedOAuthFlows"] == ["code"]
    # The app accepts tokens from both clients, and not the dashboard's
    audience = str(_env(linked)["OAUTH_AUDIENCE"])
    named = _clients(linked)
    assert all(f"'Ref': '{named[n][0]}'" in audience for n in ("McpClient", "AlexaClient"))
    assert named["DashboardClient"][0] not in audience
    assert "email" in alexa["AllowedOAuthScopes"]  # a Google login's email is checked before it may link


def test_dashboard_sign_in_client(template):
    logical_id, client = _clients(template)["DashboardClient"]
    assert client["GenerateSecret"] is False and client["AllowedOAuthFlows"] == ["code"]
    assert "https://abc.us-east-1.awsapprunner.com/auth/callback" in client["CallbackURLs"]
    assert "https://abc.us-east-1.awsapprunner.com/auth/signed-out" in client["LogoutURLs"]
    assert set(client["AllowedOAuthScopes"]) == {"openid", "email", "profile"}
    assert client["SupportedIdentityProviders"] == ["COGNITO"]
    assert logical_id in str(_env(template)["LOGIN_CLIENT_ID"])
    assert not template.find_resources("AWS::Cognito::UserPoolIdentityProvider")
    assert "ALEXA_ACCOUNT_LINKING" not in _env(template) and "CAREGIVER_LOGINS" not in _env(template)


def test_google_login_reads_its_secret_at_deploy():
    t = synth(public_url="https://abc.us-east-1.awsapprunner.com", alexa_vendor_id="M3IHEJ1OZMSGZ3",
              google_client_id="123-abc.apps.googleusercontent.com", caregiver_logins="priya@example.com",
              alexa_account_linking=True)
    google = next(iter(t.find_resources("AWS::Cognito::UserPoolIdentityProvider").values()))["Properties"]
    assert google["ProviderName"] == "Google" and google["ProviderDetails"]["client_id"] == "123-abc.apps.googleusercontent.com"
    assert "resolve:secretsmanager:kinvesper/google-client-secret" in str(google["ProviderDetails"]["client_secret"])
    assert google["AttributeMapping"]["email_verified"] == "email_verified"
    named = _clients(t)
    assert "Google" in str(named["DashboardClient"][1]["SupportedIdentityProviders"])
    assert "Google" in str(named["AlexaClient"][1]["SupportedIdentityProviders"])
    assert named["McpClient"][1]["SupportedIdentityProviders"] == ["COGNITO"]
    env = _env(t)
    assert env["CAREGIVER_LOGINS"] == "priya@example.com" and env["ALEXA_ACCOUNT_LINKING"] == "required"


def test_skill_bridge_only_with_skill_id(template):
    assert "ALEXA_SKILL_ID" not in _env(template)
    assert _env(synth(alexa_skill_id="amzn1.ask.skill.x"))["ALEXA_SKILL_ID"] == "amzn1.ask.skill.x"


def test_phone_calls_only_with_a_calling_number(template):
    assert "VOICE_CALL_FROM" not in _env(template)
    assert _env(synth(voice_call_from="+18005550100"))["VOICE_CALL_FROM"] == "+18005550100"


def test_twilio_credentials_come_from_secrets_manager(template):
    secrets = _env(template, "RuntimeEnvironmentSecrets")
    assert "TWILIO_CREDENTIALS" in secrets and "TWILIO_CREDENTIALS" not in _env(template)


def test_login_with_amazon_reads_its_secret_at_deploy():
    t = synth(public_url="https://abc.us-east-1.awsapprunner.com", amazon_client_id="amzn1.application-oa2-client.abc")
    idp = next(iter(t.find_resources("AWS::Cognito::UserPoolIdentityProvider").values()))["Properties"]
    assert idp["ProviderName"] == "LoginWithAmazon" and idp["ProviderDetails"]["client_id"] == "amzn1.application-oa2-client.abc"
    assert "resolve:secretsmanager:kinvesper/amazon-client-secret" in str(idp["ProviderDetails"]["client_secret"])
    assert idp["AttributeMapping"]["email"] == "email"
    assert "LoginWithAmazon" in str(_clients(t)["DashboardClient"][1]["SupportedIdentityProviders"])
