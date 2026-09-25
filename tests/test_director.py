"""Director 流程离线测试：草稿规范化、三轮 prompt、Director Plan 校验与确定性编译。

不依赖网络；LLM 输出用固定 fixture 模拟。
"""

from __future__ import annotations

import json

import pytest

from repo2gal.asset_pack import load_asset_pack
from repo2gal.config import DEFAULT_BACKGROUNDS, DEFAULT_BGM
from repo2gal.director import (
    DIRECTOR_SCHEMA_URI,
    build_annotation_prompt,
    build_director_prompt,
    canonicalize_draft,
    compile_director,
    compile_draft_fallback,
    load_director,
    render_feedback,
    validate_director,
)
from repo2gal.performance import PerformanceReport
from repo2gal.validator import sanitize
from repo2gal.webgal import COMPILE_COMMANDS

EXAMPLE_PACK = "builtin:cc0-chronicle"
CAST = {"widget", "Rust", "guide"}
CATALOG = {
    "changeBg": frozenset({"bg.webp", "background.archive"}),
    "changeFigure": frozenset(),
    "bgm": frozenset({"s_Title.mp3", "bgm.archive"}),
}
STORY_HASH = "sha256:" + "0" * 64


def make_report() -> PerformanceReport:
    return PerformanceReport()


def beat(index: int, **fields):
    value = {"id": f"b{index:06d}", **fields}
    return value


def make_plan(beats, **overrides):
    plan = {
        "$schema": DIRECTOR_SCHEMA_URI,
        "schemaVersion": 1,
        "sceneId": "start",
        "storyHash": STORY_HASH,
        "profile": "chronicle-subtle",
        "title": "Widget",
        "subtitle": "",
        "beats": beats,
    }
    plan.update(overrides)
    return plan


def two_beat_plan():
    return make_plan(
        [
            beat(1, kind="dialogue", speaker="widget", text="你好。"),
            beat(2, kind="narration", speaker=None, text="这是历史。"),
        ]
    )


def load_and_validate(plan, *, draft_count=None, mode="chronicle", asset_pack=None, catalog=None):
    report = make_report()
    draft_ids = [f"b{i:06d}" for i in range(1, (draft_count or len(plan["beats"])) + 1)]
    loaded = load_director(
        json.dumps(plan, ensure_ascii=False),
        report=report,
        story_hash=STORY_HASH,
        profile="chronicle-subtle",
    )
    if loaded is not None:
        validate_director(
            loaded,
            report=report,
            draft_ids=draft_ids,
            cast_names=CAST,
            mode=mode,
            asset_pack=asset_pack,
            asset_catalog=catalog or CATALOG,
            profile="chronicle-subtle",
        )
    return loaded, report


def error_messages(report):
    return [f["message"] for f in report.findings if f["kind"] == "error"]


# --- 草稿规范化 ---


def test_canonicalize_draft_with_markers():
    draft, beats = canonicalize_draft("[B]\nwidget:你好。\n[B]\n这是旁白。\n[B]\nwidget:再见。\n")
    assert beats == ["widget:你好。", "这是旁白。", "widget:再见。"]
    assert "[b000001]" in draft and "[b000003]" in draft


def test_canonicalize_draft_without_markers_splits_paragraphs():
    draft, beats = canonicalize_draft("第一段内容。\n\n第二段内容。\n")
    assert beats == ["第一段内容。", "第二段内容。"]


def test_canonicalize_draft_ignores_empty_marker_blocks():
    draft, beats = canonicalize_draft("[B]\n\n[B]\nwidget:你好。\n")
    assert beats == ["widget:你好。"]


def test_canonicalize_draft_accepts_inline_marker_content():
    draft, beats = canonicalize_draft("[B] widget:你好。\n[B] 这是旁白。\n")
    assert beats == ["widget:你好。", "这是旁白。"]
    assert "[b000002]" in draft


def test_canonicalize_draft_never_returns_no_beats():
    _, beats = canonicalize_draft("   \n")
    assert len(beats) == 1


# --- 三轮 prompt ---


def test_annotation_prompt_advertises_assets_and_capabilities():
    prompt = build_annotation_prompt(
        "[b000001]\nwidget:你好。",
        asset_pack=None,
        backgrounds=["bg.webp"],
        bgm=["s_Title.mp3"],
    )
    assert "bg.webp" in prompt
    assert "s_Title.mp3" in prompt
    assert "move-front-and-back" in prompt
    assert "[b000001]" in prompt


