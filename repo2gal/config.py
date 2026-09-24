"""集中配置：默认值、环境解析、路径常量与密钥脱敏显示。

所有“默认值写在哪”的问题在这里一次性解决，禁止其他模块各自硬编码
base_url / model / 目录规则。显示用途的脱敏（终端输出）也集中在这里；
错误正文脱敏在 ``errors.redact_error``。
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_LLM_TIMEOUT = 300

# Director 流程：第三轮导演 JSON 校验失败时的最大重试次数（0 表示不重试）。
DEFAULT_FORMAT_RETRIES = 2

# 剧本生成模式。Chronicle（编年史）是 v0.1.0 起的默认模式；
# Overview（仓库概览）是 v0.6.0 新增的第二种游戏模式；
# Quick Start（贡献者上手）是 v0.8.0 新增的第三种游戏模式。
GAME_MODES = ("chronicle", "overview", "quickstart")
DEFAULT_GAME_MODE = "chronicle"
GAME_MODE_TITLES = {
    "chronicle": "编年史",
    "overview": "仓库概览",
    "quickstart": "贡献者上手",
}

# 不使用旁白的模式：全部台词都必须由角色亲口说出。
# Overview 自 v0.6.2 起如此（WebGAL 4.6.2 会把无说话人的文本渲染成上一句 speaker）；
# Quick Start 与之同构——带新人上手的维护者全程说话，不靠旁白解说。
NARRATION_FREE_MODES = ("overview", "quickstart")

# WebGAL 发行版自带的兼容默认素材；使用 Asset Pack 时仍与包内资源并存。
DEFAULT_BACKGROUNDS = ["bg.webp", "WebGalEnter.webp", "WebGAL_New_Enter_Image.webp"]
DEFAULT_BGM = ["s_Title.mp3"]


def env_value(name: str) -> str | None:
    """读取并去空白的环境变量；空白视为未设置。"""
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def resolve_base_url(base_url: str | None) -> str:
    return (base_url or env_value("REPO2GAL_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def resolve_model_chain(model: str | None = None) -> tuple[str, ...]:
    """模型链：``REPO2GAL_MODEL`` 支持逗号分隔，首个为主模型，其余按顺序降级。

    只填一个模型名是链长为 1 的特例，行为与不配降级完全一致。免费端点对单个
    模型的过载与配额都按模型独立计算，链让「换一个再用」成为默认的恢复方式。
    """
    raw = model or env_value("REPO2GAL_MODEL") or DEFAULT_MODEL
    chain: list[str] = []
    for item in raw.split(","):
        name = item.strip()
        if name and name not in chain:
            chain.append(name)
    return tuple(chain) or (DEFAULT_MODEL,)


def resolve_api_key() -> str | None:
    """REPO2GAL_API_KEY 优先，OPENAI_API_KEY 兜底。"""
    return env_value("REPO2GAL_API_KEY") or env_value("OPENAI_API_KEY")


def resolve_github_token() -> str | None:
    return env_value("GITHUB_TOKEN")


def default_backup_root(owner: str) -> Path:
    return Path(".repo2gal") / "backups" / owner


def default_output_dir(repo: str, mode: str = DEFAULT_GAME_MODE) -> Path:
    """默认产物目录；Chronicle 保持 output/<repo>，其他模式加模式后缀。"""
    path = Path("output") / repo
    return path if mode == DEFAULT_GAME_MODE else Path(f"{path}-{mode}")


def webgal_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "repo2gal"


def mask_secret(value: str) -> str:
    """保留少量首尾字符，禁止把完整凭据写到终端。"""
    if len(value) <= 4:
        return "*" * len(value)
    visible = 2 if len(value) <= 10 else 4
    return f"{value[:visible]}{'*' * min(8, len(value) - visible * 2)}{value[-visible:]}"
