"""Kinvesper on AWS: App Runner + DynamoDB + Cognito + SNS + Secrets Manager + EventBridge + Bedrock."""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    aws_apprunner as apprunner,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_ecr_assets as ecr_assets,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_secretsmanager as secretsmanager,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
)
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parent.parent
SCOPE = "kinvesper/mcp"


class KinvesperStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        public_url: str = "",
        care_tz: str = "UTC",
        caregiver_phone: str = "",
        ops_email: str = "",
        llm_model: str = "anthropic.claude-opus-5-5",
        callback_urls: list[str] | None = None,
        retain_data: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        removal = cdk.RemovalPolicy.RETAIN if retain_data else cdk.RemovalPolicy.DESTROY

        # ---- data: one table, pk = patient id, sk = "<kind>#<id>" (see src/kinvesper/store.py)
        table = dynamodb.TableV2(
            self, "Table",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=removal,
        )

        # ---- caregiver alerts by SMS
        alerts = sns.Topic(self, "CaregiverAlerts", display_name="Kinvesper")
        if caregiver_phone:
            alerts.add_subscription(subs.SmsSubscription(caregiver_phone))

        # ---- OAuth 2.1 authorization server for /mcp
        users = cognito.UserPool(
            self, "Users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            password_policy=cognito.PasswordPolicy(min_length=12),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=removal,
        )
        mcp_scope = cognito.ResourceServerScope(scope_name="mcp", scope_description="Use the Kinvesper MCP tools")
        resource_server = users.add_resource_server(
            "Api", identifier="kinvesper", scopes=[mcp_scope], user_pool_resource_server_name="Kinvesper MCP")
        client = users.add_client(
            "McpClient",
            generate_secret=False,  # public client: authorization code + PKCE
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.resource_server(resource_server, mcp_scope), cognito.OAuthScope.OPENID],
                callback_urls=callback_urls or ["http://localhost:8790/callback"],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            prevent_user_existence_errors=True,
        )
        domain = users.add_domain("Login", cognito_domain=cognito.CognitoDomainOptions(
            domain_prefix=f"kinvesper-{cdk.Aws.ACCOUNT_ID}"))
        issuer = f"https://cognito-idp.{self.region}.amazonaws.com/{users.user_pool_id}"

        # ---- shared secrets: generated, never in the template or the image
        def secret(name: str, purpose: str) -> secretsmanager.Secret:
            return secretsmanager.Secret(
                self, name, description=f"Kinvesper {purpose}",
                generate_secret_string=secretsmanager.SecretStringGenerator(exclude_punctuation=True, password_length=40),
            )

        service_token = secret("ServiceToken", "bearer token for the built-in voice simulator")
        simulator_key = secret("SimulatorKey", "voice simulator access key (/voice?key=...)")
        dashboard_key = secret("DashboardKey", "caregiver dashboard access key (/dashboard?key=...)")
        secrets = {"SERVICE_TOKEN": service_token, "SIMULATOR_KEY": simulator_key, "DASHBOARD_KEY": dashboard_key}

        # ---- container image, built from the repo's Dockerfile and pushed to ECR by cdk deploy
        image = ecr_assets.DockerImageAsset(
            self, "Image", directory=str(REPO_ROOT), platform=ecr_assets.Platform.LINUX_AMD64,
            exclude=[".git", ".venv", ".idea", ".env", "data", "infra", "web", "tests", "**/__pycache__", "cdk.out"],
        )

        # ---- IAM: App Runner pulls the image; the app gets only what it uses
        access_role = iam.Role(self, "AccessRole", assumed_by=iam.ServicePrincipal("build.apprunner.amazonaws.com"))
        image.repository.grant_pull(access_role)

        instance_role = iam.Role(self, "InstanceRole", assumed_by=iam.ServicePrincipal("tasks.apprunner.amazonaws.com"))
        table.grant_read_write_data(instance_role)
        alerts.grant_publish(instance_role)
        for s in secrets.values():
            s.grant_read(instance_role)
        instance_role.add_to_policy(iam.PolicyStatement(
            sid="ClaudeInBedrock",  # Messages API endpoint (AnthropicBedrockMantle)
            actions=["bedrock-mantle:CreateInference"],
            resources=["*"],
        ))

        env = {
            "STORE": "dynamodb",
            "DYNAMODB_TABLE": table.table_name,
            "AWS_REGION": self.region,
            "CARE_TZ": care_tz,
            "LLM_PROVIDER": "bedrock",
            "LLM_MODEL": llm_model,
            "SNS_TOPIC_ARN": alerts.topic_arn,
            "OAUTH_ISSUER": issuer,
            "OAUTH_AUDIENCE": client.user_pool_client_id,
            "OAUTH_SCOPES": SCOPE,
            "SCHEDULER_INTERVAL_S": "0",  # App Runner idles the CPU between requests: EventBridge drives the checks
        }
        if public_url:
            env["PUBLIC_URL"] = public_url.rstrip("/")

        # One instance: MCP Streamable HTTP and voice sessions live in memory.
        scaling = apprunner.CfnAutoScalingConfiguration(
            self, "Scaling", auto_scaling_configuration_name="kinvesper-single",
            min_size=1, max_size=1, max_concurrency=100)

        service = apprunner.CfnService(
            self, "Service",
            service_name="kinvesper",
            source_configuration=apprunner.CfnService.SourceConfigurationProperty(
                authentication_configuration=apprunner.CfnService.AuthenticationConfigurationProperty(
                    access_role_arn=access_role.role_arn),
                auto_deployments_enabled=False,
                image_repository=apprunner.CfnService.ImageRepositoryProperty(
                    image_identifier=image.image_uri,
                    image_repository_type="ECR",
                    image_configuration=apprunner.CfnService.ImageConfigurationProperty(
                        port="8080",
                        runtime_environment_variables=[
                            apprunner.CfnService.KeyValuePairProperty(name=k, value=v) for k, v in env.items()],
                        runtime_environment_secrets=[
                            apprunner.CfnService.KeyValuePairProperty(name=k, value=s.secret_arn) for k, s in secrets.items()],
                    ),
                ),
            ),
            instance_configuration=apprunner.CfnService.InstanceConfigurationProperty(
                cpu="1 vCPU", memory="2 GB", instance_role_arn=instance_role.role_arn),
            health_check_configuration=apprunner.CfnService.HealthCheckConfigurationProperty(
                protocol="HTTP", path="/health", interval=10, timeout=5, healthy_threshold=1, unhealthy_threshold=3),
            auto_scaling_configuration_arn=scaling.attr_auto_scaling_configuration_arn,
        )
        url = f"https://{service.attr_service_url}"

        # ---- proactive checks every 5 minutes: EventBridge calls POST /api/checks with the dashboard key
        connection = events.Connection(
            self, "ChecksConnection",
            authorization=events.Authorization.api_key("X-Dashboard-Key", dashboard_key.secret_value),
        )
        checks = events.ApiDestination(
            self, "ChecksEndpoint", connection=connection, endpoint=f"{url}/api/checks",
            http_method=events.HttpMethod.POST, rate_limit_per_second=1)
        events.Rule(
            self, "ChecksSchedule", schedule=events.Schedule.rate(cdk.Duration.minutes(5)),
            targets=[targets.ApiDestination(checks, retry_attempts=2)])

        # ---- ops: alarm on server errors
        ops = sns.Topic(self, "OpsAlarms", display_name="Kinvesper ops")
        if ops_email:
            ops.add_subscription(subs.EmailSubscription(ops_email))
        errors = cloudwatch.Alarm(
            self, "ServerErrors",
            metric=cloudwatch.Metric(
                namespace="AWS/AppRunner", metric_name="5xxStatusResponses", statistic="Sum",
                period=cdk.Duration.minutes(5),
                dimensions_map={"ServiceName": "kinvesper", "ServiceID": service.attr_service_id}),
            threshold=5, evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="Kinvesper returned 5 or more server errors in 5 minutes",
        )
        errors.add_alarm_action(cw_actions.SnsAction(ops))

        # ---- outputs
        out = lambda name, value, desc: cdk.CfnOutput(self, name, value=value, description=desc)  # noqa: E731
        out("ServiceUrl", url, "Pass as -c publicUrl=... on the next deploy")
        out("McpUrl", f"{url}/mcp", "MCP endpoint (Streamable HTTP, OAuth 2.1)")
        out("VoiceSimulator", f"{url}/voice?key=<SimulatorKey>", "Voice simulator")
        out("Dashboard", f"{url}/dashboard?key=<DashboardKey>", "Caregiver dashboard")
        out("SimulatorKeySecret", simulator_key.secret_arn, "aws secretsmanager get-secret-value --secret-id <arn>")
        out("DashboardKeySecret", dashboard_key.secret_arn, "aws secretsmanager get-secret-value --secret-id <arn>")
        out("ServiceTokenSecret", service_token.secret_arn, "Bearer token that skips OAuth (simulators)")
        out("OAuthIssuer", issuer, "Cognito issuer")
        out("OAuthClientId", client.user_pool_client_id, "Public OAuth client (PKCE)")
        out("OAuthLogin", domain.base_url(), "Cognito hosted login")
        out("UserPoolId", users.user_pool_id, "Create caregiver logins here")
        out("TableName", table.table_name, "DynamoDB table")
        out("AlertsTopic", alerts.topic_arn, "Caregiver SMS topic")
