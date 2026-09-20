"""Chronicle / Overview / Quick Start 流程编排：唯一持有“先做什么、后做什么”的地方。

设计原则：
- 直线生成 + 有界重试：抓取 -> 选角 -> 草稿 prompt -> 三轮 LLM
  （创作 -> 批注 -> 导演 JSON）-> 确定性编译 -> 校验 -> 打包。
  只有导演 JSON 校验失败时才回打第三轮，重试次数有界（默认 2），
  重试耗尽走确定性草稿兜底；不存在无界循环或 agent 工具调用。
- 阶段产物显式化：``RunOptions`` 进、``RunArtifacts`` 出；
- 依赖可注入（fetch/llm/package），整个流程可离线端到端测试；
- 所有失败抛统一错误类型，由 CLI 映射退出码；
- validator 是硬边界：任何剧本（编译产物或 --script）打包前必须过 sanitize。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .asset_pack import AssetPack, load_asset_pack
from .config import (
    DEFAULT_BACKGROUNDS,
    DEFAULT_BASE_URL,
    DEFAULT_BGM,
    DEFAULT_FORMAT_RETRIES,
    DEFAULT_GAME_MODE,
    DEFAULT_LLM_TIMEOUT,
    DEFAULT_MODEL,
    GAME_MODE_TITLES,
    GAME_MODES,
)
from .director import (
    _beat_id,
    _draft_hash,
    build_annotation_prompt,
    build_director_prompt,
    canonicalize_draft,
    compile_director,
    compile_draft_fallback,
    load_director,
    render_feedback,
    retry_suffix,
    validate_director,
)
from .errors import UsageError, ValidationFailed
from .fetcher import RepoContext, fetch_context
from .generator import Cast, build_cast, build_prompt
from .llm import LLMClient
from .packager import package
from .performance import DEFAULT_PROFILE, PROFILES, PerformanceReport, save_json
from .validator import Report, sanitize
from .webgal import COMPILE_COMMANDS, SAFE_COMMANDS


@dataclass
class RunOptions:
    """一次运行的完整输入。由 CLI 组装，pipeline 只读取。"""

    owner: str
    repo: str
    output_dir: Path
    backup_root: Path
    mode: str = DEFAULT_GAME_MODE
    reuse_backup: bool = False
    organization: bool = False
    top_threads: int = 12
    token: str | None = None
    script: Path | None = None
    dry_run: bool = False
    strict: bool = False
    save_prompt: Path | None = None
    base_url: str = DEFAULT_BASE_URL
    models: tuple[str, ...] = (DEFAULT_MODEL,)
    api_key: str | None = None
    llm_timeout: int = DEFAULT_LLM_TIMEOUT
    asset_pack: Path | str | None = None
    public_assets: bool = False
    profile: str = DEFAULT_PROFILE
    format_retries: int = DEFAULT_FORMAT_RETRIES
    save_stage_outputs: Path | None = None


@dataclass
class RunArtifacts:
    """一次运行的全部阶段产物。

    dry-run 不带脚本时 ``raw/clean/report`` 为空（尚未生成剧本）；
    其余路径三者齐备。``output_dir`` 只在完整打包后非空。
    LLM 模式下 ``draft`` 是第一轮草稿（已规范化），``annotations`` 是第二轮批注，
    ``director_plan`` 是校验通过的第三轮 JSON，``director_report`` 是其校验报告。
    """

    ctx: RepoContext
    cast: Cast
    prompt: str
    raw: str
    clean: str
    report: Report | None
    output_dir: Path | None = None
    asset_pack: AssetPack | None = None
    draft: str = ""
    annotations: str = ""
    director_plan: dict | None = None
    director_report: PerformanceReport | None = None


def _read_script(path: Path) -> str:
    """严格读取 --script 文件；路径问题属于用法错误。"""
    if path.is_symlink():
        raise UsageError(f"--script 不接受符号链接：{path}")
    if not path.is_file():
        raise UsageError(f"脚本文件不存在：{path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"无法按 UTF-8 读取脚本：{path}（{exc}）") from exc


def _save_prompt(path: Path, prompt: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prompt, encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"无法写入 prompt：{path}（{exc}）") from exc


def _save_text(path: Path, value: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"无法写入阶段产物：{path}（{exc}）") from exc


def _save_json(path: Path, value: object) -> None:
    try:
        save_json(path, value)
    except OSError as exc:
        raise UsageError(f"无法写入审计 JSON：{path}（{exc}）") from exc


def _default_fetch(options: RunOptions, log: Callable, progress: Callable) -> RepoContext:
    return fetch_context(
        options.owner,
        options.repo,
        backup_root=options.backup_root,
        token=options.token,
        organization=options.organization,
        top_threads=options.top_threads,
        reuse_backup=options.reuse_backup,
        mode=options.mode,
        log=log,
        progress=progress,
    )


def _director_rounds(
    client: LLMClient,
    base_prompt: str,
    *,
    draft: str,
    draft_beats: list[str],
    cast: Cast,
    mode: str,
    asset_pack: AssetPack | None,
    asset_catalog: dict[str, frozenset[str]],
    profile: str,
    retries: int,
    stage_dir: Path | None,
    log: Callable,
    warn: Callable,
) -> tuple[str | None, dict | None, PerformanceReport]:
    """第三轮：导演 JSON 生成 + 有界重试（默认 2 次）。

    每次尝试的校验错误被渲染成反馈拼进下一次 prompt；全部失败时返回
    ``(None, None, degraded report)``，由调用方走确定性草稿兜底。
    """
    report = PerformanceReport(story_hash=_draft_hash(draft))
    draft_ids = [_beat_id(index) for index in range(1, len(draft_beats) + 1)]
    prompt = base_prompt
    last_report = report
    for attempt in range(1, retries + 2):
        raw_json = client.complete(prompt, temperature=0.2)
        if stage_dir is not None:
            _save_text(stage_dir / f"03-director-attempt-{attempt}.json", raw_json)
        report = PerformanceReport(story_hash=_draft_hash(draft))
        plan = load_director(
            raw_json,
            report=report,
            story_hash=_draft_hash(draft),
            profile=profile,
        )
        if plan is not None:
            validate_director(
                plan,
                report=report,
                draft_ids=draft_ids,
                cast_names=cast.names,
                mode=mode,
                asset_pack=asset_pack,
                asset_catalog=asset_catalog,
                profile=profile,
            )
        if plan is not None and report.errors == 0:
            log(f"导演 JSON 第 {attempt} 次尝试通过")
            return raw_json, plan, report
        warn(f"导演 JSON 第 {attempt} 次尝试未通过（{report.errors} 个错误）")
        feedback = render_feedback(report.findings)
        if stage_dir is not None:
            _save_text(stage_dir / f"03-director-feedback-{attempt}.md", feedback)
        prompt = base_prompt + retry_suffix(feedback)
        last_report = report
    last_report.degraded = True
    return None, None, last_report


def run_pipeline(
    options: RunOptions,
    *,
    llm_client: LLMClient | None = None,
    fetch_fn: Callable[[RunOptions, Callable, Callable], RepoContext] | None = None,
    package_fn=None,
    log=lambda _m: None,
    warn=lambda _m: None,
    progress=lambda _m: None,
) -> RunArtifacts:
    """按剧本模式（--mode）与 dry-run/script 执行矩阵运行全部阶段，返回阶段产物。"""

    if options.mode not in GAME_MODES:
        raise UsageError(f"未知剧本模式：{options.mode}")
    log(f"剧本模式：{GAME_MODE_TITLES[options.mode]}（{options.mode}）")
    if options.profile not in PROFILES:
        raise UsageError(f"未知演出 profile：{options.profile}")
    if options.format_retries < 0:
        raise UsageError("--format-retries 不能为负数")

    # --- 阶段 0：本地素材包先校验，避免无效输入触发慢速采集或付费 LLM ---
    if options.public_assets and options.asset_pack is None:
        raise UsageError("--public-assets 必须与 --asset-pack 一起使用")
    asset_pack = (
        load_asset_pack(options.asset_pack, public=options.public_assets)
        if options.asset_pack is not None
        else None
    )
    if asset_pack is not None:
        log(f"素材包校验通过：{asset_pack.name}@{asset_pack.version}")

    # --- 阶段 1：抓取（在线采集或复用离线备份） ---
    ctx = (fetch_fn or _default_fetch)(options, log, progress)

    # --- 阶段 2：确定性选角 ---
    cast = build_cast(ctx, mode=options.mode)
    log(f"角色表：{'、'.join(sorted(cast.names))}")

    # --- 素材目录（prompt 展示、validator 与导演校验共用） ---
    backgrounds = DEFAULT_BACKGROUNDS + (asset_pack.logical_ids("background") if asset_pack else [])
    bgm = DEFAULT_BGM + (asset_pack.logical_ids("bgm") if asset_pack else [])
    if asset_pack is not None:
        asset_catalog = asset_pack.command_catalog()
        asset_catalog["changeBg"] |= frozenset(DEFAULT_BACKGROUNDS)
        asset_catalog["bgm"] |= frozenset(DEFAULT_BGM)
    else:
        asset_catalog = {
            "changeBg": frozenset(DEFAULT_BACKGROUNDS),
            "changeFigure": frozenset(),
            "bgm": frozenset(DEFAULT_BGM),
        }

    # --- 阶段 3：第一轮 prompt（自由创作草稿）组装与保存 ---
    prompt = (
        build_prompt(
            ctx,
            cast,
            mode=options.mode,
            backgrounds=backgrounds,
            figures=asset_pack.logical_ids("character") if asset_pack else None,
            bgm=bgm,
        )
        if asset_pack is not None
        else build_prompt(ctx, cast, mode=options.mode)
    )
    if options.save_prompt:
        _save_prompt(options.save_prompt, prompt)
        log(f"prompt 已保存至 {options.save_prompt}（{len(prompt)} 字）")

    # --- 阶段 4：dry-run 不带脚本：到此为止（CLI 渲染 prompt） ---
    if options.dry_run and options.script is None:
        return RunArtifacts(
            ctx=ctx,
            cast=cast,
            prompt=prompt,
            raw="",
            clean="",
            report=None,
            output_dir=None,
            asset_pack=asset_pack,
        )

    stage_dir = options.save_stage_outputs
    if stage_dir is not None:
        stage_dir.mkdir(parents=True, exist_ok=True)

    # --- 阶段 5：剧本来源（现成脚本，或三轮 LLM + 确定性编译） ---
    draft = ""
    annotations = ""
    director_plan = None
    director_report = None
    if options.script is not None:
        raw = _read_script(options.script)
        log(f"使用现成脚本 {options.script}")
    else:
        client = llm_client or LLMClient(
            base_url=options.base_url,
            models=options.models,
            api_key=options.api_key,
            timeout=options.llm_timeout,
            notify=log,
        )
        log(f"LLM 第 1/3 轮：自由创作{GAME_MODE_TITLES[options.mode]}剧本草稿")
        draft_raw = client.complete(prompt, temperature=0.8)
        draft, draft_beats = canonicalize_draft(draft_raw)
        log(f"草稿规范化完成：{len(draft_beats)} 个节拍")
        if stage_dir is not None:
            _save_text(stage_dir / "01-draft.md", draft)

        log("LLM 第 2/3 轮：自然语言演出批注")
        annotation_prompt = build_annotation_prompt(
            draft,
            asset_pack=asset_pack,
            backgrounds=backgrounds,
            bgm=bgm,
        )
        annotations = client.complete(annotation_prompt, temperature=0.3)
        if stage_dir is not None:
            _save_text(stage_dir / "02-annotations.md", annotations)

        log("LLM 第 3/3 轮：导演 JSON 格式化（校验失败自动重试）")
        director_prompt = build_director_prompt(
            draft,
            annotations,
            cast_names=sorted(cast.names),
            mode=options.mode,
            asset_pack=asset_pack,
            backgrounds=backgrounds,
            bgm=bgm,
            profile=options.profile,
        )
        _last_json, director_plan, director_report = _director_rounds(
            client,
            director_prompt,
            draft=draft,
            draft_beats=draft_beats,
            cast=cast,
            mode=options.mode,
            asset_pack=asset_pack,
            asset_catalog=asset_catalog,
            profile=options.profile,
            retries=options.format_retries,
            stage_dir=stage_dir,
            log=log,
            warn=warn,
        )
        if director_plan is not None:
            log("确定性编译导演计划为 WebGAL 脚本")
            raw = compile_director(director_plan, asset_pack=asset_pack)
        else:
            warn("导演 JSON 重试耗尽，使用确定性草稿兜底编译")
            raw = compile_draft_fallback(
                draft_beats,
                cast=[name for name, _ in cast.entries],
                mode=options.mode,
            )
        if stage_dir is not None and director_report is not None:
            _save_json(stage_dir / "03-director-report.json", director_report.to_dict())
        if director_report is not None:
            log(director_report.summary())
            for finding in director_report.findings:
                if finding.get("kind") in ("error", "warn"):
                    location = ""
                    if finding.get("beatId"):
                        location += f" beat={finding['beatId']}"
                    warn(f"导演计划{location}：{finding.get('message', '未知问题')}")

    # --- 阶段 6：校验（不可绕过的硬边界） ---
    # 编译产物由确定性代码生成，可用 COMPILE_COMMANDS 白名单；
    # --script 用户脚本仍收敛在 SAFE_COMMANDS 内。
    allowed = SAFE_COMMANDS if options.script is not None else COMPILE_COMMANDS
    clean, report = sanitize(raw, speakers=cast.names, allowed=allowed, assets=asset_catalog)
    log(report.summary())
    for finding in report.findings:
        if finding.kind in ("downgrade", "warn"):
            warn(f"第 {finding.line_no} 行：{finding.message}")
    if options.strict and report.downgrades:
        raise ValidationFailed(f"strict 模式：存在 {report.downgrades} 处降级")

    # --- 阶段 7：dry-run 带脚本：只校验不打包（CLI 渲染报告） ---
    if options.dry_run:
        return RunArtifacts(
            ctx=ctx,
            cast=cast,
            prompt=prompt,
            raw=raw,
            clean=clean,
            report=report,
            output_dir=None,
            asset_pack=asset_pack,
            draft=draft,
            annotations=annotations,
            director_plan=director_plan,
            director_report=director_report,
        )

    # --- 阶段 8：打包 ---
    game_key = f"repo2gal_{options.owner}_{options.repo}"
    if options.mode != "chronicle":
        # Chronicle 保持 v0.1.0 以来的存档键不变；新模式单独隔离存档。
        game_key = f"{game_key}_{options.mode}"
    output_dir = (package_fn or package)(
        clean,
        options.output_dir,
        game_name=f"{ctx.full_name} {GAME_MODE_TITLES[options.mode]}",
        game_key=game_key,
        asset_pack=asset_pack,
        log=log,
    )
    return RunArtifacts(
        ctx=ctx,
        cast=cast,
        prompt=prompt,
        raw=raw,
        clean=clean,
        report=report,
        output_dir=output_dir,
        asset_pack=asset_pack,
        draft=draft,
        annotations=annotations,
        director_plan=director_plan,
        director_report=director_report,
    )
