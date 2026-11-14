"""Loom 仓库的只读 MCP 连接器（第一版）。

让 Claude 能查看 yuchenm1303-png/Loom 的文件、提交、分支、issue 和 PR。
这一版只读，不会修改仓库里的任何东西。
"""

import base64
import os

import httpx
from mcp.server.fastmcp import FastMCP

# 只操作这一个仓库（可以用环境变量覆盖）
OWNER = os.environ.get("GITHUB_OWNER", "yuchenm1303-png")
REPO = os.environ.get("GITHUB_REPO", "Loom")
# 可选：GitHub token。公开仓库不填也能读，只是每小时请求次数更少。
TOKEN = os.environ.get("GITHUB_TOKEN", "")

API = "https://api.github.com"

mcp = FastMCP(
    "loom-github",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
)


def _headers():
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "loom-mcp",
    }
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def _get(path, params=None):
    r = httpx.get(
        f"{API}/repos/{OWNER}/{REPO}{path}",
        headers=_headers(),
        params=params,
        timeout=20,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub 返回 {r.status_code}: {r.text[:300]}")
    return r.json()


@mcp.tool()
def repo_info() -> str:
    """查看仓库基本信息：描述、默认分支、语言、star 数等。"""
    d = _get("")
    return (
        f"{d['full_name']} ({'公开' if not d['private'] else '私有'})\n"
        f"描述: {d.get('description') or '无'}\n"
        f"默认分支: {d['default_branch']}\n"
        f"主要语言: {d.get('language')}\n"
        f"Stars: {d['stargazers_count']}  Forks: {d['forks_count']}\n"
        f"未关闭 issue+PR 数: {d['open_issues_count']}\n"
        f"最近推送: {d['pushed_at']}"
    )


@mcp.tool()
def list_files(path: str = "", ref: str = "") -> str:
    """列出某个目录下的文件和文件夹。path 留空表示仓库根目录；ref 可填分支名或提交号。"""
    params = {"ref": ref} if ref else None
    d = _get(f"/contents/{path.strip('/')}", params)
    if isinstance(d, dict):
        return f"{path} 是一个文件，大小 {d['size']} 字节。请用 read_file 读取。"
    lines = []
    for item in sorted(d, key=lambda x: (x["type"] != "dir", x["name"])):
        mark = "[目录]" if item["type"] == "dir" else f"[文件 {item['size']}B]"
        lines.append(f"{mark} {item['path']}")
    return "\n".join(lines) or "（空目录）"


@mcp.tool()
def read_file(path: str, ref: str = "", max_chars: int = 30000) -> str:
    """读取一个文件的文本内容。ref 可填分支名或提交号；超长内容会被截断。"""
    params = {"ref": ref} if ref else None
    d = _get(f"/contents/{path.strip('/')}", params)
    if isinstance(d, list):
        return f"{path} 是目录，请用 list_files。"
    if d.get("encoding") != "base64" or not d.get("content"):
        return "这个文件太大（超过 1MB）或不是普通文本，无法直接读取。"
    try:
        text = base64.b64decode(d["content"]).decode("utf-8")
    except UnicodeDecodeError:
        return "这个文件是二进制文件，不能当文本显示。"
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n...（已截断，全文共 {len(text)} 字符）"
    return text


@mcp.tool()
def list_branches() -> str:
    """列出所有分支。"""
    d = _get("/branches", {"per_page": 50})
    return "\n".join(b["name"] for b in d) or "没有分支"


@mcp.tool()
def list_commits(branch: str = "", limit: int = 15) -> str:
    """查看最近的提交记录。branch 留空表示默认分支。"""
    params = {"per_page": max(1, min(limit, 50))}
    if branch:
        params["sha"] = branch
    d = _get("/commits", params)
    lines = []
    for c in d:
        msg = c["commit"]["message"].splitlines()[0]
        date = c["commit"]["author"]["date"][:10]
        lines.append(f"{c['sha'][:7]} {date} {msg}")
    return "\n".join(lines) or "没有提交"


@mcp.tool()
def list_issues(state: str = "open", limit: int = 20) -> str:
    """列出 issue。state 可填 open、closed 或 all。"""
    d = _get(
        "/issues",
        {"state": state, "per_page": max(1, min(limit, 50))},
    )
    # GitHub 的 issues 接口会混入 PR，这里过滤掉
    issues = [i for i in d if "pull_request" not in i]
    lines = [f"#{i['number']} [{i['state']}] {i['title']}" for i in issues]
    return "\n".join(lines) or "没有符合条件的 issue"


@mcp.tool()
def get_issue(number: int) -> str:
    """查看某个 issue 的详情。"""
    i = _get(f"/issues/{number}")
    return f"#{i['number']} [{i['state']}] {i['title']}\n\n{i.get('body') or '（无正文）'}"


@mcp.tool()
def list_pull_requests(state: str = "open", limit: int = 20) -> str:
    """列出 PR。state 可填 open、closed 或 all。"""
    d = _get("/pulls", {"state": state, "per_page": max(1, min(limit, 50))})
    lines = [
        f"#{p['number']} [{p['state']}] {p['title']} ({p['head']['ref']} -> {p['base']['ref']})"
        for p in d
    ]
    return "\n".join(lines) or "没有符合条件的 PR"


@mcp.tool()
def get_pull_request(number: int, max_chars: int = 30000) -> str:
    """查看某个 PR 的说明和改动内容（diff）。"""
    p = _get(f"/pulls/{number}")
    files = _get(f"/pulls/{number}/files", {"per_page": 50})
    out = [
        f"#{p['number']} [{p['state']}] {p['title']}",
        f"{p['head']['ref']} -> {p['base']['ref']}",
        "",
        p.get("body") or "（无说明）",
        "",
        f"共改动 {len(files)} 个文件：",
    ]
    for f in files:
        out.append(f"\n--- {f['filename']} ({f['status']}, +{f['additions']} -{f['deletions']})")
        out.append(f.get("patch") or "（无文本差异）")
    text = "\n".join(out)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n...（已截断）"
    return text


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
