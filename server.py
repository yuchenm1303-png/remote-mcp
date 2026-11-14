"""Loom 仓库的完整读写 MCP 连接器（第二版）。

能力：查看文件/提交/分支/issue/PR/搜索代码/Actions 状态，
      以及新建分支、提交多个文件、开 PR、评论、管理 issue、（可选）合并 PR。

安全设计：
1. 连接器地址里带一段只有你知道的随机字符（SECRET_PATH），没有它的人访问不了。
2. 只能操作 GITHUB_OWNER/GITHUB_REPO 这一个仓库，token 也应只授权给这一个仓库。
3. 默认不允许直接往受保护分支（默认 main）提交，也不允许合并 PR。
   想放开，在 Render 的环境变量里设置：
     ALLOW_PROTECTED_WRITE=true   允许直接提交到受保护分支
     ALLOW_MERGE=true             允许合并 PR
"""

import os

import httpx
from mcp.server.fastmcp import FastMCP

OWNER = os.environ.get("GITHUB_OWNER", "yuchenm1303-png")
REPO = os.environ.get("GITHUB_REPO", "Loom")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
SECRET = os.environ.get("SECRET_PATH", "").strip("/")
PROTECTED = {
    b.strip()
    for b in os.environ.get("PROTECTED_BRANCHES", "main").split(",")
    if b.strip()
}
ALLOW_MERGE = os.environ.get("ALLOW_MERGE", "false").lower() == "true"
ALLOW_PROTECTED_WRITE = (
    os.environ.get("ALLOW_PROTECTED_WRITE", "false").lower() == "true"
)

# 没设置密钥或 token 就拒绝启动，避免把一个没保护的写入工具放到公网上
if len(SECRET) < 16:
    raise SystemExit("请先在环境变量里设置 SECRET_PATH（至少 16 位随机字符）。")
if not TOKEN:
    raise SystemExit("请先在环境变量里设置 GITHUB_TOKEN。")

API = "https://api.github.com"

mcp = FastMCP(
    "loom-github",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8000")),
    streamable_http_path=f"/{SECRET}/mcp",
)


# ---------- 基础函数 ----------


def _headers():
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "loom-mcp",
        "Authorization": f"Bearer {TOKEN}",
    }