def test_director_prompt_contains_cast_catalog_and_mode_rule():
    prompt = build_director_prompt(
        "[b000001]\nwidget:你好。",
        "[b000001] 无",
        cast_names=sorted(CAST),
        mode="overview",
        asset_pack=None,
        backgrounds=["bg.webp"],
        bgm=["s_Title.mp3"],
        profile="chronicle-subtle",
    )
    assert "widget" in prompt and "guide" in prompt
    assert "bg.webp" in prompt
    assert DIRECTOR_SCHEMA_URI in prompt
    assert "figureMotions" in prompt
    assert "不使用旁白" in prompt


def test_director_prompt_marks_figure_capable_cast():
    """角色表逐个标注有无立绘：没有立绘的角色不得出现在 figure.* 动作里。"""
    prompt = build_director_prompt(
        "[b000001]\nguide:你好。",
        "",
        cast_names=sorted(CAST),
        mode="chronicle",
        asset_pack=load_asset_pack(EXAMPLE_PACK),
        backgrounds=["background.archive"],
        bgm=["s_Title.mp3"],
        profile="chronicle-subtle",
    )
    assert "guide（有立绘）" in prompt
    assert "widget（无立绘）" in prompt
    assert "只有标「有立绘」的角色能出现在 `figure.*` 动作里" in prompt


def test_director_prompt_chronicle_allows_narration():
    prompt = build_director_prompt(
        "[b000001]\nwidget:你好。",
        "",
        cast_names=sorted(CAST),
        mode="chronicle",
        asset_pack=None,
        backgrounds=[],
        bgm=[],
        profile="chronicle-subtle",
    )
    assert "客观叙述" in prompt


# --- load_director ---


def test_load_director_accepts_valid_plan():
    plan, report = load_and_validate(two_beat_plan())
    assert plan is not None
    assert report.schema_valid is True
    assert report.semantic_valid is True
    assert error_messages(report) == []


def test_load_director_strips_markdown_fence():
    report = make_report()
    payload = "```json\n" + json.dumps(two_beat_plan(), ensure_ascii=False) + "\n```"
    plan = load_director(payload, report=report, story_hash=STORY_HASH, profile="chronicle-subtle")
    assert plan is not None
    assert plan["beats"][0]["id"] == "b000001"


def test_load_director_rejects_invalid_json():
    report = make_report()
    plan = load_director("not-json", report=report, story_hash=STORY_HASH)
    assert plan is None
    assert any("合法 JSON" in f["message"] for f in report.findings if f["kind"] == "error")


def test_load_director_rejects_schema_violation():
    report = make_report()
    plan = load_director(
        json.dumps({"beats": "not-a-list"}, ensure_ascii=False),
        report=report,
        story_hash=STORY_HASH,
        profile="chronicle-subtle",
    )
    assert plan is None
    assert any("Schema" in f["message"] for f in report.findings if f["kind"] == "error")


def test_load_director_binds_context_fields_with_warning():
    report = make_report()
    bad = two_beat_plan()
    bad["sceneId"] = "hacked"
    bad["profile"] = "chronicle-cinematic"
    plan = load_director(
        json.dumps(bad, ensure_ascii=False),
        report=report,
        story_hash=STORY_HASH,
        profile="chronicle-subtle",
    )
    assert plan is not None
    assert plan["sceneId"] == "start"
    assert plan["profile"] == "chronicle-subtle"
    assert any(f["kind"] == "warn" for f in report.findings)


# --- validate_director：beat 与草稿的契约 ---


def test_validate_rejects_beat_count_mismatch():
    _, report = load_and_validate(two_beat_plan(), draft_count=3)
    assert any("一一对应" in message for message in error_messages(report))


def test_validate_rejects_unknown_speaker():
    plan = two_beat_plan()
    plan["beats"][0]["speaker"] = "stranger"
    _, report = load_and_validate(plan)
    assert any("不在角色表中" in message for message in error_messages(report))


def test_validate_rejects_narration_in_overview():
    _, report = load_and_validate(two_beat_plan(), mode="overview")
    assert any("不使用旁白" in message for message in error_messages(report))


def test_validate_overview_rejects_speakerless_choice_text():
    plan = make_plan(
        [
            beat(1, kind="choice", speaker=None, text="看什么？",
                 choices=[{"text": "功能", "target": "b000002"}]),
            beat(2, kind="dialogue", speaker="widget", text="功能。"),
        ]
    )
    _, report = load_and_validate(plan, mode="overview")
    assert any("无说话人" in message for message in error_messages(report))


