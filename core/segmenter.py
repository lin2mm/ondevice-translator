"""分句 / 打包 / 占位符保护。

手机本地 LLM 翻译的两个致命细节：
 1) 长文不能整段喂：prefill 时间 ~ O(n^2)，且 KV cache 撑爆内存 → 必须按"句"切。
 2) `{{var}}`、`%s`、Markdown 链接、HTML 标签被模型顺手翻译掉 = 用户直接卸载 →
    必须先在送进模型前替换成 ASCII 哨兵，出来再还原。

这两个函数在 iOS/Android 各有一份移植实现，行为以本文件为准（tests/ 里有对拍用例）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

# 中英文句末标点。注意：中文句号/问号/叹号/分号都要切，省略号不能切开。
_SENT_SPLIT = re.compile(
    r"""
    (?<=[。！？；?!;])            # 立在句末标点后
    |
    (?<=[。！？?!;][””’'])       # 标点 + 右引号
    |
    (?<=[.!?])\s+(?=[A-Z\u4e00-\u9fff])   # 英文句号后接大写/中文
    """,
    re.VERBOSE,
)

_ABBREV = re.compile(
    r"\b(?:e\.g|i\.e|etc|vs|Dr|Mr|Mrs|Ms|Prof|Inc|Ltd|St|No|Fig|approx|Jr|Sr)\.$|\b[A-Z]\.$"
)

# 需要保护、不许被翻译的东西。顺序有意义：先长后短。
_PROTECT: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("code_f", re.compile(r"```.*?```", re.S)),
    ("code_b", re.compile(r"`[^`\n]+`")),
    ("url", re.compile(r"(?:https?://|www\.)\S+|[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("html", re.compile(r"</?[A-Za-z][^<>]*>")),
    ("fmt", re.compile(r"\{\{[^{}]+\}|\$\{[^}]+\}|\{[a-zA-Z_][\w.]*\}|%[-+ #0]*\d*(?:\.\d+)?[dsfxu]|%%")),
    ("mdlink", re.compile(r"\[[^\]\n]*\]\([^)\n]*\)")),
    # 数字连 ASCII 单位一起保护；中/英单位（美元/元）留给模型自己译
    ("num", re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?:\s*(?:%|kB|MB|GB|TB|kg|km|ms|fps|GHz|mAh))?", re.I)),
    ("path", re.compile(r"(?:[A-Za-z]:\\|/)[\w\-./\\]{3,}")),
)


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef"


def estimate_tokens(text: str) -> int:
    """粗估 token 数（Gemma/Hunyuan 系 SentencePiece/BPE 的经验值）。

    中文约 0.75 token/字，拉丁文约 0.28 token/字符（≈1.4 token/词）。
    目的只是控制 chunk 大小，不需要精确；宁可高估（少切一点 vs 爆内存之间偏保守）。
    """
    cjk = sum(1 for c in text if _is_cjk(c))
    rest = len(text) - cjk
    words = len(re.findall(r"[A-Za-z0-9'.\-]+", text))
    return int(cjk * 0.75 + words * 1.35) + 8   # +8: 模板/prompt 头固定开销


def split_sentences(text: str, max_len: int = 220) -> list[str]:
    """按句切，保留分隔符；单句仍超 max_len 时再按逗号/空格二次切。"""
    text = text.replace("\r\n", "\n").strip("\n")
    if not text:
        return []
    # 先按硬换行分块：换行是有语义的（用户手动分行 / 列表）
    out: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        pieces = [p for p in _SENT_SPLIT.split(para) if p and p.strip()]
        # 合并被切碎的缩写，如 "e.g" / "Mr."
        merged: list[str] = []
        for p in pieces:
            if merged and _ABBREV.search(merged[-1].strip()):
                merged[-1] = merged[-1] + p
            else:
                merged.append(p)
        for sent in merged:
            if len(sent) <= max_len or _is_cjk(sent[0] if sent else " "):
                out.append(sent.strip())
            else:
                out.extend(_soft_split(sent, max_len))
    return [o for o in out if o]


def _soft_split(sent: str, max_len: int) -> list[str]:
    for sep in ("，", ", ", "、", " ", ""):
        if sep == "":
            return [sent[i : i + max_len] for i in range(0, len(sent), max_len)]
        parts = sent.split(sep)
        if all(len(p) <= max_len for p in parts) and len(parts) > 1:
            return [p.strip() + ("" if i == len(parts) - 1 else sep) for i, p in enumerate(parts) if p.strip()]


@dataclass
class Chunk:
    text: str
    index: int
    tokens: int


def pack(sentences: list[str], budget_tokens: int) -> list[Chunk]:
    """把句子打包成不超过 budget_tokens 的 chunk（保持顺序，便于逐块流式上屏）。"""
    chunks: list[Chunk] = []
    cur: list[str] = []
    cur_tok = 0
    for s in sentences:
        t = estimate_tokens(s)
        if cur and cur_tok + t > budget_tokens:
            chunks.append(Chunk(" ".join(cur) if not _is_cjk(cur[0]) else "".join(cur), len(chunks), cur_tok))
            cur, cur_tok = [], 0
        cur.append(s)
        cur_tok += t
    if cur:
        chunks.append(Chunk("".join(cur), len(chunks), cur_tok))
    return chunks


@dataclass
class Protected:
    original: str
    masked: str
    spans: list[tuple[str, str]]        # (哨兵, 原文)


def make_protector(keep_placeholders: bool = True) -> Callable[[str], Protected]:
    """返回 protect(text) -> Protected。哨兵用 ⟨en1⟩ 这种模型极少生成的形式。"""

    def protect(text: str) -> Protected:
        spans: list[tuple[str, str]] = []
        masked = text
        counters: dict[str, int] = {}

        def sub(m: re.Match[str], kind: str = "") -> str:
            body = m.group(0)
            if not keep_placeholders and kind in ("fmt", "html"):
                return body
            counters[kind] = counters.get(kind, 0) + 1
            token = f"⟨{kind}{counters[kind]}⟩"
            spans.append((token, body))
            return token

        for kind, pat in _PROTECT:
            masked = pat.sub(lambda m, k=kind: sub(m, k), masked)
        return Protected(original=text, masked=masked, spans=spans)

    return protect


def restore(masked: str, spans: list[tuple[str, str]]) -> str:
    """还原。若模型漏掉某个哨兵（小模型常见），把缺失的按原位置补回尾部会破坏可读性，
    所以策略是：缺了就标记出来交给 validator 触发"重试/降级为不保护"。"""
    out = masked
    missing: list[str] = []
    for token, body in spans:
        if token in out:
            out = out.replace(token, body)
        else:
            missing.append(body)
    if missing:
        out = out.rstrip() + "".join(missing)     # 兜底：宁可靠后也不能丢
    return out


def has_missing_sentinel(masked: str, spans: list[tuple[str, str]]) -> bool:
    return any(t not in masked for t, _ in spans)