def _req(method, path, json=None, params=None):
    r = httpx.request(
        method,
        f"{API}/repos/{OWNER}/{REPO}{path}",
        headers=_headers(),
        json=json,
        params=params,
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub 返回 {r.status_code}: {r.text[:400]}")
    return r.json() if r.content else {}


def _get(path, params=None):
    return _req("GET", path, params=params)


def _check_writable(branch):
    if branch in PROTECTED and not ALLOW_PROTECTED_WRITE:
        raise RuntimeError(
            f"分支 {branch} 受保护，不能直接修改。请先新建分支，提交后再开 PR。"
            "（如需放开，在 Render 环境变量设置 ALLOW_PROTECTED_WRITE=true）"
        )


# ---------- 读取类工具 ----------


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
    import base64

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
def search_code(query: str, limit: int = 15) -> str:
    """在仓库代码里搜索关键字，返回匹配的文件路径。"""
    r = httpx.get(
        f"{API}/search/code",
        headers=_headers(),
        params={"q": f"{query} repo:{OWNER}/{REPO}", "per_page": max(1, min(limit, 30))},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub 返回 {r.status_code}: {r.text[:300]}")
    items = r.json().get("items", [])
    return "\n".join(i["path"] for i in items) or "没有找到匹配的文件"


@mcp.tool()
def list_branches() -> str:
    """列出所有分支。"""
    d = _get("/branches", {"per_page": 100})
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
    d = _get("/issues", {"state": state, "per_page": max(1, min(limit, 50))})
    issues = [i for i in d if "pull_request" not in i]
    lines = [f"#{i['number']} [{i['state']}] {i['title']}" for i in issues]
    return "\n".join(lines) or "没有符合条件的 issue"


@mcp.tool()
def get_issue(number: int) -> str:
    """查看某个 issue 的详情和评论。"""
    i = _get(f"/issues/{number}")
    out = [f"#{i['number']} [{i['state']}] {i['title']}", "", i.get("body") or "（无正文）"]
    comments = _get(f"/issues/{number}/comments", {"per_page": 30})
    for c in comments:
        out.append(f"\n--- {c['user']['login']}:\n{c['body']}")
    return "\n".join(out)


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
    files = _get(f"/pulls/{number}/files", {"per_page": 100})
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


@mcp.tool()
def list_workflow_runs(limit: int = 10) -> str:
    """查看最近的 GitHub Actions 运行状态。"""
    d = _get("/actions/runs", {"per_page": max(1, min(limit, 30))})
    lines = []
    for r in d.get("workflow_runs", []):
        lines.append(
            f"{r['name']} [{r['status']}/{r.get('conclusion')}] "
            f"{r['head_branch']} {r['created_at'][:16]}"
        )
    return "\n".join(lines) or "没有运行记录"


# ---------- 写入类工具 ----------


@mcp.tool()
def create_branch(name: str, from_branch: str = "") -> str:
    """从某个分支新建分支。from_branch 留空表示从默认分支创建。"""
    base = from_branch or _get("")["default_branch"]
    sha = _get(f"/git/ref/heads/{base}")["object"]["sha"]
    _req("POST", "/git/refs", json={"ref": f"refs/heads/{name}", "sha": sha})
    return f"已从 {base} 创建分支 {name}"


@mcp.tool()
def delete_branch(name: str) -> str:
    """删除一个分支（受保护分支不能删）。"""
    _check_writable(name)
    _req("DELETE", f"/git/refs/heads/{name}")
    return f"已删除分支 {name}"


@mcp.tool()
def commit_files(
    branch: str,
    message: str,
    files: list[dict] | None = None,
    delete_paths: list[str] | None = None,
) -> str:
    """在一个分支上提交一次改动（一次可改多个文件）。

    files: 要新建或覆盖的文本文件列表，每项形如 {"path": "docs/a.md", "content": "文件全文"}。
    delete_paths: 要删除的文件路径列表。
    只支持文本文件。受保护分支（默认 main）不能直接提交。
    """
    _check_writable(branch)
    entries = []
    for f in files or []:
        entries.append(
            {
                "path": f["path"].strip("/"),
                "mode": "100644",
                "type": "blob",
                "content": f["content"],
            }
        )
    for p in delete_paths or []:
        entries.append({"path": p.strip("/"), "mode": "100644", "type": "blob", "sha": None})
    if not entries:
        raise RuntimeError("没有要提交的内容")

    head = _get(f"/git/ref/heads/{branch}")["object"]["sha"]
    base_tree = _get(f"/git/commits/{head}")["tree"]["sha"]
    tree = _req("POST", "/git/trees", json={"base_tree": base_tree, "tree": entries})
    commit = _req(
        "POST",
        "/git/commits",
        json={"message": message, "tree": tree["sha"], "parents": [head]},
    )
    _req("PATCH", f"/git/refs/heads/{branch}", json={"sha": commit["sha"]})
    return f"已提交到 {branch}：{commit['sha'][:7]}（{len(entries)} 个文件改动）"


@mcp.tool()
def create_pull_request(
    title: str, head: str, base: str = "", body: str = "", draft: bool = False
) -> str:
    """新建 PR。head 是有改动的分支，base 留空表示合并到默认分支。"""
    base = base or _get("")["default_branch"]
    p = _req(
        "POST",
        "/pulls",
        json={"title": title, "head": head, "base": base, "body": body, "draft": draft},
    )
    return f"已创建 PR #{p['number']}: {p['html_url']}"


@mcp.tool()
def merge_pull_request(number: int, method: str = "squash") -> str:
    """合并 PR。method 可填 merge、squash 或 rebase。默认关闭，需设置 ALLOW_MERGE=true。"""
    if not ALLOW_MERGE:
        raise RuntimeError(
            "合并功能默认关闭，请你在 GitHub 上手动合并。"
            "（如需开启，在 Render 环境变量设置 ALLOW_MERGE=true）"
        )
    r = _req("PUT", f"/pulls/{number}/merge", json={"merge_method": method})
    return r.get("message", "已合并")


@mcp.tool()
def close_pull_request(number: int) -> str:
    """关闭一个 PR（不合并）。"""
    _req("PATCH", f"/pulls/{number}", json={"state": "closed"})
    return f"已关闭 PR #{number}"


@mcp.tool()
def create_issue(title: str, body: str = "", labels: list[str] | None = None) -> str:
    """新建 issue。"""
    payload = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    i = _req("POST", "/issues", json=payload)
    return f"已创建 issue #{i['number']}: {i['html_url']}"


@mcp.tool()
def update_issue(number: int, state: str = "", title: str = "", body: str = "") -> str:
    """修改 issue。state 可填 open 或 closed；不想改的项留空。"""
    payload = {}
    if state:
        payload["state"] = state
    if title:
        payload["title"] = title
    if body:
        payload["body"] = body
    if not payload:
        raise RuntimeError("没有要修改的内容")
    _req("PATCH", f"/issues/{number}", json=payload)
    return f"已更新 issue #{number}"


@mcp.tool()
def add_comment(number: int, body: str) -> str:
    """在 issue 或 PR 下发表评论。"""
    c = _req("POST", f"/issues/{number}/comments", json={"body": body})
    return f"已评论: {c['html_url']}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