def test_validate_rejects_forbidden_chars_in_text():
    plan = two_beat_plan()
    plan["beats"][0]["text"] = "带分号;的台词"
    _, report = load_and_validate(plan)
    assert any("禁止字符" in message for message in error_messages(report))


def test_validate_rejects_colon_in_choice_text():
    plan = make_plan(
        [
            beat(1, kind="choice", speaker=None, text="选。",
                 choices=[{"text": "a:b", "target": "b000002"}]),
            beat(2, kind="dialogue", speaker="widget", text="好。"),
        ]
    )
    _, report = load_and_validate(plan)
    assert any("保留字符" in message for message in error_messages(report))


def test_validate_rejects_missing_choice_and_jump_targets():
    plan = make_plan(
        [
            beat(1, kind="choice", speaker=None, text="选。",
                 choices=[{"text": "去", "target": "b000999"}]),
            beat(2, kind="dialogue", speaker="widget", text="好。", jump="b000998"),
        ]
    )
    _, report = load_and_validate(plan)
    messages = error_messages(report)
    assert any("choice 目标不存在" in message for message in messages)
    assert any("jump 目标不存在" in message for message in messages)


def test_validate_rejects_jump_on_choice_beat():
    plan = make_plan(
        [
            beat(1, kind="choice", speaker=None, text="选。",
                 choices=[{"text": "去", "target": "b000002"}], jump="b000002"),
            beat(2, kind="dialogue", speaker="widget", text="好。"),
        ]
    )
    _, report = load_and_validate(plan)
    assert any("不能" in message for message in error_messages(report))


def test_validate_rejects_unknown_stage_asset():
    plan = two_beat_plan()
    plan["beats"][0]["stage"] = {"background": "bg.hallucinated"}
    _, report = load_and_validate(plan)
    assert any("背景素材不可用" in message for message in error_messages(report))


# --- validate_director：cue 状态机与预算 ---


def test_validate_rejects_figure_enter_without_asset():
    plan = two_beat_plan()
    plan["beats"][0]["cue"] = {
        "anchor": "during",
        "actions": [{"kind": "figure.enter", "character": "widget", "slot": "center", "motion": "none", "duration": "short"}],
    }
    _, report = load_and_validate(plan, asset_pack=None)
    assert any("没有可用立绘" in message for message in error_messages(report))


def test_validate_accepts_enter_then_animate_with_pack():
    pack = load_asset_pack(EXAMPLE_PACK)
    plan = make_plan(
        [
            beat(1, kind="dialogue", speaker="guide", text="入场。",
                 cue={"anchor": "during", "actions": [
                     {"kind": "figure.enter", "character": "guide", "slot": "center", "motion": "none", "duration": "short"},
                     {"kind": "figure.animate", "character": "guide", "preset": "move-front-and-back", "duration": "medium"},
                 ]}),
            beat(2, kind="dialogue", speaker="guide", text="继续。"),
        ]
    )
    _, report = load_and_validate(plan, asset_pack=pack)
    assert error_messages(report) == []
    assert report.cue_count == 1
    assert report.action_count == 2


def test_validate_rejects_double_enter():
    pack = load_asset_pack(EXAMPLE_PACK)
    enter = {"kind": "figure.enter", "character": "guide", "slot": "center", "motion": "none", "duration": "short"}
    plan = make_plan(
        [
            beat(1, kind="dialogue", speaker="guide", text="一。", cue={"anchor": "during", "actions": [enter]}),
            beat(2, kind="dialogue", speaker="guide", text="二。", cue={"anchor": "during", "actions": [dict(enter)]}),
        ]
    )
    _, report = load_and_validate(plan, asset_pack=pack)
    assert any("重复入场" in message for message in error_messages(report))


def test_validate_rejects_animate_before_enter():
    pack = load_asset_pack(EXAMPLE_PACK)
    plan = make_plan(
        [
            beat(1, kind="dialogue", speaker="guide", text="没入场。",
                 cue={"anchor": "during", "actions": [
                     {"kind": "figure.animate", "character": "guide", "preset": "shockwaveIn", "duration": "short"},
                 ]}),
        ]
    )
    _, report = load_and_validate(plan, asset_pack=pack)
    assert any("尚未入场" in message for message in error_messages(report))


