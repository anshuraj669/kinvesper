"""Kinvesper MCP server (protocol 2025-11-25) over Streamable HTTP."""

from __future__ import annotations

import json
import warnings
from contextvars import ContextVar
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from mcp.server.experimental.task_context import ServerTaskContext
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import McpError
from mcp.types import (
    METHOD_NOT_FOUND,
    CallToolResult,
    ClientCapabilities,
    ElicitationCapability,
    ErrorData,
    Tool,
    ToolAnnotations,
    ToolExecution,
)
from pydantic import BaseModel, Field

from .auth import JwtTokenVerifier, auth_settings
from .config import settings
from .icons import AMBER, BLUE, GREEN, PURPLE, RED, SLATE, TEAL, icon
from .models import (
    CareSummary,
    CheckInResult,
    LogDoseResult,
    Medication,
    MedicationResult,
    NotifyResult,
    RefillResult,
    ScamAssessment,
    ScheduleResult,
)
from .service import CareService, clean_message, spoken_time

INSTRUCTIONS = """\
Kinvesper helps an older adult who lives alone manage medications, avoid scams and stay
connected to family. You are talking to the older adult by voice unless told otherwise.

- Speak in short, warm, simple sentences. Each tool returns a `message` or `spoken_summary`
  written to be read aloud; prefer it over reading raw data.
- Before logging a dose, never guess the medication. If unclear, call get_medication_schedule.
- If log_medication_dose returns already_taken, clearly tell them NOT to take another.
- If someone describes a call, text or email asking for money, gift cards, secrecy or personal
  details, call check_for_scam before giving any other advice.
- For a fall, chest pain, trouble breathing or any emergency, call emergency_help immediately.
- When they're running low or ask for more pills, offer request_pharmacy_refill. It runs as a
  task when the client supports tasks: tell them it's underway and share the result when ready.
"""

# Set while a tool runs inside a task, so the tool can report progress on it.
current_task: ContextVar[ServerTaskContext | None] = ContextVar("current_task", default=None)


class SendConfirmation(BaseModel):
    send: bool = Field(description="Send this message now?")


