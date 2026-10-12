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
    assert set(secrets) == {"SERVICE_TOKEN", "SIMULATOR_KEY", "DASHBOARD_KEY"}
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
    assert not any(a.endswith(":*") or a == "*" for a in actions)


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


def test_alexa_client_only_with_vendor_id(template):
    assert len(template.find_resources("AWS::Cognito::UserPoolClient")) == 1

    linked = synth(alexa_vendor_id="M3IHEJ1OZMSGZ3")
    clients = linked.find_resources("AWS::Cognito::UserPoolClient")
    alexa = next(c["Properties"] for c in clients.values() if c["Properties"].get("GenerateSecret"))
    callbacks = alexa["CallbackURLs"]
    assert "https://pitangui.amazon.com/api/skill/link/M3IHEJ1OZMSGZ3" in callbacks
    assert len(callbacks) == 4  # every regional Alexa redirect URL
    assert alexa["AllowedOAuthFlows"] == ["code"]
    # The app accepts tokens from both clients
    audience = str(_env(linked)["OAUTH_AUDIENCE"])
    assert all(f"'Ref': '{logical_id}'" in audience for logical_id in clients)


def test_skill_bridge_only_with_skill_id(template):
    assert "ALEXA_SKILL_ID" not in _env(template)
    assert _env(synth(alexa_skill_id="amzn1.ask.skill.x"))["ALEXA_SKILL_ID"] == "amzn1.ask.skill.x"