def test_validate_rejects_transition_without_background():
    plan = two_beat_plan()
    plan["beats"][0]["cue"] = {
        "anchor": "before",
        "actions": [{"kind": "screen.transition", "preset": "shockwaveIn", "phase": "enter", "duration": "short"}],
    }
    _, report = load_and_validate(plan)
    assert any("stage.background" in message for message in error_messages(report))


def test_validate_rejects_transition_phase_mismatch():
    plan = two_beat_plan()
    plan["beats"][0]["stage"] = {"background": "bg.webp"}
    plan["beats"][0]["cue"] = {
        "anchor": "before",
        "actions": [{"kind": "screen.transition", "preset": "shockwaveIn", "phase": "exit", "duration": "short"}],
    }
    _, report = load_and_validate(plan)
    assert any("phase=enter" in message for message in error_messages(report))


def test_validate_rejects_effect_budget_overflow():
    effect = {"kind": "screen.effect", "preset": "snow", "intensity": "subtle"}
    plan = make_plan(
        [
            beat(1, kind="dialogue", speaker="widget", text="一。", cue={"anchor": "during", "actions": [dict(effect)]}),
            beat(2, kind="dialogue", speaker="widget", text="二。", cue={"anchor": "during", "actions": [dict(effect)]}),
        ]
    )
    _, report = load_and_validate(plan)
    assert any("场景效果数量超限" in message for message in error_messages(report))


def test_validate_rejects_dramatic_shake_in_subtle_profile():
    pack = load_asset_pack(EXAMPLE_PACK)
    plan = make_plan(
        [
            beat(1, kind="dialogue", speaker="guide", text="震。",
                 cue={"anchor": "during", "actions": [
                     {"kind": "figure.enter", "character": "guide", "slot": "center", "motion": "none", "duration": "short"},
                     {"kind": "figure.shake", "character": "guide", "intensity": "dramatic", "duration": "short"},
                 ]}),
        ]
    )
    _, report = load_and_validate(plan, asset_pack=pack)
    assert any("dramatic" in message for message in error_messages(report))


def test_validate_rejects_unknown_animation_preset():
    pack = load_asset_pack(EXAMPLE_PACK)
    plan = make_plan(
        [
            beat(1, kind="dialogue", speaker="guide", text="跳。",
                 cue={"anchor": "during", "actions": [
                     {"kind": "figure.enter", "character": "guide", "slot": "center", "motion": "none", "duration": "short"},
                     {"kind": "figure.animate", "character": "guide", "preset": "moonwalk", "duration": "short"},
                 ]}),
        ]
    )
    _, report = load_and_validate(plan, asset_pack=pack)
    assert any("未注册的立绘动画 preset" in message for message in error_messages(report))


# --- compile_director ---


def test_compile_emits_intro_labels_choices_and_end():
    plan = make_plan(
        [
            beat(1, kind="narration", speaker=None, text="开场。"),
            beat(2, kind="choice", speaker=None, text="选。",
                 choices=[{"text": "甲", "target": "b000003"}, {"text": "乙", "target": "b000004"}]),
            beat(3, kind="dialogue", speaker="widget", text="甲。", jump="b000004"),
            beat(4, kind="dialogue", speaker="widget", text="汇合。"),
        ]
    )
    script = compile_director(plan, asset_pack=None)
    lines = script.splitlines()
    assert lines[0] == "intro:Widget;"
    assert "say:开场。 -clear;" in lines
    assert "choose:甲:b000003|乙:b000004;" in lines
    assert "label:b000003;" in lines and "label:b000004;" in lines
    assert "jumpLabel:b000004;" in lines
    assert lines[-1] == "end;"
    assert lines.count("label:b000004;") == 1


def test_compile_dialogue_and_narration_forms():
    plan = two_beat_plan()
    script = compile_director(plan, asset_pack=None)
    assert "widget:你好。;" in script
    assert "say:这是历史。 -clear;" in script


def test_compile_stage_and_transition_args():
    plan = two_beat_plan()
    plan["beats"][0]["stage"] = {"background": "bg.webp", "bgm": "s_Title.mp3"}
    plan["beats"][0]["cue"] = {
        "anchor": "before",
        "actions": [{"kind": "screen.transition", "preset": "shockwaveIn", "phase": "enter", "duration": "short"}],
    }
    script = compile_director(plan, asset_pack=None)
    assert "changeBg:bg.webp -enter=shockwaveIn -enterDuration=500;" in script
    assert "bgm:s_Title.mp3;" in script


