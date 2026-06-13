import asyncio
import json
import os
import httpx
from datetime import date
from pathlib import Path
from dotenv import load_dotenv
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from openai import OpenAI

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
GITHUB_OWNER  = os.getenv("GITHUB_OWNER", "")
GITHUB_REPO   = os.getenv("GITHUB_REPO", "")
PROJECT_DIR   = Path(__file__).parent.as_posix()
STANDUP_FILE  = (Path(__file__).parent / "standups" / "standup_today.txt").as_posix()
REPORT_PATH   = (Path(__file__).parent / "reports" / f"standup_{date.today()}.md").as_posix()

# ── Import custom MCP servers (in-process) ────────────────────────────────────
from standup_server import mcp as standup_mcp
from github_server  import mcp as github_mcp

# ── Existing Filesystem MCP server (official Anthropic server via npx) ────────
fs_transport = StdioTransport(
    command="npx",
    args=["-y", "@modelcontextprotocol/server-filesystem", PROJECT_DIR]
)


def to_gemini_tools(tools: list) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name":        t.name,
                "description": t.description or "",
                "parameters":  t.inputSchema or {"type": "object", "properties": {}}
            }
        }
        for t in tools
    ]


def extract_text(raw) -> str:
    # FastMCP CallToolResult object (newer versions)
    if hasattr(raw, "data") and raw.data is not None:
        return str(raw.data)
    # fallback: content list of TextContent objects
    if hasattr(raw, "content"):
        raw = raw.content
    if isinstance(raw, list):
        return "\n".join(item.text if hasattr(item, "text") else str(item) for item in raw)
    return str(raw)


async def main():
    gemini_client = OpenAI(
        api_key=os.getenv("GEMINI_API_KEY"),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        http_client=httpx.Client(verify=False)
    )

    # Ensure reports directory exists
    Path("reports").mkdir(exist_ok=True)

    async with Client(standup_mcp) as standup_client, \
               Client(github_mcp)   as github_client, \
               Client(fs_transport) as fs_client:

        # ── Collect tools from each server ────────────────────────────────────
        standup_tools = await standup_client.list_tools()
        github_tools  = await github_client.list_tools()
        fs_tools      = await fs_client.list_tools()

        print(f"📦 StandupServer tools : {[t.name for t in standup_tools]}")
        print(f"📦 GitHubServer tools  : {[t.name for t in github_tools]}")
        print(f"📦 Filesystem tools    : {[t.name for t in fs_tools]}\n")

        # ── Build routing map: tool_name → which client owns it ───────────────
        tool_router = {}
        for t in standup_tools:
            tool_router[t.name] = standup_client   # parse_standup, detect_blockers, generate_summary
        for t in github_tools:
            tool_router[t.name] = github_client    # list_open_prs, get_repo_stats
        for t in fs_tools:
            tool_router[t.name] = fs_client        # read_file, write_file, list_directory, ...

        # give Gemini write_file too — fully autonomous, LLM decides when to save
        EXCLUDE_TOOLS = {"edit_file"}
        all_tools  = standup_tools + github_tools + [t for t in fs_tools if t.name not in EXCLUDE_TOOLS]
        gemini_tools = to_gemini_tools(all_tools)

        # ── System prompt — goal only, agent decides tool order ───────────────
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an autonomous daily standup summarizer agent.\n\n"
                    "You have access to tools for reading files, parsing standups, "
                    "detecting blockers, fetching GitHub data, and generating summaries.\n\n"
                    f"Context:\n"
                    f"- Standup file: {STANDUP_FILE}\n"
                    f"- GitHub repo: {GITHUB_OWNER}/{GITHUB_REPO}\n\n"
                    "Your goal: Read today's standup, understand what each team member did, "
                    "identify blockers, fetch open PRs and issues from GitHub, generate "
                    "a complete markdown summary, then save it using write_file.\n\n"
                    "Important: list_open_prs and list_open_issues are independent of each other "
                    "— always call them together in the same response to save API calls.\n\n"
                    f"Save the report to: {REPORT_PATH}"
                )
            },
            {
                "role": "user",
                "content": "Generate today's standup summary report."
            }
        ]

        print("🤖 Standup Summarizer Agent starting...\n")

        # ── Agentic loop ──────────────────────────────────────────────────────
        import time
        while True:
            for attempt in range(5):
                try:
                    response = gemini_client.chat.completions.create(
                        model="gemini-2.5-flash",
                        messages=messages,
                        tools=gemini_tools,
                        tool_choice="auto"
                    )
                    break
                except Exception as e:
                    err = str(e)
                    if "429" in err or "RESOURCE_EXHAUSTED" in err:
                        wait = 20 + attempt * 5
                        print(f"  ⏳ Rate limit hit — waiting {wait}s before retry (attempt {attempt+1}/5)...")
                        time.sleep(wait)
                    elif attempt == 4:
                        raise
                    else:
                        print(f"  ⚠️  API error (attempt {attempt+1}), retrying in 2s...")
                        time.sleep(2)

            msg = response.choices[0].message

            # ── Debug: show what Gemini decided ───────────────────────────────
            if msg.tool_calls:
                print(f"\n🧠 Gemini decided to call {len(msg.tool_calls)} tool(s): "
                      f"{[tc.function.name for tc in msg.tool_calls]}")
            else:
                print("\n🧠 Gemini decided: no more tools needed")

            # No more tool calls → Gemini is done
            if not msg.tool_calls:
                print("\n" + "─" * 50)
                print("✅ Agent complete!\n")
                if msg.content:
                    print(msg.content)
                break

            # Append assistant message to history
            messages.append({
                "role":       "assistant",
                "content":    msg.content or "",
                "tool_calls": [
                    {
                        "id":   tc.id,
                        "type": "function",
                        "function": {
                            "name":      tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    }
                    for tc in msg.tool_calls
                ]
            })

            # Execute each tool call — route to correct MCP server
            for tc in msg.tool_calls:
                name   = tc.function.name
                args   = json.loads(tc.function.arguments)
                client = tool_router.get(name)

                server_label = (
                    "StandupServer [custom]"   if client is standup_client else
                    "GitHubServer [custom]"    if client is github_client  else
                    "Filesystem MCP [existing]" if client is fs_client     else
                    "unknown"
                )
                print(f"  🔧 [{name}]  →  {server_label}")

                if not client:
                    result = f"Error: tool '{name}' not found in any server"
                else:
                    try:
                        raw    = await client.call_tool(name, args)
                        result = extract_text(raw)
                    except Exception as e:
                        result = f"Error calling {name}: {e}"

                # print result summary (not full content — can be large)
                preview = result[:80].replace("\n", " ")
                print(f"     ↳ result: {preview}{'...' if len(result) > 80 else ''}")

                if name == "write_file":
                    print("\n" + "─" * 50)
                    print(f"✅ Report saved → {REPORT_PATH}")
                    print("─" * 50)

                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      result
                })


if __name__ == "__main__":
    asyncio.run(main())