class KinvesperMCP(FastMCP):
    """FastMCP plus task-augmented tool calls (MCP 2025-11-25 Tasks) for the tools in `task_tools`."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.task_tools: dict[str, str] = {}  # tool name -> immediate reply for the model
        with warnings.catch_warnings():  # the SDK marks its experimental Tasks API deprecated in favor of 2.x
            warnings.simplefilter("ignore", DeprecationWarning)
            self._mcp_server.experimental.enable_tasks()

    async def list_tools(self) -> list[Tool]:
        tools = await super().list_tools()
        for t in tools:
            t.execution = ToolExecution(taskSupport="optional" if t.name in self.task_tools else "forbidden")
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        context = self.get_context()
        experimental = context.request_context.experimental if context.request_context else None
        if experimental is None or not experimental.is_task:
            return await super().call_tool(name, arguments)
        if name not in self.task_tools:
            raise McpError(ErrorData(code=METHOD_NOT_FOUND, message=f"Tool {name} does not support task-augmented invocation"))

        async def work(task: ServerTaskContext) -> CallToolResult:
            token = current_task.set(task)
            try:
                result = await self._tool_manager.call_tool(name, arguments, context=context, convert_result=True)
            finally:
                current_task.reset(token)
            if isinstance(result, CallToolResult):
                return result
            content, structured = result if isinstance(result, tuple) else (result, None)
            return CallToolResult(content=list(content), structuredContent=structured)

        return await experimental.run_task(work, model_immediate_response=self.task_tools[name])


def transport_security() -> TransportSecuritySettings | None:
    """DNS-rebinding protection for a localhost bind, also trusting PUBLIC_URL (e.g. an ngrok tunnel)."""
    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        return None  # bound publicly (behind a load balancer): FastMCP's default, no Host check
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if settings.public_url:
        public = urlparse(settings.public_url)
        hosts.append(public.netloc)
        origins.append(f"{public.scheme}://{public.netloc}")
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


def _can_elicit(ctx: Context) -> bool:
    return ctx.session.check_client_capability(ClientCapabilities(elicitation=ElicitationCapability()))


def build_server(service: CareService) -> KinvesperMCP:
    auth = auth_settings(settings)
    mcp = KinvesperMCP(
        name="Kinvesper",
        instructions=INSTRUCTIONS,
        website_url=settings.base_url,
        icons=icon("logo", TEAL),
        token_verifier=JwtTokenVerifier(settings) if auth else None,
        auth=auth,
        transport_security=transport_security(),
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
    )

    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    # ---------------- tools ----------------

    @mcp.tool(
        title="Medication schedule",
        icons=icon("clock", BLUE),
        description=(
            "Today's medication schedule with the status of every dose (taken, due, missed, upcoming). "
            "Use for 'what pills do I take', 'did I take my blood pressure pill?', 'what's next?'. "
            "Pass `medication` (name or purpose, e.g. 'blood pressure') to ask about one medicine."
        ),
        annotations=read_only,
    )
    def get_medication_schedule(
        medication: Annotated[str | None, Field(description="Optional medication name or purpose")] = None,
    ) -> ScheduleResult:
        service.touch_activity()  # any interaction counts as activity
        return service.schedule(medication)

    @mcp.tool(
        title="Log a dose",
        icons=icon("check", GREEN),
        description=(
            "Record that the person just took a medication. Guards against double-dosing: returns "
            "status 'already_taken' if that dose was already logged."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
    )
    def log_medication_dose(
        medication: Annotated[str, Field(description="Medication name or purpose, e.g. 'Metformin' or 'sugar pill'")],
    ) -> LogDoseResult:
        service.touch_activity()  # any interaction counts as activity
        return service.log_dose(medication)

    @mcp.tool(
        title="Add a medication",
        icons=icon("plus", BLUE),
        description="Add a new prescription to the schedule (usually done by the caregiver).",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    )
    def add_medication(
        name: str,
        dose: Annotated[str, Field(description="e.g. '10 mg'")],
        times: Annotated[list[str], Field(description="Daily times in 24h HH:MM, e.g. ['08:00', '20:00']")],
        purpose: str = "",
        instructions: str = "",
        pills_remaining: int = 30,
    ) -> MedicationResult:
        med = service.add_medication(Medication(
            name=name, dose=dose, times=times, purpose=purpose, instructions=instructions, pills_remaining=pills_remaining,
        ))
        when = " and ".join(spoken_time(t) for t in med.times)
        return MedicationResult(medication=med, message=f"Added {med.name} {med.dose}, taken at {when}.")

    @mcp.tool(
        title="Scam check",
        icons=icon("shield", AMBER),
        description=(
            "Assess a suspicious phone call, text, email or visitor for elder-fraud red flags "
            "(gift cards, wire transfers, 'grandchild in jail', IRS/Medicare threats, secrecy, remote access). "
            "Automatically alerts the family when risk is high."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True),
    )
    async def check_for_scam(
        description: Annotated[str, Field(description="What the caller or message said, in the person's words")],
    ) -> ScamAssessment:
        service.touch_activity()  # any interaction counts as activity
        return await service.check_scam(description)

    @mcp.tool(
        title="Daily check-in",
        icons=icon("heart", PURPLE),
        description="Record how the person is feeling today. Alerts family if they're unwell or report a fall, pain, dizziness.",
        annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True),
    )
    async def daily_check_in(
        mood: Annotated[Literal["great", "good", "okay", "low", "unwell"], Field(description="Overall mood")],
        notes: Annotated[str, Field(description="Anything else they said about how they feel")] = "",
    ) -> CheckInResult:
        service.touch_activity()  # any interaction counts as activity
        return await service.check_in(mood, notes)

    @mcp.tool(
        title="Message family",
        icons=icon("message", TEAL),
        description=(
            "Send a message from the person to their caregiver (e.g. 'tell Priya I'm going to the doctor'). "
            "Asks the person to confirm first when the client supports it."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True),
    )
    async def notify_caregiver(
        message: Annotated[str, Field(description=(
            "The message itself, as the caregiver should read it (e.g. \"I'll call you tonight\"), "
            "without \"Tell <name>\" in front"))],
        ctx: Context,
        urgent: bool = False,
    ) -> NotifyResult:
        service.touch_activity()  # any interaction counts as activity
        caregiver = service.profile().caregiver_name
        message = clean_message(message, caregiver)
        if not _can_elicit(ctx):
            return await service.message_caregiver(message, urgent)
        answer = await ctx.elicit(f'Send this to {caregiver}? "{message}"', SendConfirmation)
        if answer.action == "accept" and answer.data.send:
            return await service.message_caregiver(message, urgent, confirmed_by_user=True)
        return NotifyResult(sent=False, confirmed_by_user=False, message=f"Okay, I won't send it to {caregiver}.")

    @mcp.tool(
        title="Emergency help",
        icons=icon("warning", RED),
        description="Immediately alert family for a fall, injury, chest pain, trouble breathing or any emergency. No confirmation needed.",
        annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True),
    )
    async def emergency_help(
        description: Annotated[str, Field(description="What happened")],
    ) -> NotifyResult:
        service.touch_activity()  # any interaction counts as activity
        return await service.emergency(description)

    @mcp.tool(
        title="Care summary",
        icons=icon("chart", SLATE),
        description="Adherence statistics, missed doses, open alerts and a written summary for the caregiver.",
        annotations=read_only,
    )
    async def get_care_summary(
        days: Annotated[int, Field(ge=1, le=30, description="How many days to cover")] = 7,
    ) -> CareSummary:
        return await service.summary(days)

    mcp.task_tools["request_pharmacy_refill"] = (
        "I've sent the refill request to the pharmacy. I'll let you know as soon as it's ready."
    )

    @mcp.tool(
        title="Pharmacy refill",
        icons=icon("bag", GREEN),
        description=(
            "Order a refill of a medication from the person's pharmacy, for pickup or delivery. "
            "Long-running: supports task-augmented calls (poll tasks/get for progress). "
            "Returns the existing request if a refill for that medication is already underway."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def request_pharmacy_refill(
        medication: Annotated[str, Field(description="Medication name or purpose")],
        fulfillment: Annotated[Literal["pickup", "delivery"], Field(description="Pick up at the pharmacy or deliver")] = "pickup",
    ) -> RefillResult:
        service.touch_activity()  # any interaction counts as activity
        task = current_task.get()
        if task is None:  # plain call: the simulated pharmacy answers straight away
            return await service.request_refill(medication, fulfillment)

        async def report(refill) -> None:
            await task.update_status(refill.status_message)

        return await service.request_refill(medication, fulfillment, on_status=report, step_seconds=settings.refill_step_seconds)

    # ---------------- resources ----------------

    @mcp.resource("care://profile", icons=icon("user", TEAL), name="profile", title="Care profile", mime_type="application/json",
                  description="The person, their caregiver and pharmacy")
    def profile_resource() -> str:
        return service.profile().model_dump_json(indent=1)

    @mcp.resource("care://medications", icons=icon("list", BLUE), name="medications", title="Medication list", mime_type="application/json",
                  description="All active prescriptions with times and pills remaining")
    def medications_resource() -> str:
        return json.dumps([m.model_dump() for m in service.medications()], indent=1)

    @mcp.resource("care://today", icons=icon("clock", BLUE), name="today", title="Today's doses", mime_type="application/json",
                  description="Live status of today's doses")
    def today_resource() -> str:
        return service.schedule().model_dump_json(indent=1)

    @mcp.resource("care://alerts", icons=icon("bell", AMBER), name="alerts", title="Open alerts", mime_type="application/json",
                  description="Unacknowledged alerts for the caregiver")
    def alerts_resource() -> str:
        return json.dumps([a.model_dump() for a in service.alerts(include_acknowledged=False)], indent=1)

    @mcp.resource("care://refills", icons=icon("bag", GREEN), name="refills", title="Pharmacy refills",
                  mime_type="application/json", description="Refill requests and their status, newest first")
    def refills_resource() -> str:
        return json.dumps([r.model_dump() for r in service.refills()], indent=1)

    # ---------------- prompts ----------------

    @mcp.prompt(title="Morning check-in", icons=icon("sun", AMBER), description="A gentle morning routine: greeting, mood, and today's medications")
    def morning_check_in() -> str:
        p = service.profile()
        return (
            f"Greet {p.preferred_name} warmly for the morning. Ask how they slept and how they feel, then record it with "
            "daily_check_in. Then use get_medication_schedule and walk them through what to take now, one medicine at a time."
        )

    @mcp.prompt(title="Weekly caregiver report", icons=icon("report", SLATE), description="Summarize the past week for the family caregiver")
    def weekly_caregiver_report(days: str = "7") -> str:
        p = service.profile()
        return (
            f"You are writing to {p.caregiver_name}, {p.patient_name}'s {p.caregiver_relation}. Call get_care_summary "
            f"with days={days} and read care://alerts, then write a short, warm update with one concrete suggestion."
        )

    return mcp
