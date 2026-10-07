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


def test_first_deploy_without_public_url():
    assert "PUBLIC_URL" not in _env(synth())


def test_llm_provider_can_be_switched_off():
    assert _env(synth(llm_provider="none"))["LLM_PROVIDER"] == "none"


def test_destroy_mode():
    synth(retain_data=False).has_resource("AWS::DynamoDB::GlobalTable", {"DeletionPolicy": "Delete"})
