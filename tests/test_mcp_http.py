"""End-to-end: a real MCP client talking to the server over Streamable HTTP."""

import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

EXPECTED_TOOLS = {
    "get_medication_schedule", "log_medication_dose", "add_medication", "check_for_scam",
    "daily_check_in", "notify_caregiver", "emergency_help", "get_care_summary",
}

async def test_protocol_version_and_tools(server_url):
    async with streamable_http_client(server_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.protocolVersion == "2025-11-25"
            assert init.serverInfo.name == "Kinvesper"

            tools = {t.name: t for t in (await session.list_tools()).tools}
            assert EXPECTED_TOOLS <= set(tools)
            assert tools["get_medication_schedule"].annotations.readOnlyHint is True
            assert tools["log_medication_dose"].outputSchema is not None

async def test_structured_tool_call(server_url):
    async with streamable_http_client(server_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("check_for_scam", {"description": "The IRS says I owe taxes and must pay in gift cards today"})
            assert not result.isError
            assert result.structuredContent["risk"] == "high"

async def test_resources_and_prompts(server_url):
    async with streamable_http_client(server_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            uris = {str(r.uri) for r in (await session.list_resources()).resources}
            assert {"care://profile", "care://medications", "care://today", "care://alerts"} <= uris

            meds = await session.read_resource("care://medications")
            assert any(m["name"] == "Lisinopril" for m in json.loads(meds.contents[0].text))

            prompts = {p.name for p in (await session.list_prompts()).prompts}
            assert {"morning_check_in", "weekly_caregiver_report"} <= prompts