def test_compile_cue_anchor_suffixes():
    pack = load_asset_pack(EXAMPLE_PACK)
    plan = make_plan(
        [
            beat(1, kind="dialogue", speaker="guide", text="入场。",
                 stage={"background": "background.archive"},
                 cue={"anchor": "during", "actions": [
                     {"kind": "figure.enter", "character": "guide", "slot": "center", "motion": "from-left", "duration": "medium"},
                 ]}),
        ]
    )
    script = compile_director(plan, asset_pack=pack)
    assert "changeFigure:character.guide.normal -repo2galEnter=from-left" in script
    assert "-parallel;" in script  # during -> setTransform 并行
    assert "guide:入场。;" in script


def test_compile_cue_before_uses_next():
    pack = load_asset_pack(EXAMPLE_PACK)
    plan = make_plan(
        [
            beat(1, kind="dialogue", speaker="guide", text="入场。",
                 cue={"anchor": "before", "actions": [
                     {"kind": "figure.enter", "character": "guide", "slot": "left", "motion": "fade", "duration": "short"},
                 ]}),
        ]
    )
    script = compile_director(plan, asset_pack=pack)
    assert "-left" in script
    assert "-next;" in script
    assert script.index("setTransform") < script.index("guide:入场。")


def test_compile_figure_exit_and_move_reuse_runtime_id():
    pack = load_asset_pack(EXAMPLE_PACK)
    plan = make_plan(
        [
            beat(1, kind="dialogue", speaker="guide", text="进。",
                 cue={"anchor": "during", "actions": [
                     {"kind": "figure.enter", "character": "guide", "slot": "center", "motion": "none", "duration": "short"},
                 ]}),
            beat(2, kind="dialogue", speaker="guide", text="走。",
                 cue={"anchor": "during", "actions": [
                     {"kind": "figure.exit", "character": "guide", "motion": "none", "duration": "short"},
                 ]}),
        ]
    )
    script = compile_director(plan, asset_pack=pack)
    assert "changeFigure:none -id=fig-guide" in script
    # 编译产物必须零降级通过 validator：figure.exit 产出的 changeFigure:none
    # 是保留取值，不能被素材白名单当成缺失素材（否则 --strict 直接失败）。
    catalog = pack.command_catalog()
    catalog["changeBg"] |= frozenset(DEFAULT_BACKGROUNDS)
    catalog["bgm"] |= frozenset(DEFAULT_BGM)
    _, report = sanitize(script, speakers={"guide"}, allowed=COMPILE_COMMANDS, assets=catalog)
    assert report.downgrades == 0


def test_compile_screen_effect_inlines_pixi_init():
    plan = two_beat_plan()
    plan["beats"][1]["cue"] = {
        "anchor": "during",
        "actions": [{"kind": "screen.effect", "preset": "snow", "intensity": "subtle"}],
    }
    script = compile_director(plan, asset_pack=None)
    assert "pixiInit;" in script
    assert "pixiPerform:snow;" in script


# --- 兜底编译 ---


def test_fallback_detects_dialogue_and_narration():
    script = compile_draft_fallback(["widget:你好。", "这是旁白。"], cast=["widget", "Rust"], mode="chronicle")
    assert "widget:你好。;" in script
    assert "say:这是旁白。 -clear;" in script
    assert script.endswith("end;\n")


def test_fallback_overview_assigns_guide():
    script = compile_draft_fallback(["这是说明。"], cast=["widget", "Rust"], mode="overview")
    assert "widget:这是说明。;" in script
    assert "say:" not in script


def test_fallback_strips_parser_hostile_chars():
    script = compile_draft_fallback(["widget:带分号;的台词"], cast=["widget"], mode="chronicle")
    assert "带分号，的台词" in script
    assert ";的台词" not in script


# --- 反馈渲染 ---


def test_render_feedback_lists_errors_with_locations():
    findings = [
        {"kind": "error", "message": "speaker 不在角色表中", "beatId": "b000002"},
        {"kind": "warn", "message": "可以忽略"},
        {"kind": "error", "message": "跳转目标不存在"},
    ]
    text = render_feedback(findings)
    assert "1. speaker 不在角色表中（beat b000002）" in text
    assert "2. 跳转目标不存在" in text
    assert "可以忽略" not in text
