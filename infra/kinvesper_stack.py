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
# Alexa's account-linking redirect hosts (North America, Europe, Far East, and alexa.amazon.com)
ALEXA_LINK_HOSTS = ("pitangui.amazon.com", "layla.amazon.com", "alexa.amazon.co.jp", "alexa.amazon.com")


class KinvesperStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        public_url: str = "",
        care_tz: str = "UTC",
        caregiver_phone: str = "",
        caregiver_email: str = "",
        ops_email: str = "",
        llm_provider: str = "bedrock",
        llm_model: str = "anthropic.claude-opus-5-5",
        ollama_url: str = "",
        callback_urls: list[str] | None = None,
        alexa_vendor_id: str = "",
        alexa_skill_id: str = "",
        voice_call_from: str = "",
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

        # ---- caregiver alerts by SMS and/or email (email needs no SMS sandbox or spend limit)
        alerts = sns.Topic(self, "CaregiverAlerts", display_name="Kinvesper")
        if caregiver_phone:
            alerts.add_subscription(subs.SmsSubscription(caregiver_phone))
        if caregiver_email:
            alerts.add_subscription(subs.EmailSubscription(caregiver_email))

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
        clients = [client]
        alexa_client = None
        if alexa_vendor_id:
            # Alexa+ account linking: a confidential client (Alexa sends the secret to the token
            # endpoint) with every regional Alexa redirect URL, or linking fails outside one region.
            alexa_client = users.add_client(
                "AlexaClient",
                generate_secret=True,
                o_auth=cognito.OAuthSettings(
                    flows=cognito.OAuthFlows(authorization_code_grant=True),
                    scopes=[cognito.OAuthScope.resource_server(resource_server, mcp_scope), cognito.OAuthScope.OPENID],
                    callback_urls=[f"https://{host}/api/skill/link/{alexa_vendor_id}" for host in ALEXA_LINK_HOSTS],
                ),
                supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
                prevent_user_existence_errors=True,
                refresh_token_validity=cdk.Duration.days(365),  # don't make an older person relink every month
            )
            clients.append(alexa_client)
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
        claude_key = None
        if llm_provider == "anthropic":
            # Starts as a placeholder (the voice agent falls back to its keyword router until the key
            # works); scripts/set_claude_key.sh stores the real key and restarts the service.
            claude_key = secret("ClaudeApiKey", "Claude API key (set it with scripts/set_claude_key.sh)")
            secrets["ANTHROPIC_API_KEY"] = claude_key

        # ---- container image, built from the repo's Dockerfile and pushed to ECR by cdk deploy
        image = ecr_assets.DockerImageAsset(
            self, "Image", directory=str(REPO_ROOT), platform=ecr_assets.Platform.LINUX_AMD64,
            exclude=[".git", ".venv", ".idea", ".env", "data", "infra", "web", "tests", "alexa", "**/__pycache__", "cdk.out"],
        )

        # ---- IAM: App Runner pulls the image; the app gets only what it uses
        access_role = iam.Role(self, "AccessRole", assumed_by=iam.ServicePrincipal("build.apprunner.amazonaws.com"))
        image.repository.grant_pull(access_role)

        instance_role = iam.Role(self, "InstanceRole", assumed_by=iam.ServicePrincipal("tasks.apprunner.amazonaws.com"))
        table.grant_read_write_data(instance_role)
        alerts.grant_publish(instance_role)
        instance_role.add_to_policy(iam.PolicyStatement(
            sid="CaregiverEmail",  # Settings subscribes the caregiver's email to the alerts topic
            actions=["sns:Subscribe", "sns:Unsubscribe", "sns:ListSubscriptionsByTopic"],
            resources=[alerts.topic_arn, f"{alerts.topic_arn}:*"],  # the topic and its subscriptions
        ))
        instance_role.add_to_policy(iam.PolicyStatement(
            sid="CaregiverSms",  # texts to the number in Settings: SMS to a phone number has no resource ARN
            actions=["sns:Publish"],
            resources=["*"],
        ))
        for s in secrets.values():
            s.grant_read(instance_role)
        instance_role.add_to_policy(iam.PolicyStatement(
            sid="UrgentAlertCalls",  # spoken phone calls to the caregiver (AWS End User Messaging SMS and Voice)
            actions=["sms-voice:SendVoiceMessage"],
            resources=["*"],
        ))
        instance_role.add_to_policy(iam.PolicyStatement(
            sid="VoiceSimulatorSpeech",  # Amazon Transcribe hears the voice simulator, Amazon Polly speaks its replies
            actions=["transcribe:StartStreamTranscription", "polly:SynthesizeSpeech"],
            resources=["*"],
        ))
        if llm_provider == "bedrock":
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
            "LLM_PROVIDER": llm_provider,  # "none" runs offline (rule-based) until Bedrock access is granted
            "LLM_MODEL": llm_model,
            "SNS_TOPIC_ARN": alerts.topic_arn,
            "OAUTH_ISSUER": issuer,
            "OAUTH_AUDIENCE": ",".join(c.user_pool_client_id for c in clients),
            "OAUTH_SCOPES": SCOPE,
            "SCHEDULER_INTERVAL_S": "0",  # App Runner idles the CPU between requests: EventBridge drives the checks
            "STT_PROVIDER": "transcribe",  # Whisper takes ~8 s per request on App Runner's CPU; Transcribe ~1 s
        }
        if public_url:
            env["PUBLIC_URL"] = public_url.rstrip("/")
        if alexa_skill_id:
            env["ALEXA_SKILL_ID"] = alexa_skill_id  # turns on the classic skill bridge at /alexa
        if voice_call_from:
            env["VOICE_CALL_FROM"] = voice_call_from  # a voice-capable number: urgent alerts ring the caregiver
        if llm_provider == "bedrock" or (llm_provider == "anthropic" and not llm_model.startswith("anthropic.")):
            env["LLM_MODEL"] = llm_model
        else:
            del env["LLM_MODEL"]  # each provider has its own default model
        if llm_provider == "ollama":
            env["OLLAMA_URL"] = ollama_url  # e.g. a tunnel to the machine running Ollama

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
        out("ServiceArn", service.attr_service_arn, "App Runner service")
        out("ServiceUrl", url, "Pass as -c publicUrl=... on the next deploy")
        out("McpUrl", f"{url}/mcp", "MCP endpoint (Streamable HTTP, OAuth 2.1)")
        out("VoiceSimulator", f"{url}/voice?key=<SimulatorKey>", "Voice simulator")
        out("Dashboard", f"{url}/dashboard?key=<DashboardKey>", "Caregiver dashboard")
        out("SimulatorKeySecret", simulator_key.secret_arn, "aws secretsmanager get-secret-value --secret-id <arn>")
        out("DashboardKeySecret", dashboard_key.secret_arn, "aws secretsmanager get-secret-value --secret-id <arn>")
        out("ServiceTokenSecret", service_token.secret_arn, "Bearer token that skips OAuth (simulators)")
        if claude_key:
            out("ClaudeApiKeySecret", claude_key.secret_arn, "Set with scripts/set_claude_key.sh")
        out("OAuthIssuer", issuer, "Cognito issuer")
        out("OAuthClientId", client.user_pool_client_id, "Public OAuth client (PKCE)")
        if alexa_client:
            out("AlexaClientId", alexa_client.user_pool_client_id,
                "Alexa+ account linking client; its secret is in the Cognito console (or describe-user-pool-client)")
        out("OAuthLogin", domain.base_url(), "Cognito hosted login")
        out("UserPoolId", users.user_pool_id, "Create caregiver logins here")
        out("TableName", table.table_name, "DynamoDB table")
        out("AlertsTopic", alerts.topic_arn, "Caregiver alerts topic (SMS/email)")
