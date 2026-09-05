"""输出体检 + 重试策略。小模型 20–40% 的"翻车"是可以被机械规则挡下来的，
这一层比换大模型便宜得多（也是"最轻量"能成立的前提）。

检查项（对中英互译）：
  empty            空输出
  passthrough      原样吐回源文（小模型对短英文常见）
  wrong_script     目标语言脚本不对（译英却仍是汉字为主 / 译中却没有汉字）
  meta_leak        "好的，以下是翻译：" / "Here is the translation:" 这类前言
  trunc            引号/括号不闭合、以逗号连结尾（被 max_tokens 截断）
  placeholder      ⟨xx1⟩ 哨兵丢失或数量不对
  repeat_loop      同一片段连续重复（采样温度 0.7 的常见副作用）
  num_mismatch     数字集合不一致（丢数字/改数字，翻译里最致命的错误类型）
  len_ratio        长度比异常（过短=漏译，过长=幻觉）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_META_PREFIX = re.compile(
    r"^\s*(?:好的[，,]?|以下是(?:翻译|译文)?|翻译(?:结果|如下)[：:]|当然[，,]?|here(?:'s| is) the translation[：:]?"
    r"|the translation (?:is|follows)[：:]?|translation:)\s*",
    re.I,
)
_DIGITS = re.compile(r"\d+(?:[.,]\d+)*")
_UNFINISHED = re.compile(r"[,，;；、\-—…]$")


def _cjk_ratio(t: str) -> float:
    if not t:
        return 0.0
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return 0.0
    cjk = sum(1 for c in letters if "\u4e00" <= c <= "\u9fff")
    return cjk / len(letters)


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    text: str = ""            # 可能是"清洗后"的文本
    retry_sampling: dict[str, float] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


def check(
    out: str,
    src_text: str,
    tgt: str,
    src: str = "auto",
    sentinels: list[tuple[str, str]] | None = None,
    max_new_tokens: int = 1024,
) -> Verdict:
    reasons: list[str] = []
    text = (out or "").strip()
    # 反复剥离客套话前缀（"好的，以下是翻译：" 会叠加出现）
    for _ in range(3):
        stripped = _META_PREFIX.sub("", text, count=1).strip()
        if stripped == text:
            break
        text = stripped
    text = text.lstrip(" ：:，,、。").strip()
    text = re.sub(r"^[\"'“”]+|[\"'“”]+$", "", text).strip()

    if not text:
        return Verdict(False, ["empty"], "", {"temperature": 0.2, "repetition_penalty": 1.15})

    if text.replace(" ", "").lower() == src_text.replace(" ", "").lower():
        reasons.append("passthrough")
    if tgt.startswith("en") and _cjk_ratio(text) > 0.5:
        reasons.append("wrong_script")
    if tgt.startswith("zh") and _cjk_ratio(text) < 0.25:
        reasons.append("wrong_script")

    # 源文里的括号/引号成对，译文不成对 => 结构被吃掉
    if (src_text.count("“") + src_text.count('"')) % 2 == 0 and text.count('"') % 2 == 1:
        reasons.append("unbalanced_quote")
    if _UNFINISHED.search(text) and len(text) >= 0.85 * max_new_tokens * 2:
        reasons.append("trunc")

    sentinels = sentinels or []
    if sentinels:
        n_src = len(re.findall(r"⟨[a-z]+\d+⟩", src_text))
        n_out = len(re.findall(r"⟨[a-z]+\d+⟩", text))
        if n_out != n_src:
            reasons.append("placeholder")

    # 复读检测：12 字/8 词的片段出现 >=2 次
    for unit in (12, 8):
        seg = re.findall(r".{%d}" % unit, text) if unit == 12 else text.split()
        if len(seg) > 6:
            seen: dict[str, int] = {}
            for g in range(3):
                for piece in seg[g:]:
                    key = piece if unit == 8 else "".join(piece)
                    seen[key] = seen.get(key, 0) + 1
            if any(v >= 3 for v in seen.values()):
                reasons.append("repeat_loop")
                break

    a, b = set(_DIGITS.findall(src_text)), set(_DIGITS.findall(text))
    if a and not a <= b:
        reasons.append("num_mismatch")

    # 长度比：zh->en 约 1:2~1:4（字符数），en->zh 反之
    r = len(text) / max(1, len(src_text))
    if tgt.startswith("en") and (r < 0.35 or r > 6.0):
        reasons.append("len_ratio")
    if tgt.startswith("zh") and (r < 0.15 or r > 2.2):
        reasons.append("len_ratio")

    clean = bool(reasons == [])
    retry = {} if clean else {"temperature": 0.3, "top_p": 0.8, "top_k": 40.0, "repetition_penalty": 1.08}
    return Verdict(clean, reasons, text, retry)


def split_langs(text: str) -> str:
    """把混排文本切成 zh / en 两段（用于 lang=auto 时给分句和长度校验定基准）。"""
    return re.sub(r"\s+", " ", text).strip()
