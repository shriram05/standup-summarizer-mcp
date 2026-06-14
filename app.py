import asyncio
import json
import os
import queue
import threading
import time
import httpx
import streamlit as st
from datetime import date
from pathlib import Path
from dotenv import load_dotenv
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from openai import OpenAI

load_dotenv()

GITHUB_OWNER = os.getenv("GITHUB_OWNER", "")
GITHUB_REPO  = os.getenv("GITHUB_REPO", "")
PROJECT_DIR  = Path(__file__).parent.as_posix()
STANDUP_FILE = (Path(__file__).parent / "standups" / "standup_today.txt").as_posix()
REPORT_PATH  = (Path(__file__).parent / "reports" / f"standup_{date.today()}.md").as_posix()


SERVER_COLORS = {
    "StandupServer [custom]":    "🟣",
    "GitHubServer [custom]":     "🟠",
    "Filesystem MCP [existing]": "🟢",
}


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
    if hasattr(raw, "data") and raw.data is not None:
        return str(raw.data)
    if hasattr(raw, "content"):
        raw = raw.content
    if isinstance(raw, list):
        return "\n".join(item.text if hasattr(item, "text") else str(item) for item in raw)
    return str(raw)


async def run_agent_streaming(q: queue.Queue):
    """Run agent and push events into queue as they happen — enables live UI updates."""
    standup_transport = StdioTransport(command="python", args=["standup_server.py"])
    github_transport  = StdioTransport(command="python", args=["github_server.py"])
    fs_transport      = StdioTransport(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", PROJECT_DIR]
    )
    gemini_client = OpenAI(
        api_key=os.getenv("GEMINI_API_KEY"),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        http_client=httpx.Client(verify=False)
    )

    Path("reports").mkdir(exist_ok=True)

    async with Client(standup_transport) as standup_client, \
               Client(github_transport)  as github_client, \
               Client(fs_transport)      as fs_client:

        standup_tools = await standup_client.list_tools()
        github_tools  = await github_client.list_tools()
        fs_tools      = await fs_client.list_tools()

        tool_router = {}
        for t in standup_tools: tool_router[t.name] = standup_client
        for t in github_tools:  tool_router[t.name] = github_client
        for t in fs_tools:      tool_router[t.name] = fs_client

        EXCLUDE_TOOLS = {"edit_file"}
        all_tools    = standup_tools + github_tools + [t for t in fs_tools if t.name not in EXCLUDE_TOOLS]
        gemini_tools = to_gemini_tools(all_tools)

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
            {"role": "user", "content": "Generate today's standup summary report."}
        ]

        llm_call_count = 0

        while True:
            q.put({"type": "thinking", "call_num": llm_call_count + 1})

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
                        q.put({"type": "rate_limit", "msg": f"Rate limit — waiting {wait}s before retry...", "wait": wait})
                        time.sleep(wait)
                    elif attempt == 4:
                        q.put({"type": "error", "msg": str(e)})
                        return
                    else:
                        time.sleep(2)

            llm_call_count += 1
            msg = response.choices[0].message

            if not msg.tool_calls:
                q.put({"type": "done", "llm_calls": llm_call_count, "content": msg.content or ""})
                break

            tool_names = [tc.function.name for tc in msg.tool_calls]
            q.put({"type": "llm_decision", "call_num": llm_call_count, "tools": tool_names})

            messages.append({
                "role":       "assistant",
                "content":    msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ]
            })

            for tc in msg.tool_calls:
                name   = tc.function.name
                args   = json.loads(tc.function.arguments)
                client = tool_router.get(name)

                server_label = (
                    "StandupServer [custom]"    if client is standup_client else
                    "GitHubServer [custom]"     if client is github_client  else
                    "Filesystem MCP [existing]" if client is fs_client      else
                    "unknown"
                )

                q.put({"type": "tool_running", "name": name, "server": server_label})

                try:
                    raw    = await client.call_tool(name, args)
                    result = extract_text(raw)
                except Exception as e:
                    result = f"Error: {e}"

                q.put({"type": "tool_call", "name": name, "server": server_label, "result": result})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

                # exit immediately after write_file — no need for another LLM call
                if name == "write_file" and "Error" not in result:
                    q.put({"type": "done", "llm_calls": llm_call_count, "content": ""})
                    return


