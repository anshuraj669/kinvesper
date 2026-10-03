"""Walk through the demo scenarios against a running server.

    uv run kinvesper            # terminal 1
    uv run python scripts/demo_client.py [http://127.0.0.1:8000/mcp]
"""

import asyncio
import sys
import warnings

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, ElicitResult

warnings.filterwarnings("ignore", "The experimental tasks API is deprecated", DeprecationWarning)

SCENARIOS = [
    ("Margaret: 'What pills do I need today?'", "get_medication_schedule", {}),
    ("Margaret: 'Did I take my blood pressure pill?'", "get_medication_schedule", {"medication": "blood pressure"}),
    ("Margaret: 'I just took my blood pressure pill.'", "log_medication_dose", {"medication": "blood pressure pill"}),
    ("Margaret: 'I took my blood pressure pill.' (again)", "log_medication_dose", {"medication": "blood pressure pill"}),
    ("Margaret: 'A man says my grandson is in jail and needs gift cards for bail. He said don't tell anyone.'",
     "check_for_scam", {"description": "A man says my grandson is in jail and needs gift cards for bail. He said don't tell anyone."}),
    ("Margaret: 'I'm feeling a bit lonely today.'", "daily_check_in", {"mood": "low", "notes": "feeling a bit lonely"}),
    ("Margaret: 'Tell Priya I'm going to the doctor at 3.'", "notify_caregiver", {"message": "I'm going to the doctor at 3"}),
    ("Priya (caregiver): 'How was Mom's week?'", "get_care_summary", {"days": 7}),
]


async def confirm(_ctx, params):
    """Elicitation: Margaret says yes when asked to confirm."""
    print(f"❓ {params.message}\n   (Margaret: 'Yes.')")
    return ElicitResult(action="accept", content={"send": True})


async def refill_task(session: ClientSession) -> None:
    print("🗣  Margaret: 'Can you order more of my cholesterol pills?'\n   → request_pharmacy_refill (as a task)")
    created = await session.experimental.call_tool_as_task("request_pharmacy_refill", {"medication": "cholesterol"})
    last = None
    async for status in session.experimental.poll_task(created.task.taskId):
        if status.statusMessage != last:
            print(f"   ⏳ {status.status}: {status.statusMessage}")
            last = status.statusMessage
    result = await session.experimental.get_task_result(created.task.taskId, CallToolResult)
    print(f"🔊 {result.structuredContent['message']}\n")


async def main(url: str) -> None:
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write, elicitation_callback=confirm) as session:
            init = await session.initialize()
            print(f"Connected to {init.serverInfo.name} (MCP {init.protocolVersion})\n")
            for said, tool, args in SCENARIOS:
                result = await session.call_tool(tool, args)
                data = result.structuredContent or {}
                spoken = data.get("message") or data.get("spoken_summary") or data.get("advice") or data.get("narrative")
                print(f"🗣  {said}\n   → {tool}({args})\n🔊 {spoken}\n")
            await refill_task(session)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/mcp"))
