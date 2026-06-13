# Daily Standup Summarizer — MCP Agent

An autonomous AI agent that reads daily standup text, detects blockers, fetches open GitHub PRs and issues, generates a formatted markdown report, and saves it — all orchestrated using **MCP (Model Context Protocol)** and **Gemini 2.5 Flash**.

## What Makes This an MCP Project

This project connects **1 MCP client to 3 MCP servers simultaneously** — 1 existing official server + 2 custom-built servers:

```
MCP Client (client.py / app.py)
    │
    ├── @modelcontextprotocol/server-filesystem  ← EXISTING official Anthropic MCP server (via npx)
    │   └── read_file, write_file, list_directory, ...
    │
    ├── StandupServer  ← CUSTOM FastMCP server
    │   ├── parse_standup()      — parse raw text into structured JSON
    │   ├── detect_blockers()    — find blocked team members
    │   └── generate_summary()  — build markdown report
    │
    └── GitHubServer  ← CUSTOM FastMCP server (GitHub REST API)
        ├── list_open_prs()
        ├── list_open_issues()
        └── get_repo_stats()
```

**Gemini 2.5 Flash** acts as the autonomous agent — it decides which tools to call and in what order, routing each call to the correct MCP server.

## How It Works

```
User fills standup (via UI or standup_today.txt)
        ↓
Gemini agent autonomously decides tool call order:
  LLM Call 1 → read_text_file         (Filesystem MCP — existing)
  LLM Call 2 → parse_standup          (StandupServer — custom)
  LLM Call 3 → detect_blockers        (StandupServer — custom)
  LLM Call 4 → list_open_prs          (GitHubServer — custom)
  LLM Call 5 → list_open_issues       (GitHubServer — custom)
  LLM Call 6 → generate_summary       (StandupServer — custom)
  LLM Call 7 → write_file             (Filesystem MCP — existing)
        ↓
Markdown report saved to reports/standup_YYYY-MM-DD.md
```

The agent is **fully autonomous** — no hardcoded step order. Gemini reasons about which tool to call next based on each result.

## Sample Output

```markdown
# Daily Standup Summary — 2026-06-13

## Team Progress
- **Shriram** ✅ | Done: RAG pipeline integration → Today: Write unit tests
- **Rahul** ⚠️  | Done: Reviewed 3 PRs → Today: CI pipeline setup
- **Priya** ✅  | Done: Deployed hotfix → Today: Refactor auth flow
- **Ananya** ⚠️ | Done: Designed DB schema → Today: Start API implementation

## Blockers (Needs Attention)
- **Rahul** is BLOCKED — Waiting for staging environment credentials
- **Ananya** is BLOCKED — API specification not finalized by product team

## Open PRs (Need Review)
- PR #1 — Add greeting to sample.txt by @shriram0511

## Open Issues
- Issue #2 — Bug in code by @shriram0511

## Stats
- **4** members active  |  **2** blocker(s)  |  **1** open PR(s)  |  **1** open issue(s)
```

## Tech Stack

| Layer | Technology |
|---|---|
| LLM / Agent | Gemini 2.5 Flash (via OpenAI-compatible endpoint) |
| MCP Framework | FastMCP (custom servers) + `@modelcontextprotocol/server-filesystem` (existing) |
| Frontend | Streamlit |
| GitHub API | httpx (REST) |
| Language | Python 3.12 |

## Project Structure

```
MCP-Project/
├── app.py               # Streamlit web UI — live agent trace + report display
├── client.py            # CLI client — MCP client + Gemini agentic loop
├── standup_server.py    # Custom MCP server — parse, detect, summarize
├── github_server.py     # Custom MCP server — GitHub PRs and issues
├── standups/
│   └── standup_today.txt   # Daily standup input (editable in UI)
├── reports/                # Generated reports — one file per day (auto-created)
├── requirements.txt
└── .env
```

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

> Also requires Node.js (for `npx`) — used to run the official Filesystem MCP server.

### 2. Create `.env`
```env
GEMINI_API_KEY=your_gemini_api_key
GITHUB_TOKEN=your_github_token
GITHUB_OWNER=your_github_username
GITHUB_REPO=your_repo_name
```

**Get Gemini API key:** [Google AI Studio](https://aistudio.google.com) → Get API Key (free)

**Get GitHub token:** GitHub → Settings → Developer Settings → Personal Access Tokens → Classic → Select `repo` scope

### 3. Run — Web UI (recommended)
```bash
streamlit run app.py
```
- Edit today's standup directly in the browser
- Watch the agent trace live as tools execute
- Report renders in the UI with a download button

### 4. Run — CLI
```bash
python client.py
```
Report saved to `reports/standup_YYYY-MM-DD.md`

## Key MCP Concepts Demonstrated

| Concept | Where |
|---|---|
| 1 client → multiple servers | `client.py` connects to 3 servers simultaneously |
| Existing MCP server (npx) | `@modelcontextprotocol/server-filesystem` via `StdioTransport` |
| Custom MCP server | `standup_server.py` and `github_server.py` using FastMCP |
| Tool routing | `tool_router` dict maps each tool name to its owning server |
| Autonomous agent | Gemini decides tool order — no hardcoded steps |
| Agentic loop | while loop — LLM calls tools until task is complete |
