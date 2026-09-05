"""各家引擎的 prompt 模板（中英互译专用产品，只实现 zh<->en 必需路径）。

Hy-MT2 事实（2026-09 从 tencent/Hy-MT2-1.8B 模型卡核实）：
  * 无默认 system prompt，全部指令写在 user 消息里。
  * 语言必须写"全名"：中文指令用「英语」，英文指令用 "English"。
  * 官方采样：temperature 0.7 / top_p 0.6 / top_k 20 / repetition_penalty 1.05。
    注意：GGUF 权重内嵌的 general.sampling.top_p 实测为 0.8（与模型卡正文不一致）。
    三方（core / iOS LTLlamaBridge / Android llama_jni）统一取本文件的 0.6，以 core 为唯一事实源；
    是否改用 0.8 由真机 A/B 决定（docs/06 风险表 + docs/08 第 3 节第 4 步）。
  * 官方模板含：默认 / 术语(terminology) / 风格(style) / 个性化 / 分隔符保留 / 结构化数据。
    -> 术语模板正是"自学习"回灌模型的通道（docs/02）。

TranslateGemma 的官方模板在 gated 模型卡内，本次未能核实；接它之前必须核对（docs/06）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

LANG_NAMES: dict[str, dict[str, str]] = {
    "zh": {
        "zh": "中文", "en": "英语", "zh-Hant": "繁体中文", "ja": "日语", "ko": "韩语",
        "fr": "法语", "de": "德语", "es": "西班牙语", "ru": "俄语", "ar": "阿拉伯语",
    },
    "en": {
        "zh": "Chinese", "en": "English", "zh-Hant": "Traditional Chinese", "ja": "Japanese",
        "ko": "Korean", "fr": "French", "de": "German", "es": "Spanish", "ru": "Russian",
        "ar": "Arabic",
    },
}

HYMT2_SAMPLING = {"temperature": 0.7, "top_p": 0.6, "top_k": 20.0, "repetition_penalty": 1.05}
HYMT2_SAMPLING_SAFE = {"temperature": 0.3, "top_p": 0.8, "top_k": 40.0, "repetition_penalty": 1.08}


def lang_name(code: str, prompt_lang: str) -> str:
    return LANG_NAMES.get(prompt_lang, LANG_NAMES["en"]).get(code, code)


def prompt_lang_for(tgt: str, src: str) -> str:
    """中文->外语 用中文指令，外语->中文 用英文指令（模型卡要求）。"""
    return "zh" if (tgt.startswith("zh") or src.startswith("zh")) else "en"


@dataclass
class TranslateRequest:
    text: str
    tgt: str
    src: str = "auto"
    terminology_block: str = ""
    examples_block: str = ""          # 自学习 few-shot（docs/02）
    style: Optional[str] = None
    background: Optional[str] = None
    preserve_delimiters: bool = False
    structured: Optional[str] = None
    max_new_tokens: int = 1024


@dataclass
class Prompt:
    user: str
    sampling: dict[str, float] = field(default_factory=dict)
    template_id: str = "default"
    system: Optional[str] = None

    def messages(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if self.system:
            out.append(("system", self.system))
        out.append(("user", self.user))
        return out


class HyMT2PromptBuilder:
    name = "hy-mt2"

    def build(self, req: TranslateRequest) -> Prompt:
        pl = prompt_lang_for(req.tgt, req.src)
        tgt = lang_name(req.tgt, pl)
        if req.structured:
            user, tid = self._structured(req, tgt, pl), "structured"
        elif req.style:
            user, tid = self._plain_style(req, tgt, pl), "style"
        elif req.terminology_block or req.examples_block or req.preserve_delimiters:
            user, tid = self._with_refs(req, tgt, pl), "terminology"
        else:
            user, tid = self._default(req, tgt, pl), "default"
        return Prompt(user=user, sampling=dict(HYMT2_SAMPLING), template_id=tid)

    def _default(self, req: TranslateRequest, tgt: str, pl: str) -> str:
        if pl == "zh":
            return "将以下文本翻译为 " + tgt + "，注意只需要输出翻译后的结果，不要额外解释：\n\n" + req.text
        return (
            "Translate the following text into " + tgt + ". Note that you should only output "
            "the translated result without any additional explanation:\n\n" + req.text
        )

    def _with_refs(self, req: TranslateRequest, tgt: str, pl: str) -> str:
        """术语块 + 自学习例句块 + 分隔符保留，可叠加在默认指令前。"""
        head: list[str] = []
        if req.terminology_block:
            head.append(req.terminology_block.rstrip())
        if req.examples_block:
            head.append(req.examples_block.rstrip())
        body = self._default(req, tgt, pl)
        if req.preserve_delimiters:
            if pl == "zh":
                body = (
                    "请将以下文本准确翻译为 " + tgt + "。\n你必须在译文中保留等量的分隔符，"
                    "绝对不可遗漏、转义或翻译该符号，并注意分隔符的位置。\n\n" + req.text
                )
            else:
                body = (
                    "Please accurately translate the following text into " + tgt + ".\nYou must retain "
                    "the exact same number of delimiters in the translation. Strictly do not omit, "
                    "escape, or translate these symbols, and pay close attention to their placement."
                    "\n\n" + req.text
                )
        return ("\n".join(head) + "\n" + body) if head else body

    def _plain_style(self, req: TranslateRequest, tgt: str, pl: str) -> str:
        if pl == "zh":
            return "请将以下文本翻译为 " + tgt + "。\n注意翻译的风格要严格符合【**" + str(req.style) + "**】\n\n" + req.text
        return (
            "Please translate the following text into " + tgt + ". Note that the translation style "
            "must strictly conform to [**" + str(req.style) + "**]:\n\n" + req.text
        )

    def _structured(self, req: TranslateRequest, tgt: str, pl: str) -> str:
        fmt = (req.structured or "json").upper()
        if pl == "zh":
            return (
                "*# 任务目标*\n将下方文本中的 " + fmt + " 格式数据翻译为 " + tgt + "。\n\n"
                "*# 严格约束*\n"
                "1. **结构锁定**：绝对保持原有的 " + fmt + " 数据结构、缩进和层级完全不变。\n"
                "2. **选择性翻译**：仅翻译面向用户展示的可见文本内容。\n"
                "3. **禁止修改**：**严禁**翻译或更改任何代码标签、键名 (Key)、变量占位符或代码属性。\n\n"
                "*# 数据输入*\n" + req.text
            )
        return (
            "*### Task*\nTranslate the user-facing text within the following " + fmt + " data into " + tgt + ".\n\n"
            "*### Strict Rules*\n"
            "1. **Structure Preservation:** You MUST preserve the original " + fmt + " data structure, "
            "nesting, hierarchy, and indentation exactly as they are.\n"
            "2. **Selective Translation:** Translate ONLY the visible, user-facing text content/values.\n"
            "3. **Strict Non-Translation:** NEVER translate or alter code tags, keys, properties, object "
            "names, or variable placeholders. Leave them exactly in their original form.\n\n"
            "*### Source Data*\n" + req.text
        )


class SmallMTPromptBuilder:
    """Marian/Opus-MT 等编码器-解码器 NMT：没有 prompt，只有源文本。
    保留这个类是为了让编排层对"引擎有无模板"这件事保持同一接口。"""

    name = "marian"

    def build(self, req: TranslateRequest) -> Prompt:
        return Prompt(user=req.text, sampling={}, template_id="raw")


BUILDERS = {"hy-mt2": HyMT2PromptBuilder(), "marian": SmallMTPromptBuilder()}


def builder_for(engine_hint: str):
    """按 engine.id 选模板构建器。marian/onnx 这类无模板引擎走 raw。"""
    hint = (engine_hint or "").lower()
    if "marian" in hint or "onnx" in hint or "opus" in hint:
        return BUILDERS["marian"]
    return BUILDERS["hy-mt2"]
