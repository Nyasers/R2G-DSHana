"""WebGAL 脚本语法常量。

全部对照 OpenWebGAL/WebGAL 解析器源码核实，勿凭记忆修改。
参见 docs/dev/webgal-script-reference.md。
"""

from __future__ import annotations

# packages/parser/src/config/scriptConfig.ts 的完整命令表。
KNOWN_COMMANDS: frozenset[str] = frozenset(
    {
        "say",
        "changeBg",
        "changeFigure",
        "bgm",
        "playVideo",
        "pixiPerform",
        "pixiInit",
        "intro",
        "miniAvatar",
        "changeScene",
        "choose",
        "end",
        "setComplexAnimation",
        "setFilter",
        "label",
        "jumpLabel",
        "chooseLabel",
        "setVar",
        "if",
        "callScene",
        "showVars",
        "unlockCg",
        "unlockBgm",
        "filmMode",
        "setTextbox",
        "setAnimation",
        "playEffect",
        "setTempAnimation",
        "setTransform",
        "setTransition",
        "getUserInput",
        "applyStyle",
        "wait",
        "callSteam",
    }
)

# 允许 LLM 直接产出的最小子集。任何超出此集合的命令都会被 validator 降级，
# 因为 WebGAL 对未知命令不报错、而是把命令名当成角色名静默错渲染。
# v0.7.0 起 LLM 不再输出 WebGAL 文本，此集合约束 --script 用户脚本。
SAFE_COMMANDS: frozenset[str] = frozenset(
    {
        "say",
        "changeBg",
        "changeFigure",
        "bgm",
        "intro",
        "label",
        "jumpLabel",
        "choose",
        "end",
    }
)

# 确定性编译内核（director.compile_director / performance._compile_action）
# 会产出的额外命令。编译产物由普通代码生成，可以用比 SAFE_COMMANDS 更宽的
# 白名单过 validator（validator 仍是硬边界：结构、跳转、素材引用照查）。
COMPILE_COMMANDS: frozenset[str] = SAFE_COMMANDS | frozenset(
    {
        "pixiInit",
        "pixiPerform",
        "setTransform",
        "setTempAnimation",
    }
)

# 资源命令 -> game/ 下的子目录，用于校验裸文件名。
ASSET_DIRS: dict[str, str] = {
    "changeBg": "background",
    "unlockCg": "background",
    "changeFigure": "figure",
    "miniAvatar": "figure",
    "bgm": "bgm",
    "unlockBgm": "bgm",
    "playEffect": "vocal",
    "playVideo": "video",
    "changeScene": "scene",
    "callScene": "scene",
}

# 素材命令里的保留引用值：解析器认可、但不是素材名的取值。
# ``changeFigure:none`` 是官方「清空立绘」的写法，确定性编译内核用它实现角色退场
# （``performance._compile_action`` 的 ``figure.exit``）。素材白名单必须放行它，
# 否则每次角色退场都会被 validator 当作缺失素材降级：非 strict 时角色不会退场，
# `--strict` 时整个运行以退出码 5 失败。
RESERVED_ASSET_REFERENCES: dict[str, frozenset[str]] = {
    "changeFigure": frozenset({"none"}),
}


def escape_text(text: str) -> str:
    """转义 WebGAL 正文中的保留字符。

    引擎认可的转义为 \\: \\, \\. \\; —— 注意没有 \\- ，
    所以正文里的 " -" 无法转义，只能靠 validator 告警。
    """
    for ch in (";", ":", ",", "."):
        text = text.replace(ch, "\\" + ch)
    return text


def statement_body(line: str) -> str:
    """取出语句主体（丢掉 ';' 之后的行内注释）。

    解析器用 split(/(?<!\\\\);/) 切分，第一段是语句、其余是注释。
    """
    out: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            out.append(line[i : i + 2])
            i += 2
            continue
        if ch == ";":
            break
        out.append(ch)
        i += 1
    return "".join(out)
