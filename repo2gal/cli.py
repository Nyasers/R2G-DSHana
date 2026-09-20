"""repo2gal 命令行入口：参数解析与结果渲染。

流程编排全部在 ``pipeline.py``；本文件只做三件事：
1. 把 CLI 参数映射为 ``RunOptions``；
2. 调用流水线并转发进度/日志回调；
3. 按统一错误类型的 exit_code 退出，未预期异常兜底 exit 1。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import click

from .asset_pack import init_asset_pack, load_asset_pack
from .config import (
    DEFAULT_FORMAT_RETRIES,
    DEFAULT_GAME_MODE,
    DEFAULT_LLM_TIMEOUT,
    GAME_MODES,
    default_backup_root,
    default_output_dir,
    resolve_api_key,
    resolve_base_url,
    resolve_github_token,
    resolve_model,
    resolve_model_fallbacks,
)
from .errors import Repo2GalError
from .fetcher import parse_repo
from .pipeline import RunOptions, run_pipeline
from .performance import DEFAULT_PROFILE, PROFILES


def _log(msg: str) -> None:
    click.echo(click.style("·", fg="cyan") + f" {msg}")


def _warn(msg: str) -> None:
    click.echo(click.style("!", fg="yellow") + f" {msg}")


def _progress(msg: str) -> None:
    click.echo(click.style("  ↳", fg="blue") + f" {msg}")


def _die(msg: str, code: int) -> None:
    click.echo(click.style("✗ ", fg="red") + msg, err=True)
    sys.exit(code)


@click.command(name="repo2gal")
@click.argument("repo")
@click.option(
    "--mode",
    type=click.Choice(sorted(GAME_MODES)),
    default=DEFAULT_GAME_MODE,
    show_default=True,
    help="剧本模式：chronicle 编年史 / overview 仓库概览 / quickstart 贡献者上手",
)
@click.option(
    "--output",
    "-o",
    default=None,
    type=click.Path(),
    help="产物目录；默认 ./output/<repo>，非 chronicle 模式为 ./output/<repo>-<mode>",
)
@click.option("--model", default=None, help="模型名，默认取 REPO2GAL_MODEL 或 deepseek-v4-pro")
@click.option("--base-url", default=None, help="OpenAI 兼容端点，默认取 REPO2GAL_BASE_URL")
@click.option(
    "--threads",
    default=12,
    show_default=True,
    help="chronicle 选入上下文的热门讨论数 / quickstart 的起步任务数（overview 忽略）",
)
@click.option(
    "--backup-dir",
    type=click.Path(),
    default=None,
    help="python-github-backup 原始数据目录，默认 .repo2gal/backups/<owner>",
)
@click.option("--reuse-backup", is_flag=True, help="不联网，复用 --backup-dir 中的已有备份")
@click.option("--organization", is_flag=True, help="目标 owner 是 GitHub Organization")
@click.option("--dry-run", is_flag=True, help="只抓数据并打印第一轮创作 prompt，不调用 LLM")
@click.option("--script", type=click.Path(path_type=Path), help="跳过 LLM，改用现成 WebGAL 脚本文件")
@click.option("--save-prompt", type=click.Path(), help="把第一轮创作 prompt 存盘，便于调试")
@click.option("--strict", is_flag=True, help="validator 有降级即判失败")
@click.option(
    "--profile",
    type=click.Choice(sorted(PROFILES)),
    default=DEFAULT_PROFILE,
    show_default=True,
    help="演出风格与预算（导演 JSON 校验用）",
)
@click.option(
    "--format-retries",
    type=click.IntRange(min=0),
    default=DEFAULT_FORMAT_RETRIES,
    show_default=True,
    help="导演 JSON 校验失败时打回第三轮的重试次数；耗尽后走确定性草稿兜底",
)
@click.option(
    "--save-stage-outputs",
    type=click.Path(path_type=Path),
    help="保存三轮阶段产物目录：草稿、批注、导演 JSON 各次尝试与校验报告",
)
@click.option(
    "--asset-pack",
    type=click.Path(path_type=Path),
    help="使用一个已通过校验的本地 Asset Pack v1 目录",
)
@click.option(
    "--public-assets",
    is_flag=True,
    help="按公开发布标准校验素材许可证（拒绝 LicenseRef）",
)
@click.option("--timeout", default=DEFAULT_LLM_TIMEOUT, show_default=True, help="LLM 请求超时（秒）")
def generate(
    repo,
    mode,
    output,
    model,
    base_url,
    threads,
    backup_dir,
    reuse_backup,
    organization,
    dry_run,
    script,
    save_prompt,
    strict,
    profile,
    format_retries,
    save_stage_outputs,
    asset_pack,
    public_assets,
    timeout,
):
    """把 GitHub 仓库变成可游玩的 WebGAL 视觉小说。

    \b
    示例：
      repo2gal vuejs/core
      repo2gal vuejs/core --mode overview
      repo2gal vuejs/core --mode quickstart
      repo2gal https://github.com/OpenWebGAL/WebGAL --dry-run
      repo2gal vuejs/core --reuse-backup --script my_story.txt
    """
    try:
        owner, name = parse_repo(repo)
    except Repo2GalError as exc:
        _die(str(exc), exc.exit_code)

    options = RunOptions(
        owner=owner,
        repo=name,
        output_dir=Path(output) if output else default_output_dir(name, mode),
        backup_root=Path(backup_dir) if backup_dir else default_backup_root(owner),
        mode=mode,
        reuse_backup=reuse_backup,
        organization=organization,
        top_threads=threads,
        token=resolve_github_token(),
        script=Path(script) if script else None,
        dry_run=dry_run,
        strict=strict,
        save_prompt=Path(save_prompt) if save_prompt else None,
        base_url=resolve_base_url(base_url),
        model=resolve_model(model),
        model_fallbacks=resolve_model_fallbacks(),
        api_key=resolve_api_key(),
        llm_timeout=timeout,
        asset_pack=Path(asset_pack) if asset_pack else None,
        public_assets=public_assets,
        profile=profile,
        format_retries=format_retries,
        save_stage_outputs=Path(save_stage_outputs) if save_stage_outputs else None,
    )

    try:
        artifacts = run_pipeline(options, log=_log, warn=_warn, progress=_progress)
    except Repo2GalError as exc:
        _die(str(exc), exc.exit_code)
    except Exception as exc:  # 兜底：未预期异常保持可调试，退出码 1
        traceback.print_exc()
        _die(f"内部错误：{type(exc).__name__}: {exc}", 1)

    if artifacts.output_dir is None:
        # dry-run 两种形态：只打印 prompt，或只打印校验报告
        if artifacts.report is None:
            click.echo("\n" + "─" * 60)
            click.echo(artifacts.prompt)
            click.echo("─" * 60)
            _log(f"dry-run 结束，prompt 共 {len(artifacts.prompt)} 字")
        else:
            _log(f"dry-run 校验结束：{artifacts.report.summary()}")
        return

    click.echo()
    click.echo(click.style("✓ 完成！", fg="green", bold=True))
    click.echo(f"  本地预览：python3 -m http.server -d {artifacts.output_dir} 8000")
    click.echo("  然后打开 http://localhost:8000")


@click.group(name="assets")
def assets_cli():
    """初始化或校验本地 Repo2Gal Asset Pack v1。"""


@assets_cli.command(name="init")
@click.argument("path", type=click.Path(path_type=Path))
def assets_init(path: Path) -> None:
    """在 PATH 创建不覆盖现有文件的素材包骨架。"""
    try:
        root = init_asset_pack(path)
    except Repo2GalError as exc:
        _die(str(exc), exc.exit_code)
    click.echo(click.style("✓ ", fg="green") + f"素材包骨架已创建：{root}")


@assets_cli.command(name="validate")
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--public", "public_assets", is_flag=True, help="按公开发布标准拒绝 LicenseRef")
def assets_validate(path: Path, public_assets: bool) -> None:
    """离线校验 PATH 的 Schema、授权、路径、MIME 与 SHA-256。"""
    try:
        pack = load_asset_pack(path, public=public_assets)
    except Repo2GalError as exc:
        _die(str(exc), exc.exit_code)
    counts = [
        f"{asset_type}×{len(pack.logical_ids(asset_type))}"
        for asset_type in ("background", "character", "bgm")
        if pack.logical_ids(asset_type)
    ]
    mode = "公开发布" if public_assets else "本地使用"
    click.echo(
        click.style("✓ ", fg="green")
        + f"素材包校验通过：{pack.name}@{pack.version}（{mode}，{'，'.join(counts) or '无媒体'}）"
    )


class _CommandDispatcher:
    """只为兼容 ``repo2gal owner/repo`` 而做的薄 argv 分派。"""

    name = "repo2gal"

    def main(self, args=None, prog_name=None, **extra):
        argv = list(args) if args is not None else sys.argv[1:]
        command = assets_cli if argv[:1] == ["assets"] else generate
        command_args = argv[1:] if command is assets_cli else argv
        display_name = prog_name or self.name
        if command is assets_cli:
            display_name += " assets"
        return command.main(args=command_args, prog_name=display_name, **extra)

    def __call__(self, *args, **kwargs):
        return self.main(*args, **kwargs)


main = _CommandDispatcher()


if __name__ == "__main__":
    main()
