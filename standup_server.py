import json
import re
from datetime import date
from fastmcp import FastMCP

mcp = FastMCP("StandupServer")


def _safe_json(s, fallback):
    """Parse JSON string robustly — handles cases where Groq passes slightly different formats"""
    if isinstance(s, (list, dict)):
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        # try to extract JSON array or object from within the string
        for pattern in (r'\[[\s\S]*\]', r'\{[\s\S]*\}'):
            m = re.search(pattern, s)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    continue
        return fallback


@mcp.tool()
def parse_standup(raw_text: str) -> str:
    """Parse raw standup text into structured JSON with name, done, today, blockers fields"""
    entries = []
    current = {}

    for line in raw_text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            if current and "name" in current:
                entries.append(current)
                current = {}
            continue

        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip().lower()
            val = val.strip()

            if key == "dev":
                if current and "name" in current:
                    entries.append(current)
                current = {"name": val, "done": "", "today": "", "blockers": "None"}
            elif key == "done":
                current["done"] = val
            elif key == "today":
                current["today"] = val
            elif key == "blockers":
                current["blockers"] = val

    if current and "name" in current:
        entries.append(current)

    return json.dumps(entries, indent=2)


@mcp.tool()
def detect_blockers(entries_json: str) -> str:
    """Detect which team members are blocked from parsed standup JSON"""
    entries = _safe_json(entries_json, fallback=[])
    blocked = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        b = entry.get("blockers", "None").strip()
        if b.lower() not in ["none", "no blockers", "nil", "-", "", "no"]:
            blocked.append({"name": entry["name"], "blocker": b})

    return json.dumps(blocked, indent=2)


@mcp.tool()
def generate_summary(entries_json: str, blockers_json: str, prs_json: str, issues_json: str = "[]") -> str:
    """Generate formatted markdown standup summary content — file is saved by the Filesystem MCP server"""
    print(f"[DEBUG] entries_json type={type(entries_json).__name__} value={str(entries_json)[:120]}")
    entries  = _safe_json(entries_json,  fallback=[])
    blockers = _safe_json(blockers_json, fallback=[])
    prs      = _safe_json(prs_json,      fallback=[])
    issues   = _safe_json(issues_json,   fallback=[])
    today    = str(date.today())

    entries  = [e for e in entries  if isinstance(e, dict)]
    blockers = [b for b in blockers if isinstance(b, dict)]
    prs      = [p for p in prs      if isinstance(p, dict)]
    issues   = [i for i in issues   if isinstance(i, dict)]

    blocked_names = {b["name"] for b in blockers}
    lines = [f"# Daily Standup Summary — {today}\n"]

    lines.append("## Team Progress")
    for e in entries:
        icon = "⚠️ " if e["name"] in blocked_names else "✅"
        lines.append(f"- **{e['name']}** {icon} | Done: {e['done']} → Today: {e['today']}")

    lines.append("\n## Blockers (Needs Attention)")
    if blockers:
        for b in blockers:
            lines.append(f"- **{b['name']}** is BLOCKED — {b['blocker']}")
    else:
        lines.append("- No blockers today 🎉")

    lines.append("\n## Open PRs (Need Review)")
    if prs:
        for pr in prs[:5]:
            number = pr.get("number", "N/A")
            title  = pr.get("title", "Untitled")
            author = pr.get("user", {}).get("login", "unknown")
            draft  = " [DRAFT]" if pr.get("draft") else ""
            lines.append(f"- PR #{number}{draft} — {title} by @{author}")
    else:
        lines.append("- No open PRs")

    lines.append("\n## Open Issues")
    if issues:
        for issue in issues[:5]:
            number = issue.get("number", "N/A")
            title  = issue.get("title", "Untitled")
            author = issue.get("author", "unknown")
            labels = issue.get("labels", [])
            label_str = f" `{'` `'.join(labels)}`" if labels else ""
            lines.append(f"- Issue #{number}{label_str} — {title} by @{author}")
    else:
        lines.append("- No open issues")

    lines.append(f"\n## Stats")
    lines.append(
        f"- **{len(entries)}** members active  |  "
        f"**{len(blockers)}** blocker(s)  |  "
        f"**{len(prs)}** open PR(s)  |  "
        f"**{len(issues)}** open issue(s)"
    )

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