def run_agent_thread(q: queue.Queue):
    """Runs the async agent in a dedicated thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_agent_streaming(q))
    except Exception as e:
        q.put({"type": "error", "msg": str(e)})
    finally:
        loop.close()
        q.put({"type": "finished"})


def render_log(log: dict):
    t = log["type"]
    if t == "thinking":
        st.write(f"🧠 Gemini thinking... (LLM call #{log['call_num']})")
    elif t == "llm_decision":
        tools_str = " → ".join(log["tools"])
        st.info(f"🧠 **LLM Call #{log['call_num']}** — Gemini decided: `{tools_str}`")
    elif t == "tool_running":
        color = SERVER_COLORS.get(log["server"], "⚪")
        st.write(f"{color} Running `{log['name']}` on {log['server']}...")
    elif t == "tool_call":
        color = SERVER_COLORS.get(log["server"], "⚪")
        with st.expander(f"{color} `{log['name']}` → {log['server']} ✓"):
            if log["name"] == "write_file":
                st.success("Report saved to disk successfully")
            else:
                preview = log["result"][:600]
                st.code(preview + ("..." if len(log["result"]) > 600 else ""), language="json")
    elif t == "rate_limit":
        st.warning(f"⏳ {log['msg']}")
    elif t == "error":
        st.error(f"❌ Error: {log['msg']}")
    elif t == "done":
        st.success(f"✅ Agent complete — {log['llm_calls']} LLM calls total")


# ── Streamlit UI ──────────────────────────────────────────────────────────────

st.set_page_config(page_title="Standup Summarizer", page_icon="🤖", layout="wide")

st.title("🤖 Daily Standup Summarizer")
st.caption(
    "MCP Architecture: **Filesystem MCP** [existing] + **StandupServer** [custom] + **GitHubServer** [custom] → Gemini 2.5 Flash"
)

st.divider()

left, right = st.columns([1, 2])

with left:
    st.subheader("📋 Today's Standup")
    st.caption(f"📅 {date.today().strftime('%A, %d %B %Y')}")
    standup_path = Path(__file__).parent / "standups" / "standup_today.txt"
    default_content = standup_path.read_text() if standup_path.exists() else ""

    content = st.text_area(
        "Edit standup — saved automatically before the agent runs:",
        value=default_content,
        height=450,
    )

    st.markdown("**MCP Servers connected:**")
    st.markdown("🟢 `@modelcontextprotocol/server-filesystem` — existing")
    st.markdown("🟣 `StandupServer` — custom (parse, detect, summarize)")
    st.markdown("🟠 `GitHubServer` — custom (PRs, issues)")

    run = st.button("🚀 Run Agent", type="primary", use_container_width=True)

with right:
    st.subheader("🔍 Agent Trace")

    if run:
        # save edited standup before running
        standup_path.parent.mkdir(exist_ok=True)
        standup_path.write_text(content)

        q = queue.Queue()
        t = threading.Thread(target=run_agent_thread, args=(q,), daemon=True)
        t.start()

        trace_placeholder = st.empty()
        report_placeholder = st.empty()

        displayed = []

        # poll queue and update UI live while thread is running
        while t.is_alive() or not q.empty():
            try:
                event = q.get(timeout=0.3)
                if event["type"] == "finished":
                    break
                # skip "thinking" and "tool_running" from permanent log — they're interim
                if event["type"] not in ("thinking", "tool_running"):
                    displayed.append(event)

                with trace_placeholder.container():
                    for log in displayed:
                        render_log(log)
                    # show live status for interim events
                    if event["type"] == "thinking":
                        st.write(f"🧠 Gemini thinking... (LLM call #{event['call_num']})")
                    elif event["type"] == "tool_running":
                        color = SERVER_COLORS.get(event["server"], "⚪")
                        st.write(f"{color} Running `{event['name']}`...")
            except queue.Empty:
                pass

        t.join()

        # show final report
        report_file = Path(REPORT_PATH)
        if report_file.exists():
            report_md = report_file.read_text(encoding="utf-8")
            with report_placeholder.container():
                st.divider()
                st.subheader("📄 Generated Report")
                st.markdown(report_md)
                st.download_button(
                    label="⬇️ Download Report",
                    data=report_md,
                    file_name=f"standup_{date.today()}.md",
                    mime="text/markdown"
                )
    else:
        st.info("Click **Run Agent** to start the MCP pipeline.")