import json
import os
import httpx
from fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

mcp = FastMCP("GitHubServer")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28"
}


@mcp.tool()
def list_open_prs(owner: str, repo: str) -> str:
    """List all open pull requests for a GitHub repository"""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls?state=open"
    try:
        response = httpx.get(url, headers=HEADERS, timeout=10, verify=False)
        if response.status_code == 200:
            prs = response.json()
            simplified = [
                {
                    "number":     pr["number"],
                    "title":      pr["title"],
                    "user":       {"login": pr["user"]["login"]},
                    "created_at": pr["created_at"],
                    "draft":      pr.get("draft", False)
                }
                for pr in prs
            ]
            return json.dumps(simplified, indent=2)
        else:
            return json.dumps([])
    except Exception:
        return json.dumps([])


@mcp.tool()
def list_open_issues(owner: str, repo: str) -> str:
    """List open issues for a GitHub repository"""
    url = f"https://api.github.com/repos/{owner}/{repo}/issues?state=open"
    try:
        response = httpx.get(url, headers=HEADERS, timeout=10, verify=False)
        if response.status_code == 200:
            issues = [
                i for i in response.json()
                if "pull_request" not in i   # exclude PRs from issues list
            ]
            simplified = [
                {
                    "number": i["number"],
                    "title":  i["title"],
                    "author": i["user"]["login"],
                    "labels": [l["name"] for l in i.get("labels", [])]
                }
                for i in issues[:10]
            ]
            return json.dumps(simplified, indent=2)
        else:
            return json.dumps([])
    except Exception:
        return json.dumps([])


@mcp.tool()
def get_repo_stats(owner: str, repo: str) -> str:
    """Get basic statistics for a GitHub repository"""
    url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        response = httpx.get(url, headers=HEADERS, timeout=10, verify=False)
        if response.status_code == 200:
            data = response.json()
            return json.dumps({
                "name":        data["name"],
                "description": data.get("description", ""),
                "open_issues": data["open_issues_count"],
                "stars":       data["stargazers_count"],
                "forks":       data["forks_count"],
                "language":    data.get("language", "N/A")
            }, indent=2)
        else:
            return json.dumps({})
    except Exception:
        return json.dumps({})


if __name__ == "__main__":
    mcp.run()
