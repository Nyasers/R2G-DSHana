"""Director 流程：三轮 LLM（创作 -> 批注 -> 导演 JSON）与确定性 WebGAL 编译。

设计目标（v0.7.0）：

- **注意力分离**。第一轮只写故事（自由格式，每个节拍一行 ``[B]`` 锚点），
  第二轮只做自然语言演出批注，第三轮只把草稿与批注落成 Director Plan JSON。
  不再让 LLM 一边想剧情一边记 WebGAL 语法。
- **确定性编译**。WebGAL 命令、资源路径、label、坐标与时序全部由本模块生成；
  LLM 只输出受限语义 JSON（与旧 Performance Plan 同构，能力表复用
  ``performance.CAPABILITIES``）。
- **有界重试**。Director JSON 校验失败时把结构化错误清单打回第三轮重试，
  次数由 pipeline 控制（默认 2）。重试耗尽后走确定性草稿兜底，绝不产出
  非法脚本。
- **validator 仍是硬边界**。编译产物打包前必须再过 ``validator.sanitize``。

``performance.py`` 自本版本起只保留演出编译内核与能力表；
Beat Manifest / Performance Plan 的旧入口已被本模块取代。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

from .asset_pack import AssetPack
from .config import GAME_MODE_TITLES, NARRATION_FREE_MODES
from .performance import (
    CAPABILITIES,
    PROFILES,
    PerformanceReport,
    _asset_character_map,
    _asset_for_character,
    _compile_action,
    _copy_state,
    _duration,
    _initial_state,
    _merge_incoming_states,
    _parse_json_response,
    _safe_runtime_id,
)

DIRECTOR_SCHEMA_URI = "https://repo2gal.dev/schemas/director/v1.json"

_SCHEMA = json.loads(
    resources.files("repo2gal")
    .joinpath("schemas/director-v1.schema.json")
    .read_text(encoding="utf-8")
)
Draft202012Validator.check_schema(_SCHEMA)

#: 草稿节拍锚点：行首的 ``[B]``，允许与内容同行（模型常把标记和台词写在一行）。
_BEAT_MARKER = re.compile(r"^\s*\[B\]\s*(?P<inline>.*)$", re.IGNORECASE)

#: 草稿兜底解析时的台词行：``角色名:台词``（半角或全角冒号）。
_DIALOGUE_LINE = re.compile(r"^(?P<speaker>.+?)\s*[:：]\s*(?P<text>.+)$")

#: 进入 WebGAL 正文后会破坏解析的字符/模式（不可转义，只能禁止）。
_FORBIDDEN_IN_TEXT = (";", " -")

_MODE_RULES = {
    "chronicle": "narration 只用于没有具体角色说话的客观叙述；凡是角色说出口的话必须写成 dialogue。",
    "overview": "本模式不使用旁白：禁止 narration；带路的向导台词必须全部写成 dialogue，"
    "带文本的 choice beat 也必须写 speaker。",
    "quickstart": "本模式不使用旁白：禁止 narration；带新人上手的维护者台词必须全部写成"
    " dialogue，带文本的 choice beat 也必须写 speaker。",
}


def _beat_id(index: int) -> str:
    return f"b{index:06d}"


def _draft_hash(canonical: str) -> str:
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def split_draft_blocks(raw: str) -> tuple[list[list[str]], int]:
    """按 ``[B]`` 锚点切分草稿，返回 (块列表, 锚点数量)。

    锚点与本行同写的文本算作该块的首行；没有锚点时整篇草稿归入一个块，
    由调用方决定是否按空行兜底分段。
    """
    blocks: list[list[str]] = []
    current: list[str] | None = None
    markers = 0
    for line in raw.splitlines():
        match = _BEAT_MARKER.match(line.strip())
        if match:
            markers += 1
            if current is not None:
                blocks.append(current)
            inline = match.group("inline").strip()
            current = [inline] if inline else []
            continue
        if current is None:
            current = []
        current.append(line)
    if current is not None:
        blocks.append(current)
    return [block for block in blocks if any(ln.strip() for ln in block)], markers


def canonicalize_draft(raw: str) -> tuple[str, list[str]]:
    """把 LLM 草稿规范化为带 ``[b000001]`` 锚点的稳定文本。

    Returns:
        (规范化草稿, 节拍文本列表)。节拍文本列表是第三轮 JSON 的 id 依据
        （一对一映射，不得合并、拆分或重排）。
    """
    blocks, markers = split_draft_blocks(raw)
    if markers == 0:
        paragraphs = [
            para.strip() for para in re.split(r"\n\s*\n", raw.strip()) if para.strip()
        ]
        blocks = [[line] for line in paragraphs]
    beats = ["\n".join(block).strip() for block in blocks if any(ln.strip() for ln in block)]
    if not beats:
        beats = [raw.strip() or "（草稿为空）"]
    canonical = "\n\n".join(
        f"[{_beat_id(i)}]\n{text}" for i, text in enumerate(beats, start=1)
    ) + "\n"
    return canonical, beats


# --- 三轮 prompt 组装 ---


def _figures_line(asset_pack: AssetPack | None) -> str:
    characters = sorted(_asset_character_map(asset_pack))
    return "、".join(characters) if characters else "（无）"


def _cast_line(cast_names: list[str], asset_pack: AssetPack | None) -> str:
    """角色表：逐个标出有没有立绘。

    LLM 常给没有立绘的角色安排 figure.* 动作（validator 会报「角色没有可用立绘」并整份打回），
    把「谁能上台」写进角色表本身，比只给一份立绘名单更不容易被忽略。
    """
    staged = set(_asset_character_map(asset_pack))
    marked = [
        f"{name}（{'有立绘' if name in staged else '无立绘'}）" for name in cast_names
    ]
    return "、".join(marked) if marked else "（无）"


def build_annotation_prompt(
    draft: str,
    *,
    asset_pack: AssetPack | None,
    backgrounds: list[str],
    bgm: list[str],
) -> str:
    """第二轮 prompt：为草稿节拍写自然语言演出批注。"""
    template = resources.files("repo2gal").joinpath("prompts/annotations.md").read_text(encoding="utf-8")
    return (
        template.replace("{backgrounds}", "、".join(backgrounds) or "（无）")
        .replace("{figures}", _figures_line(asset_pack))
        .replace("{bgm}", "、".join(bgm) or "（无）")
        .replace("{draft}", draft)
    )


def build_director_prompt(
    draft: str,
    annotations: str,
    *,
    cast_names: list[str],
    mode: str,
    asset_pack: AssetPack | None,
    backgrounds: list[str],
    bgm: list[str],
    profile: str,
) -> str:
    """第三轮 prompt：把草稿与批注落成 Director Plan JSON。"""
    template = resources.files("repo2gal").joinpath("prompts/director.md").read_text(encoding="utf-8")
    return (
        template.replace("{characters}", _cast_line(cast_names, asset_pack))
        .replace("{backgrounds}", "、".join(backgrounds) or "（无）")
        .replace("{figures}", _figures_line(asset_pack))
        .replace("{bgm}", "、".join(bgm) or "（无）")
        .replace("{capabilities}", json.dumps(CAPABILITIES, ensure_ascii=False, indent=2))
        .replace("{profile}", profile)
        .replace("{maxActionsPerCue}", str(PROFILES[profile]["maxActionsPerCue"]))
        .replace("{modeRule}", _MODE_RULES[mode])
        .replace("{annotations}", annotations or "（无批注）")
        .replace("{draft}", draft)
    )


def render_feedback(findings: list[dict[str, Any]]) -> str:
    """把校验错误清单渲染成第三轮重试时的可读反馈。"""
    errors = [f for f in findings if f.get("kind") == "error"]
    lines: list[str] = []
    for index, finding in enumerate(errors, start=1):
        location = finding.get("beatId") or finding.get("cueId") or ""
        suffix = f"（beat {location}）" if location else ""
        lines.append(f"{index}. {finding.get('message', '未知错误')}{suffix}")
    return "\n".join(lines) or "（无错误详情）"


def retry_suffix(feedback: str) -> str:
    return (
        "\n\n# 上一次输出未通过校验\n\n"
        f"{feedback}\n\n"
        "请逐条修正上述问题后，重新输出完整、合法的 Director Plan JSON。"
    )


# --- Director Plan 解析与校验 ---


def _unwrap_id_reference(value: Any) -> Any:
    """把 ``{"target": "b000022"}`` 拆成 ``"b000022"``。

    模型偶尔按「jump.target」的字面写法把 beat id 包成对象，schema 只接受裸字符串。
    这里只归一形状；目标 id 是否存在仍由 :func:`validate_director` 判定。
    """
    if isinstance(value, dict) and set(value) == {"target"} and isinstance(value["target"], str):
        return value["target"]
    return value


def _normalize_beat_id_references(plan: dict[str, Any], report: PerformanceReport) -> None:
    """归一 ``jump`` 与 ``choices[].target`` 的机械包装（形状问题，不改语义）。"""
    beats = plan.get("beats")
    if not isinstance(beats, list):
        return
    for beat in beats:
        if not isinstance(beat, dict):
            continue
        current = beat.get("jump")
        unwrapped = _unwrap_id_reference(current)
        if unwrapped is not current:
            report.add("fix", "jump 写成了对象，已拆成裸字符串 beat id", beat_id=beat.get("id"))
            beat["jump"] = unwrapped
        choices = beat.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            current = choice.get("target")
            unwrapped = _unwrap_id_reference(current)
            if unwrapped is not current:
                report.add(
                    "fix", "choices[].target 写成了对象，已拆成裸字符串 beat id", beat_id=beat.get("id")
                )
                choice["target"] = unwrapped


def load_director(
    raw: str,
    *,
    report: PerformanceReport,
    scene_id: str = "start",
    story_hash: str | None = None,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """解析第三轮 JSON 输出并绑定系统上下文字段。

    ``sceneId`` / ``storyHash`` / ``profile`` 描述当前确定性运行而非创作决策，
    与旧 ``load_plan`` 一样由 Python 绑定，模型照抄错误也不至于报废可用计划。
    """
    try:
        plan = _parse_json_response(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        report.add("error", f"导演计划不是合法 JSON：{exc}")
        return None
    _normalize_beat_id_references(plan, report)
    for key, expected in (("storyHash", story_hash), ("sceneId", scene_id), ("profile", profile)):
        if expected is None:
            continue
        if plan.get(key) != expected:
            if key in plan:
                report.add("warn", f"LLM 返回的 {key} 已按当前运行上下文修正")
            plan[key] = expected
    validator = Draft202012Validator(_SCHEMA)
    errors = sorted(
        validator.iter_errors(plan), key=lambda error: tuple(str(p) for p in error.absolute_path)
    )
    if errors:
        report.add(
            "error",
            "导演计划 Schema 校验失败：" + "；".join(error.message for error in errors[:8]),
        )
        return None
    report.schema_valid = True
    return plan


def _apply_action_to_state(action: dict[str, Any], state: dict[str, Any], asset_pack: AssetPack | None) -> None:
    """把 cue 动作对角色可见性的影响落到分支状态机（校验用）。"""
    kind = action["kind"]
    character = action.get("character")
    if not character:
        return
    if kind == "figure.enter":
        state["figures"][character] = {
            "visible": True,
            "slot": action["slot"],
            "asset": _asset_for_character(character, asset_pack),
            "runtimeId": _safe_runtime_id(character),
        }
        if character in state["ambiguousFigures"]:
            state["ambiguousFigures"].remove(character)
    elif kind == "figure.exit":
        state["figures"].pop(character, None)
    elif kind == "figure.move" and character in state["figures"]:
        state["figures"][character]["slot"] = action["to"]


def _walk_beats(beats: list[dict[str, Any]], asset_pack: AssetPack | None) -> list[tuple[dict[str, Any], dict[str, Any], tuple[str, ...]]]:
    """沿控制流（choose/jump 分支）推进角色状态机，返回逐 beat 的校验快照。

    与旧 ``extract_beats`` 的控制流语义一致：label 处合并入边状态，
    choose/jump 登记目标入边并终止 fallthrough。
    """
    state = _initial_state()
    branch_path: list[str] = []
    incoming_states: dict[str, list[dict[str, Any]]] = {}
    fallthrough_reachable = True
    rows: list[tuple[dict[str, Any], dict[str, Any], tuple[str, ...]]] = []
    for beat in beats:
        if beat["id"] in incoming_states:
            incoming = list(incoming_states.pop(beat["id"], []))
            if fallthrough_reachable:
                incoming.append(_copy_state(state))
            state = _merge_incoming_states(incoming) if incoming else _initial_state()
            fallthrough_reachable = bool(incoming)
            branch_path = [beat["id"]]
        rows.append((beat, _copy_state(state), tuple(branch_path)))
        cue = beat.get("cue") or {}
        for action in cue.get("actions", []):
            _apply_action_to_state(action, state, asset_pack)
        if beat["kind"] == "choice":
            for option in beat.get("choices", []):
                incoming_states.setdefault(option["target"], []).append(_copy_state(state))
            fallthrough_reachable = False
        jump = beat.get("jump")
        if jump:
            incoming_states.setdefault(jump, []).append(_copy_state(state))
            fallthrough_reachable = False
    return rows


def validate_director(
    plan: dict[str, Any],
    *,
    report: PerformanceReport,
    draft_ids: list[str],
    cast_names: set[str],
    mode: str,
    asset_pack: AssetPack | None,
    asset_catalog: dict[str, frozenset[str]],
    profile: str,
) -> None:
    """对 Director Plan 做语义校验，错误逐条写入 report（错误可回喂第三轮）。

    校验维度：beat 与草稿一对一、角色白名单、文本保留字符、素材引用、
    分支/jump 目标、Overview 旁白禁令、cue 能力表/预算与角色状态机。
    """
    beats: list[dict[str, Any]] = plan.get("beats", [])
    plan_ids = [beat.get("id") for beat in beats if isinstance(beat, dict)]
    if plan_ids != draft_ids:
        report.add(
            "error",
            f"beat 必须与草稿一一对应：期望 {len(draft_ids)} 个（{draft_ids[0]} 到 {draft_ids[-1]}），"
            f"实际 {len(plan_ids)} 个",
        )
        return
    id_set = set(draft_ids)

    title = plan.get("title") or ""
    if any(token in title for token in _FORBIDDEN_IN_TEXT):
        report.add("error", "title 包含禁止字符（; 或 ' -'）")

    character_assets = _asset_character_map(asset_pack)
    cue_count = 0
    action_count = 0
    screen_effects = 0
    for beat, state_before, _branch in _walk_beats(beats, asset_pack):
        beat_id = beat["id"]
        kind = beat["kind"]
        speaker = beat.get("speaker")
        text = beat.get("text") or ""
        stage = beat.get("stage") or {}

        if kind == "dialogue":
            if not isinstance(speaker, str) or speaker not in cast_names:
                report.add("error", f"dialogue 的 speaker 不在角色表中：{speaker!r}", beat_id=beat_id)
            if not text:
                report.add("error", "dialogue 缺少 text", beat_id=beat_id)
        elif kind == "narration":
            if speaker is not None:
                report.add("error", "narration 的 speaker 必须为 null", beat_id=beat_id)
            if not text:
                report.add("error", "narration 缺少 text", beat_id=beat_id)
            if mode in NARRATION_FREE_MODES:
                title = GAME_MODE_TITLES.get(mode, mode)
                report.add(
                    "error",
                    f"{title} 模式不使用旁白，请改为角色的 dialogue",
                    beat_id=beat_id,
                )
        elif kind == "choice":
            choices = beat.get("choices") or []
            if not choices:
                report.add("error", "choice beat 缺少 choices", beat_id=beat_id)
            if text:
                if speaker is not None and speaker not in cast_names:
                    report.add("error", f"choice beat 的 speaker 不在角色表中：{speaker!r}", beat_id=beat_id)
                if speaker is None and mode in NARRATION_FREE_MODES:
                    title = GAME_MODE_TITLES.get(mode, mode)
                    report.add(
                        "error",
                        f"{title} 模式禁止无说话人的 choice 文本",
                        beat_id=beat_id,
                    )
            for option in choices:
                option_text = option.get("text") or ""
                if any(token in option_text for token in (":", "|", ";", " -")):
                    report.add("error", f"选项文本包含保留字符：{option_text!r}", beat_id=beat_id)
                if option.get("target") not in id_set:
                    report.add("error", f"choice 目标不存在：{option.get('target')!r}", beat_id=beat_id)
            if beat.get("jump"):
                report.add("error", "choice beat 不能再设置 jump", beat_id=beat_id)

        for field in ("text",):
            if any(token in text for token in _FORBIDDEN_IN_TEXT):
                report.add("error", f"{kind} 文本包含禁止字符（; 或 ' -'）：{text[:40]!r}", beat_id=beat_id)

        jump = beat.get("jump")
        if jump and jump not in id_set:
            report.add("error", f"jump 目标不存在：{jump!r}", beat_id=beat_id)

        if stage.get("background") is not None and stage["background"] not in asset_catalog["changeBg"]:
            report.add("error", f"背景素材不可用：{stage['background']!r}", beat_id=beat_id)
        if stage.get("bgm") is not None and stage["bgm"] not in asset_catalog["bgm"]:
            report.add("error", f"音乐素材不可用：{stage['bgm']!r}", beat_id=beat_id)

        cue = beat.get("cue") or {}
        actions = cue.get("actions") or []
        if not actions:
            continue
        cue_count += 1
        action_count += len(actions)
        transform_targets: set[str] = set()
        figures_state = _copy_state(state_before)
        for index, action in enumerate(actions):
            kind_ = action["kind"]
            character = action.get("character")
            if character and character in state_before.get("ambiguousFigures", []):
                report.add("error", f"角色在分支汇合处状态不一致，不能安全演出：{character}", beat_id=beat_id)
                continue
            if character and character not in character_assets:
                report.add("error", f"角色没有可用立绘：{character}", beat_id=beat_id)
                continue
            visible = bool(figures_state.get("figures", {}).get(character, {}).get("visible"))
            if character and kind_ in ("figure.move", "figure.shake", "figure.animate") and character in transform_targets:
                report.add("error", f"同一 cue 对角色重复安排冲突演出：{character}", beat_id=beat_id)
            if character and kind_ in ("figure.move", "figure.shake", "figure.animate"):
                transform_targets.add(character)
                if not visible:
                    report.add("error", f"角色尚未入场：{character}", beat_id=beat_id)
            if kind_ == "figure.enter" and character:
                if visible:
                    report.add("error", f"角色重复入场：{character}", beat_id=beat_id)
            elif kind_ == "figure.exit" and character:
                if not visible:
                    report.add("error", f"角色尚未入场或已退场：{character}", beat_id=beat_id)
            if kind_ == "figure.animate" and action["preset"] not in CAPABILITIES["figureAnimations"]:
                report.add("error", f"未注册的立绘动画 preset：{action['preset']}", beat_id=beat_id)
            if kind_ == "screen.transition":
                if action["preset"] not in CAPABILITIES["transitionPresets"]:
                    report.add("error", f"未注册的转场 preset：{action['preset']}", beat_id=beat_id)
                if stage.get("background") is None:
                    report.add("error", "screen.transition 必须与同 beat 的 stage.background 一起出现", beat_id=beat_id)
                expected_phase = "exit" if action["preset"] == "shockwaveOut" else "enter"
                if action.get("phase") != expected_phase:
                    report.add("error", f"转场 preset {action['preset']} 必须使用 phase={expected_phase}", beat_id=beat_id)
            if kind_ == "screen.effect":
                screen_effects += 1
                if action["preset"] not in CAPABILITIES["pixiEffects"]:
                    report.add("error", f"未注册的 Pixi preset：{action['preset']}", beat_id=beat_id)
                if screen_effects > int(PROFILES[profile]["maxScreenEffects"]):
                    report.add("error", "当前 profile 的场景效果数量超限", beat_id=beat_id)
            if kind_ == "figure.shake" and action["intensity"] == "dramatic" and not PROFILES[profile]["allowDramaticShake"]:
                report.add("error", f"{profile} 不允许 dramatic shake", beat_id=beat_id)
            if index >= int(PROFILES[profile]["maxActionsPerCue"]):
                report.add("error", "cue 超过当前 profile 的动作预算", beat_id=beat_id)
            _apply_action_to_state(action, figures_state, asset_pack)

    max_cues = (
        max(1, int(len(beats) * float(PROFILES[profile]["maxCuesPerBeatRatio"]))) if beats else 0
    )
    if cue_count > max_cues:
        report.add("error", f"cue 数量超过 {profile} 预算：{cue_count}>{max_cues}")
    report.cue_count = cue_count
    report.action_count = action_count
    report.semantic_valid = report.errors == 0


# --- 确定性编译 ---


def _narration_line(text: str) -> str:
    return f"say:{text} -clear;"


def _merge_compile_states(states: list[dict[str, Any]]) -> dict[str, Any]:
    """编译侧的分支汇合：只有所有入边一致的角色状态才保留。"""
    if len(states) == 1:
        return dict(states[0])
    merged: dict[str, Any] = {}
    for character in set().union(*(state.keys() for state in states)):
        values = [state.get(character) for state in states]
        if all(value is not None and value == values[0] for value in values):
            merged[character] = copy.deepcopy(values[0])
    return merged


def compile_director(plan: dict[str, Any], *, asset_pack: AssetPack | None) -> str:
    """把校验通过的 Director Plan 编译成完整 WebGAL 脚本。

    label 由目标 beat id 确定性生成（无需 LLM 命名）；旁白统一带 ``-clear``；
    ``screen.transition`` 直接并入同 beat 的 changeBg 参数；末尾补 ``end;``。
    """
    beats: list[dict[str, Any]] = plan.get("beats", [])
    targets = {
        option["target"]
        for beat in beats
        if beat["kind"] == "choice"
        for option in beat.get("choices", [])
    }
    targets |= {beat["jump"] for beat in beats if beat.get("jump")}

    lines: list[str] = []
    title = plan.get("title") or ""
    if title:
        subtitle = plan.get("subtitle") or ""
        lines.append(f"intro:{title}|{subtitle};" if subtitle else f"intro:{title};")

    states: dict[tuple[str, ...], dict[str, Any]] = {}
    branch_path: list[str] = []
    incoming_states: dict[str, list[dict[str, Any]]] = {}
    fallthrough_reachable = True

    for beat in beats:
        beat_id = beat["id"]
        if beat_id in incoming_states:
            incoming = list(incoming_states.pop(beat_id, []))
            if fallthrough_reachable:
                incoming.append(dict(states.get(tuple(branch_path), {})))
            merged = _merge_compile_states(incoming)
            branch_path = [beat_id]
            fallthrough_reachable = bool(incoming)
        else:
            merged = states.get(tuple(branch_path), {})
        state = states.setdefault(tuple(branch_path), {})
        state.clear()
        state.update(merged)

        if beat_id in targets:
            lines.append(f"label:{beat_id};")

        cue = beat.get("cue") or {}
        actions = cue.get("actions") or []
        transition_args = [
            item
            for action in actions
            if action["kind"] == "screen.transition"
            for item in (
                f"-{action['phase']}={action['preset']}",
                f"-{action['phase']}Duration={_duration(action['duration'])}",
            )
        ]
        body_actions = [action for action in actions if action["kind"] != "screen.transition"]

        stage = beat.get("stage") or {}
        if stage.get("background"):
            background_line = f"changeBg:{stage['background']}"
            if transition_args:
                background_line += " " + " ".join(transition_args)
            lines.append(background_line + ";")
        if stage.get("bgm"):
            lines.append(f"bgm:{stage['bgm']};")

        anchor = cue.get("anchor", "during") if body_actions else None
        if anchor == "before":
            for action in body_actions:
                lines.extend(_compile_action(action, anchor, asset_pack, state))

        kind = beat["kind"]
        speaker = beat.get("speaker")
        text = beat.get("text") or ""
        if kind == "dialogue":
            lines.append(f"{speaker}:{text};")
        elif kind == "narration":
            lines.append(_narration_line(text))
        elif kind == "choice":
            if text:
                if speaker:
                    lines.append(f"{speaker}:{text};")
                else:
                    lines.append(_narration_line(text))
            options = "|".join(f"{option['text']}:{option['target']}" for option in beat.get("choices", []))
            lines.append(f"choose:{options};")

        if anchor == "during":
            for action in body_actions:
                lines.extend(_compile_action(action, anchor, asset_pack, state))
        elif anchor == "after":
            for action in body_actions:
                lines.extend(_compile_action(action, anchor, asset_pack, state))

        if beat.get("jump"):
            lines.append(f"jumpLabel:{beat['jump']};")

        # 控制流状态推进（choose/jump 登记入边）
        if kind == "choice":
            for option in beat.get("choices", []):
                incoming_states.setdefault(option["target"], []).append(dict(state))
            fallthrough_reachable = False
        elif beat.get("jump"):
            incoming_states.setdefault(beat["jump"], []).append(dict(state))
            fallthrough_reachable = False

    lines.append("end;")
    return "\n".join(lines) + "\n"


def _fallback_clean(text: str) -> str:
    """兜底文本清理：去掉会破坏 WebGAL 语句切分的字符。"""
    return text.replace(";", "，").replace(" -", "，")


def compile_draft_fallback(beats: list[str], *, cast: list[str], mode: str) -> str:
    """重试耗尽后的确定性兜底：只凭第一轮草稿拼出可玩脚本。

    台词行按 ``角色名:台词`` 识别为 dialogue，其余按旁白处理；
    不使用旁白的模式（Overview / Quick Start）把无说话人文本归给带路角色
    （项目化身，角色表第一位）。
    """
    guide = cast[0] if cast else None
    lines: list[str] = []
    for block in beats:
        for raw_line in block.splitlines():
            line = _fallback_clean(raw_line.strip())
            if not line:
                continue
            match = _DIALOGUE_LINE.match(line)
            if match and match.group("speaker") in cast:
                lines.append(f"{match.group('speaker')}:{match.group('text').strip()};")
                continue
            if mode in NARRATION_FREE_MODES and guide:
                lines.append(f"{guide}:{line};")
            else:
                lines.append(_narration_line(line))
    lines.append("end;")
    return "\n".join(lines) + "\n"
