"""GitHub 采集适配层（Chronicle / Overview / Quick Start 模式）。

GitHub 的认证、分页、限流、重试、GraphQL、Discussion、wiki 与增量备份
全部委托给成熟项目 ``josegonzalez/python-github-backup``。本模块只做三件事：

1. 以 subprocess 调用 ``github-backup``，按剧本模式选择采集范围；
2. 把其落盘的 Git 仓库和 JSON 归一化成 RepoContext；
3. 确定性提取模式专用素材：Overview 的目录树与根级项目文件，
   Quick Start 的贡献者入口文件与可作为“第一个任务”的起步 Issue。

项目明确禁止在已有成熟开源实现时自造 GitHub API 客户端。
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import requests

from .config import DEFAULT_GAME_MODE, GAME_MODES
from .errors import FetchError, UsageError


GITHUB_REST_API = "https://api.github.com"


@dataclass
class Comment:
    author: str
    body: str


@dataclass
class Thread:
    """一条 Issue、PR 或 Discussion，连同它的讨论。"""

    number: int
    title: str
    kind: str  # "issue" | "pr" | "discussion"
    state: str
    author: str
    created_at: str
    comment_count: int
    body: str
    comments: list[Comment] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)


@dataclass
class Release:
    tag: str
    name: str
    published_at: str
    body: str


@dataclass
class Contributor:
    login: str
    contributions: int


@dataclass
class RepoContext:
    """喂给 LLM 的结构化上下文。"""

    owner: str
    name: str
    description: str
    language: str
    stars: int
    created_at: str
    topics: list[str] = field(default_factory=list)
    readme_excerpt: str = ""
    wiki_excerpt: str = ""
    file_tree: str = ""
    project_files: str = ""
    contributors: list[Contributor] = field(default_factory=list)
    releases: list[Release] = field(default_factory=list)
    threads: list[Thread] = field(default_factory=list)
    contributor_files: str = ""
    starter_issues: list[Thread] = field(default_factory=list)
    backup_dir: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def parse_repo(url: str) -> tuple[str, str]:
    """从 URL 或 owner/repo 里解出 (owner, repo)。"""
    text = url.strip().removesuffix(".git")
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+)", text)
    if match:
        return match.group(1), match.group(2)
    parts = text.split("/")
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    raise UsageError(f"无法解析仓库标识：{url!r}（期望 owner/repo 或 GitHub URL）")


# 不使用 --all：上游的 --all 会额外下载 Release 二进制并读取 hooks，可能需要
# 高权限 token，也可能意外拉取数十 GB。这里显式列出生成叙事真正需要的全量数据。
NARRATIVE_BACKUP_FLAGS = (
    "--repositories",
    "--issues",
    "--issue-comments",
    "--issue-events",
    "--issue-timeline",
    "--pulls",
    "--pull-comments",
    "--pull-reviews",
    "--pull-commits",
    "--pull-details",
    "--discussions",
    "--wikis",
    "--releases",
    "--labels",
    "--milestones",
    "--fork",
)

# Overview 模式只需要源码、README/项目文件与发布里程碑。Issue/PR/Discussion
# 对“快速了解项目”没有直接价值，却会显著拖慢首次采集；不放进本 flags 集合。
OVERVIEW_BACKUP_FLAGS = (
    "--repositories",
    "--releases",
    "--wikis",
)

# Quick Start（贡献者上手）面向真的想提交改动的人：除了源码与 wiki，只需要
# Issue 与评论——用它确定性挑出“第一个任务”。Release、PR、Discussion 与
# label 列表对上手路径没有直接价值，不进本 flags 集合。
QUICKSTART_BACKUP_FLAGS = (
    "--repositories",
    "--issues",
    "--issue-comments",
    "--wikis",
)

# 可作为“第一个任务”的 Issue 标签（大小写与空白归一后匹配）。
_STARTER_ISSUE_LABELS = frozenset(
    {
        "good-first-issue",
        "help wanted",
        "help-wanted",
        "first-timers-only",
        "first timers only",
        "beginner",
        "beginner friendly",
        "easy",
        "starter",
        "low hanging fruit",
    }
)

# Overview 上下文中的目录树过滤与截断参数。
_TREE_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "bower_components",
        "dist",
        "build",
        "target",
        ".next",
        ".nuxt",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
        "coverage",
        "htmlcov",
        ".repo2gal",
        "output",
    }
)
_TREE_MAX_DEPTH = 4
_TREE_MAX_LINES = 120
_TREE_MAX_CHARS = 4000

# 根级项目文件：安装/构建/容器配置与贡献入口，是 Overview 的确定性素材来源。
_OVERVIEW_PROJECT_FILES = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "requirements.txt",
    "requirements.in",
    "Pipfile",
    "poetry.lock",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "yarn.lock",
    "tsconfig.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "composer.json",
    "Gemfile",
    "mix.exs",
    "CMakeLists.txt",
    "Makefile",
    "Justfile",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    "ARCHITECTURE.md",
)
_PROJECT_FILE_LIMIT = 1000
_PROJECT_FILES_TOTAL_LIMIT = 6000

# Quick Start 的贡献者入口文件：贡献指南、构建/测试入口、PR 模板与行为准则。
# 路径按仓库内相对路径匹配（例如 .github/CONTRIBUTING.md），不限于根级。
_QUICKSTART_PROJECT_FILES = (
    "CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    "CONTRIBUTING.txt",
    "CONTRIBUTING",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
    "DEVELOPMENT.md",
    "docs/DEVELOPMENT.md",
    "HACKING.md",
    "CODE_OF_CONDUCT.md",
    "CODEOWNERS",
    ".github/CODEOWNERS",
    "Makefile",
    "makefile",
    "Justfile",
    "justfile",
    "Taskfile.yml",
    "tox.ini",
    "noxfile.py",
    "pyproject.toml",
    "setup.py",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "CMakeLists.txt",
    "Dockerfile",
    "docker-compose.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/pull_request_template.md",
    "PULL_REQUEST_TEMPLATE.md",
)

# CI 工作流文件名随仓库而异，按目录前缀确定性发现并限量摘录。
_WORKFLOW_PREFIX = ".github/workflows/"
_WORKFLOW_SUFFIXES = (".yml", ".yaml")
_WORKFLOW_FILE_LIMIT = 3


def run_backup(
    owner: str,
    repo: str,
    backup_root: Path,
    *,
    token: str | None = None,
    organization: bool = False,
    incremental: bool = True,
    backup_flags: Iterable[str] = NARRATIVE_BACKUP_FLAGS,
    log=lambda _msg: None,
    progress=lambda _msg: None,
) -> Path:
    """调用 python-github-backup，返回该仓库的备份目录。

    Token 通过权限为 0600 的临时文件传递，避免出现在进程列表或 shell history。
    Discussion 使用 GraphQL，完整采集必须有 token，因此本适配层直接要求认证。
    ``backup_flags`` 只允许选择预先锁定的显式 flags 集合，不得传上游 ``--all``。
    """
    if not token:
        raise FetchError(
            "python-github-backup 需要 GitHub Token；请设置 GITHUB_TOKEN，"
            "或用 --reuse-backup 读取已有备份"
        )

    sibling = Path(sys.executable).with_name("github-backup")
    executable = str(sibling) if sibling.is_file() else shutil.which("github-backup")
    if not executable:
        raise FetchError("找不到 github-backup，请执行 pip install github-backup")

    backup_root = Path(backup_root).resolve()
    repo_dir = backup_root / "repositories" / repo
    backup_root.mkdir(parents=True, exist_ok=True)

    command = [
        executable,
        owner,
        "--output-directory",
        str(backup_root),
        "--repository",
        repo,
        *backup_flags,
    ]
    if organization:
        command.append("--organization")
    command.append("--private")
    if token.startswith("ghs_"):
        # GitHub Actions GITHUB_TOKEN is an installation token. Upstream supports it
        # through its public GitHub App mode; classic Basic auth returns 401 for /user.
        command.append("--as-app")
    if incremental and repo_dir.exists():
        command.append("--incremental")
        # 已备份且未变更的 issue/PR 沿用本地文件，不再重拉评论、评审、提交与
        # 时间线；列表请求仍每次执行，用来发现新实体。
        command.append("--incremental-by-files")

    token_file: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
            handle.write(token)
            token_file = handle.name
        os.chmod(token_file, 0o600)
        option = "--token-fine" if token.startswith("github_pat_") else "--token"
        command.extend((option, Path(token_file).as_uri()))

        log(f"调用 python-github-backup 采集 {owner}/{repo}")
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        output: list[str] = []
        if process.stdout:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if line:
                    output.append(line)
                    progress(line)
        returncode = process.wait()
        if returncode:
            message = output[-1] if output else "无错误详情"
            raise FetchError(f"github-backup 退出码 {returncode}：{message}")
    finally:
        if token_file:
            Path(token_file).unlink(missing_ok=True)

    if not repo_dir.exists():
        raise FetchError(f"github-backup 未产出预期目录：{repo_dir}")
    log(f"原始备份已保存：{repo_dir}")
    return repo_dir


def fetch_repository_metadata(
    owner: str,
    repo: str,
    token: str,
    *,
    log=lambda _msg: None,
) -> dict[str, Any]:
    """通过官方 GitHub REST API 补齐上游不落盘的仓库概览。

    这是仓库数据采集模块唯一允许的直接网络补充。URL 固定为
    ``api.github.com/repos/{owner}/{repo}``；不得改成抓取 GitHub HTML 页面。
    失败时保留 github-backup 主流程，不把非关键元数据升级为致命错误。
    """
    log("通过官方 GitHub REST API 获取仓库概览")
    try:
        response = requests.get(
            f"{GITHUB_REST_API}/repos/{owner}/{repo}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Repo2Gal",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        log(f"仓库概览获取失败（{exc}），继续使用备份数据")
        return {}
    if not response.ok:
        log(f"仓库概览获取失败（HTTP {response.status_code}），继续使用备份数据")
        return {}
    try:
        data = response.json()
    except ValueError:
        log("仓库概览响应不是合法 JSON，继续使用备份数据")
        return {}
    return data if isinstance(data, dict) else {}


def _metadata_path(repo_backup_dir: Path) -> Path:
    return repo_backup_dir / "repo2gal-repository.json"


def _read_metadata(repo_backup_dir: Path) -> dict[str, Any]:
    path = _metadata_path(repo_backup_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _clean_body(text: str | None, limit: int) -> str:
    """压掉 Markdown 噪声并限制单段长度。"""
    if not text:
        return ""
    text = re.sub(r"```.*?```", "[代码块]", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit] + "…" if len(text) > limit else text


def _load_json_files(directory: Path) -> Iterable[dict[str, Any]]:
    if not directory.is_dir():
        return []
    values: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            values.append(data)
    return values


def _login(actor: Any) -> str:
    if isinstance(actor, dict):
        return actor.get("login") or actor.get("name") or "unknown"
    return "unknown"


def _comments(items: Iterable[dict[str, Any]], limit: int = 20) -> list[Comment]:
    result: list[Comment] = []
    for item in items:
        body = _clean_body(item.get("body") or item.get("bodyText"), 500)
        if body:
            result.append(Comment(author=_login(item.get("user") or item.get("author")), body=body))
        for reply in item.get("reply_data") or []:
            reply_body = _clean_body(reply.get("body") or reply.get("bodyText"), 500)
            if reply_body:
                result.append(Comment(author=_login(reply.get("author")), body=reply_body))
        if len(result) >= limit:
            break
    return result[:limit]


def _labels(items: Any) -> list[str]:
    """Issue/PR JSON 的 labels 是对象数组；只保留名称，供起步任务筛选使用。"""
    names: list[str] = []
    for item in items or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _is_starter_issue(thread: Thread) -> bool:
    """判断 Issue 是否带“新人可上手”标签（标签名先做大小写与空白归一）。"""
    for label in thread.labels:
        normalized = " ".join(label.casefold().split())
        if normalized in _STARTER_ISSUE_LABELS or normalized.startswith("good first issue"):
            return True
    return False


def _thread_from_issue(data: dict[str, Any]) -> Thread:
    comments = _comments(data.get("comment_data") or [])
    return Thread(
        number=data.get("number", 0),
        title=data.get("title") or "（无标题）",
        kind="issue",
        state=data.get("state") or "unknown",
        author=_login(data.get("user")),
        created_at=(data.get("created_at") or "")[:10],
        comment_count=max(data.get("comments", 0), len(comments)),
        body=_clean_body(data.get("body"), 800),
        comments=comments,
        labels=_labels(data.get("labels")),
    )


def _thread_from_pull(data: dict[str, Any]) -> Thread:
    raw_comments = [
        *(data.get("comment_regular_data") or []),
        *(data.get("comment_data") or []),
        *(data.get("review_data") or []),
    ]
    comments = _comments(raw_comments)
    count = data.get("comments", 0) + data.get("review_comments", 0)
    return Thread(
        number=data.get("number", 0),
        title=data.get("title") or "（无标题）",
        kind="pr",
        state=data.get("state") or "unknown",
        author=_login(data.get("user")),
        created_at=(data.get("created_at") or "")[:10],
        comment_count=max(count, len(comments)),
        body=_clean_body(data.get("body"), 800),
        comments=comments,
        labels=_labels(data.get("labels")),
    )


def _thread_from_discussion(data: dict[str, Any]) -> Thread:
    comments = _comments(data.get("comment_data") or [])
    return Thread(
        number=data.get("number", 0),
        title=data.get("title") or "（无标题）",
        kind="discussion",
        state="closed" if data.get("closed") else "open",
        author=_login(data.get("author")),
        created_at=(data.get("createdAt") or "")[:10],
        comment_count=max(data.get("comment_count", 0), len(comments)),
        body=_clean_body(data.get("body") or data.get("bodyText"), 800),
        comments=comments,
    )


def _read_text_candidates(
    directory: Path,
    names: tuple[str, ...],
    limit: int,
    *,
    reference: str | None = None,
    git_files: list[str] | None = None,
) -> str:
    if not directory.is_dir():
        return ""
    if reference is None or git_files is None:
        reference, files = _git_files(directory)
    else:
        files = git_files
    lower_names = {name.lower() for name in names}
    for name in files:
        if "/" not in name and name.lower() in lower_names:
            text = _git_output(directory, "show", f"{reference}:{name}")
            if text:
                return _clean_body(text, limit)
    for path in directory.iterdir():
        if path.is_file() and path.name.lower() in lower_names:
            try:
                return _clean_body(path.read_text(encoding="utf-8"), limit)
            except (OSError, UnicodeDecodeError):
                pass
    return ""


def _clean_project_file(text: str, limit: int) -> str:
    """项目配置文件要尽量原样进入 prompt，不做 Markdown 链接清理。"""
    text = text.strip()
    return text[:limit] + "\n…" if len(text) > limit else text


def _read_project_files(
    directory: Path,
    *,
    reference: str,
    git_files: list[str],
    names: tuple[str, ...] = _OVERVIEW_PROJECT_FILES,
) -> str:
    """读取指定项目文件摘录（安装/构建/贡献入口）。

    ``names`` 使用仓库内相对路径匹配，因此既能取根级文件，也能取
    ``.github/workflows/ci.yml`` 这类嵌套路径。
    """
    if not directory.is_dir():
        return ""
    listed = {path.casefold() for path in git_files}
    chunks: list[str] = []
    for name in names:
        if sum(map(len, chunks)) >= _PROJECT_FILES_TOTAL_LIMIT:
            break
        if reference or git_files:
            if name.casefold() not in listed:
                continue
        elif not (directory / name).is_file():
            continue
        if reference:
            raw = _git_output(directory, "show", f"{reference}:{name}")
        else:
            try:
                raw = (directory / name).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        if not raw:
            continue
        chunks.append(f"### {name}\n{_clean_project_file(raw, _PROJECT_FILE_LIMIT)}")
    return "\n\n".join(chunks)


def _discover_workflow_files(directory: Path, git_files: list[str]) -> tuple[str, ...]:
    """确定性发现 CI 工作流文件；文件名随仓库而异，因此按前缀扫描并限量。"""
    if git_files:
        candidates = [
            path
            for path in git_files
            if path.startswith(_WORKFLOW_PREFIX)
            and path.lower().endswith(_WORKFLOW_SUFFIXES)
        ]
    else:
        candidates = [
            str(path.relative_to(directory))
            for path in sorted((directory / ".github" / "workflows").glob("*"))
            if path.is_file() and path.name.lower().endswith(_WORKFLOW_SUFFIXES)
        ]
    return tuple(sorted(candidates)[:_WORKFLOW_FILE_LIMIT])


def _read_contributor_files(
    directory: Path,
    *,
    reference: str,
    git_files: list[str],
) -> str:
    """读取 Quick Start 需要的贡献者入口文件与 CI 定义摘录。"""
    names = _QUICKSTART_PROJECT_FILES + _discover_workflow_files(directory, git_files)
    return _read_project_files(
        directory, reference=reference, git_files=git_files, names=names
    )


def _local_file_paths(directory: Path) -> list[str]:
    """非 Git 备份的兜底文件列表。"""
    return sorted(
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )


def _build_file_tree(directory: Path, git_files: list[str]) -> str:
    """把源码文件列表压成浅层目录树，供 Overview prompt 使用。

    只做确定性、有上限的文本渲染；不进压缩包、不读文件内容，也不依赖任何
    第三方树生成器。依赖目录、构建产物与缓存目录整体跳过。
    """
    paths = git_files or _local_file_paths(directory)
    lines: list[str] = []
    ancestors: list[str] = []
    omitted = 0
    for raw in sorted(paths):
        parts = raw.replace("\\", "/").split("/")
        parts = [part for part in parts if part]
        if not parts:
            continue
        if any(part in _TREE_IGNORED_DIRS for part in parts[:-1]):
            continue
        depth = len(parts) - 1
        if depth > _TREE_MAX_DEPTH:
            omitted += 1
            continue
        common = 0
        while (
            common < min(len(ancestors), len(parts) - 1)
            and ancestors[common] == parts[common]
        ):
            common += 1
        for index in range(common, len(parts) - 1):
            lines.append(f"{'  ' * index}{parts[index]}/")
        ancestors = parts[:-1]
        lines.append(f"{'  ' * depth}{parts[-1]}")
    if omitted:
        lines.append(f"…（另有 {omitted} 个更深层文件未列出）")
    if len(lines) > _TREE_MAX_LINES:
        lines = lines[:_TREE_MAX_LINES] + [f"…（目录树超过 {_TREE_MAX_LINES} 行，已截断）"]
    text = "\n".join(lines)
    if len(text) > _TREE_MAX_CHARS:
        text = text[:_TREE_MAX_CHARS] + "\n…（目录树过长，已截断）"
    return text


def _read_wiki(directory: Path, limit: int = 3000) -> str:
    if not directory.is_dir():
        return ""
    chunks: list[str] = []
    reference, git_paths = _git_files(directory)
    paths = git_paths or [
        str(path.relative_to(directory))
        for path in sorted(directory.rglob("*.md"))
        if ".git" not in path.parts
    ]
    for relative in paths:
        if not relative.lower().endswith(".md"):
            continue
        if reference:
            raw = _git_output(directory, "show", f"{reference}:{relative}")
        else:
            try:
                raw = (directory / relative).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        text = _clean_body(raw, 1000)
        if text:
            chunks.append(f"### {Path(relative).stem}\n{text}")
        if sum(map(len, chunks)) >= limit:
            break
    return _clean_body("\n\n".join(chunks), limit)


def _git_output(repo_dir: Path, *args: str) -> str:
    if not (repo_dir / ".git").exists():
        return ""
    result = subprocess.run(
        ["git", "-C", str(repo_dir), *args], text=True, capture_output=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_files(repo_dir: Path) -> tuple[str, list[str]]:
    """返回最新远端引用及其文件列表，避免读取增量备份中的陈旧工作树。"""
    if not (repo_dir / ".git").exists():
        return "", []
    reference = _git_output(
        repo_dir, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"
    )
    if not reference:
        remotes = _git_output(
            repo_dir, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"
        ).splitlines()
        reference = next(
            (item for preferred in ("origin/main", "origin/master") for item in remotes if item == preferred),
            remotes[0] if remotes else "HEAD",
        )
    files = _git_output(repo_dir, "ls-tree", "-r", "--name-only", reference).splitlines()
    return reference, files


def _detect_language(repo_dir: Path, *, git_files: list[str] | None = None) -> str:
    if git_files is None:
        _, git_files = _git_files(repo_dir)
    git_paths = git_files
    if git_paths:
        extensions = Counter(Path(path).suffix.lower() for path in git_paths)
    else:
        extensions = Counter(
            path.suffix.lower()
            for path in repo_dir.rglob("*")
            if path.is_file() and ".git" not in path.parts
        )
    mapping = {
        ".py": "Python",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".js": "JavaScript",
        ".jsx": "JavaScript",
        ".rs": "Rust",
        ".go": "Go",
        ".java": "Java",
        ".kt": "Kotlin",
        ".cpp": "C++",
        ".c": "C",
        ".rb": "Ruby",
        ".php": "PHP",
    }
    known: Counter[str] = Counter()
    for extension, count in extensions.items():
        if extension in mapping:
            known[mapping[extension]] += count
    return known.most_common(1)[0][0] if known else "未知"


def context_from_backup(
    owner: str,
    repo: str,
    repo_backup_dir: Path,
    *,
    metadata: dict[str, Any] | None = None,
    top_threads: int = 12,
    mode: str = DEFAULT_GAME_MODE,
    log=lambda _msg: None,
) -> RepoContext:
    """把 python-github-backup 的落盘结果归一化成 RepoContext。"""
    if mode not in GAME_MODES:
        raise UsageError(f"未知剧本模式：{mode}")
    repo_backup_dir = Path(repo_backup_dir)
    source_dir = repo_backup_dir / "repository"
    metadata = metadata if metadata is not None else _read_metadata(repo_backup_dir)

    reference, git_files = _git_files(source_dir)
    starter_issues: list[Thread] = []
    if mode == "chronicle":
        threads = [
            *(_thread_from_issue(item) for item in _load_json_files(repo_backup_dir / "issues")),
            *(_thread_from_pull(item) for item in _load_json_files(repo_backup_dir / "pulls")),
            *(
                _thread_from_discussion(item)
                for item in _load_json_files(repo_backup_dir / "discussions")
            ),
        ]
        threads.sort(key=lambda item: (item.comment_count, item.created_at), reverse=True)
        threads = threads[:top_threads]
    elif mode == "quickstart":
        # Quick Start 只读 Issue：把开放且带新人标签的 Issue 作为起步任务，
        # 历史争论不进贡献者上手剧本；起步任务按编号倒序，最新的排在前面。
        threads = []
        starter_issues = [
            _thread_from_issue(item)
            for item in _load_json_files(repo_backup_dir / "issues")
        ]
        starter_issues = [
            item
            for item in starter_issues
            if item.state == "open" and _is_starter_issue(item)
        ]
        starter_issues.sort(key=lambda item: item.number, reverse=True)
        starter_issues = starter_issues[:top_threads]
    else:
        # Overview 不读社区讨论：既省去数千个 JSON 的解析时间，也从数据源上
        # 阻止历史争论进入“快速了解项目”的 prompt。
        threads = []

    activity: Counter[str] = Counter()
    for thread in (*threads, *starter_issues):
        if thread.author != "unknown":
            activity[thread.author] += 1
        activity.update(comment.author for comment in thread.comments if comment.author != "unknown")

    releases = [
        Release(
            tag=item.get("tag_name") or "",
            name=item.get("name") or item.get("tag_name") or "",
            published_at=(item.get("published_at") or item.get("created_at") or "")[:10],
            body=_clean_body(item.get("body"), 500),
        )
        for item in _load_json_files(repo_backup_dir / "releases")
        if not item.get("draft")
    ]
    releases.sort(key=lambda item: item.published_at, reverse=True)

    readme = _read_text_candidates(
        source_dir,
        ("README.md", "README.rst", "README.txt", "README"),
        3000,
        reference=reference,
        git_files=git_files,
    )
    readme_description = next(
        (line.lstrip("#= ") for line in readme.splitlines() if line.strip()), ""
    )
    roots = _git_output(
        source_dir, "rev-list", "--max-parents=0", reference or "HEAD"
    ).splitlines()
    first_commit = _git_output(source_dir, "show", "-s", "--format=%cs", roots[0]) if roots else ""

    context = RepoContext(
        owner=(metadata.get("owner") or {}).get("login") or owner,
        name=metadata.get("name") or repo,
        description=metadata.get("description") or readme_description,
        language=metadata.get("language") or _detect_language(source_dir, git_files=git_files),
        stars=metadata.get("stargazers_count") or 0,
        created_at=(metadata.get("created_at") or first_commit)[:10],
        topics=metadata.get("topics") or [],
        readme_excerpt=readme,
        wiki_excerpt=_read_wiki(repo_backup_dir / "wiki"),
        file_tree=_build_file_tree(source_dir, git_files),
        project_files=_read_project_files(
            source_dir, reference=reference, git_files=git_files
        ),
        contributor_files=(
            _read_contributor_files(source_dir, reference=reference, git_files=git_files)
            if mode == "quickstart"
            else ""
        ),
        contributors=[Contributor(login=name, contributions=count) for name, count in activity.most_common(8)],
        releases=releases[:10],
        threads=threads,
        starter_issues=starter_issues,
        backup_dir=str(repo_backup_dir),
    )
    if mode == "overview":
        material_summary = "社区讨论（Overview 已跳过）"
    elif mode == "quickstart":
        material_summary = f"{len(context.starter_issues)} 个起步任务（Issue good first issue 等）"
    else:
        material_summary = f"{len(context.threads)} 条热门讨论（含 Discussion）"
    log(
        f"上下文：{material_summary}，"
        f"{len(context.releases)} 个 Release，wiki={'有' if context.wiki_excerpt else '无'}，"
        f"目录树={'有' if context.file_tree else '无'}，"
        f"项目文件={'有' if context.project_files else '无'}，"
        f"贡献者入口={'有' if context.contributor_files else '无'}"
    )
    if mode == "overview":
        if not context.readme_excerpt and not context.project_files and not context.file_tree:
            raise FetchError("备份中没有 README、项目配置文件或源码目录结构，素材不足以生成仓库概览")
    elif mode == "quickstart":
        if not context.readme_excerpt and not context.contributor_files and not context.file_tree:
            raise FetchError(
                "备份中没有 README、贡献者入口文件或源码目录结构，素材不足以生成贡献者上手剧本"
            )
    elif not context.threads and not context.readme_excerpt and not context.wiki_excerpt:
        raise FetchError("备份中没有 README、wiki 或社区讨论，素材不足以生成剧情")
    return context


def fetch_context(
    owner: str,
    repo: str,
    *,
    backup_root: Path,
    token: str | None = None,
    organization: bool = False,
    top_threads: int = 12,
    reuse_backup: bool = False,
    mode: str = DEFAULT_GAME_MODE,
    log=lambda _msg: None,
    progress=lambda _msg: None,
) -> RepoContext:
    """执行备份（或复用已有备份）并构建上下文。"""
    if mode not in GAME_MODES:
        raise UsageError(f"未知剧本模式：{mode}")
    if not reuse_backup and not token:
        raise FetchError(
            "python-github-backup 需要 GitHub Token；请设置 GITHUB_TOKEN，"
            "或用 --reuse-backup 读取已有备份"
        )
    expected = Path(backup_root).resolve() / "repositories" / repo
    if reuse_backup:
        if not expected.exists():
            raise FetchError(f"--reuse-backup 指定的备份不存在：{expected}")
        repo_dir = expected
        log(f"复用原始备份：{repo_dir}")
        metadata = _read_metadata(repo_dir)
    else:
        metadata = fetch_repository_metadata(owner, repo, token or "", log=log)
        if mode == "chronicle":
            backup_flags = NARRATIVE_BACKUP_FLAGS
        elif mode == "quickstart":
            backup_flags = QUICKSTART_BACKUP_FLAGS
        else:
            backup_flags = OVERVIEW_BACKUP_FLAGS
        repo_dir = run_backup(
            owner,
            repo,
            Path(backup_root),
            token=token,
            organization=organization,
            backup_flags=backup_flags,
            log=log,
            progress=progress,
        )
        if metadata:
            _metadata_path(repo_dir).write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    return context_from_backup(
        owner,
        repo,
        repo_dir,
        metadata=metadata,
        top_threads=top_threads,
        mode=mode,
        log=log,
    )
