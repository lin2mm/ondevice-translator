"""编排层：检测语言 -> 分句 -> 保护占位符 -> 注入(术语/自学习例句) -> 生成 -> 体检/重试 -> 还原。

平台侧（Swift / Kotlin）实现同一个 `Engine` 协议即可复用这套逻辑；本文件同时是
"行为基准"：docs 里承诺的质量/延迟数字都由这条链路产生。

免费栈的三条 Engine 后端（见 docs/01）：
    llama-cpp     Hy-MT2-1.8B-1.25Bit.gguf 462MB —— 主力，iPhone 15 也能跑
    apple         Apple Translation 框架的系统离线包 —— 0 额外体积，兜底
    marian        opus-mt-en-zh / zh-en ONNX ~110MB —— 极限轻量档
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, Sequence

from . import segmenter, validate
from .glossary import Glossary, Mode, render_terminology_block
from .learn import Learner
from .prompt import HYMT2_SAMPLING_SAFE, Prompt, TranslateRequest, builder_for


class Engine(Protocol):
    """手机侧唯一需要实现的接口。generate 必须是阻塞式、可取消的。"""

    id: str
    def ready(self) -> bool: ...
    def generate(self, prompt: Prompt, *, max_new_tokens: int, sampling: dict[str, float],
                 cancel: Callable[[], bool]) -> str: ...
    def unload(self) -> None: ...        # 内存告警时释放（iOS didReceiveMemoryPressure / Android onTrimMemory）


@dataclass
class ChunkResult:
    index: int
    source: str
    target: str
    ms: int
    tokens: int
    attempts: int
    verdicts: list[str] = field(default_factory=list)


@dataclass
class Translation:
    text: str
    src: str
    tgt: str
    chunks: list[ChunkResult]
    engine: str
    cache_hit: bool = False
    degraded: bool = False

    @property
    def total_ms(self) -> int:
        return sum(c.ms for c in self.chunks)

    @property
    def quality_flags(self) -> list[str]:
        return sorted({r for c in self.chunks for r in c.verdicts})


class LRUCache:
    """译文缓存。命中条件包含 glossary/learn 版本号，改了术语表绝不返回旧译文。"""

    def __init__(self, capacity: int = 512):
        self.cap = capacity
        self._d: "OrderedDict[str, str]" = OrderedDict()
        self.hits = 0

    @staticmethod
    def key(text: str, src: str, tgt: str, glossary_v: int, learn_v: int, style: Optional[str]) -> str:
        blob = "\x1f".join([text, src, tgt, str(glossary_v), str(learn_v), style or ""])
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, k: str) -> Optional[str]:
        v = self._d.get(k)
        if v is not None:
            self._d.move_to_end(k)
            self.hits += 1
        return v

    def put(self, k: str, v: str) -> None:
        self._d[k] = v
        self._d.move_to_end(k)
        while len(self._d) > self.cap:
            self._d.popitem(last=False)


class Translator:
    def __init__(
        self,
        engine: Engine,
        *,
        glossary: Optional[Glossary] = None,
        learner: Optional[Learner] = None,
        chunk_tokens: int = 150,
        max_retries: int = 1,
        cache_capacity: int = 512,
        use_memory: bool = True,
    ):
        self.engine = engine
        self.glossary = glossary
        self.learner = learner
        self.chunk_tokens = chunk_tokens
        self.max_retries = max_retries
        self.use_memory = use_memory
        self.cache = LRUCache(cache_capacity)
        self.builder = builder_for(getattr(engine, "id", "hy-mt2"))
        self._glossary_v = 1
        self._learn_v = 1
        self._protect = segmenter.make_protector()

    # ---------- 语言检测（中英互译产品：二分类足够，别引入 fasttext） ----------
    @staticmethod
    def detect(text: str) -> tuple[str, str]:
        cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        latin = sum(1 for c in text if c.isascii() and c.isalpha())
        if cjk == 0 and latin == 0:
            return "zh", "en"
        ratio = cjk / max(1, cjk + latin)
        return ("zh", "en") if ratio >= 0.25 else ("en", "zh")

    def set_glossary(self, g: Glossary) -> None:
        self.glossary = g
        self._glossary_v += 1        # 让缓存整体失效

    def translate(self, text: str, *, src: str = "auto", tgt: Optional[str] = None,
                  style: Optional[str] = None, structured: Optional[str] = None,
                  cancel: Callable[[], bool] = lambda: False) -> Translation:
        d_src, d_tgt = self.detect(text)
        src = d_src if src == "auto" else src
        tgt = tgt or ("en" if src == "zh" else "zh")

        ck = self.cache.key(text, src, tgt, self._glossary_v, self._learn_v, style)
        hit = self.cache.get(ck)
        if hit is not None:
            return Translation(hit, src, tgt, [], self.engine.id, cache_hit=True)

        sents = segmenter.split_sentences(text) or [text.strip()]
        chunks = segmenter.pack(sents, self.chunk_tokens)
        results: list[ChunkResult] = []
        outs: list[str] = []
        degraded = False

        for ch in chunks:
            if cancel():
                break
            prot = self._protect(ch.text)
            masked = prot.masked
            for rule in (self.glossary.keep_patterns() if self.glossary else []):
                masked = rule.sub(lambda m: m.group(0), masked)   # keep 词交给 protector/模型，不额外改
            req = TranslateRequest(text=masked, src=src, tgt=tgt, structured=structured)
            req.terminology_block = self._terminology(ch.text, tgt)
            req.examples_block = self._examples(ch.text, src, tgt)
            prompt = self.builder.build(req)

            t0 = time.perf_counter()
            out, verdict, attempts = "", None, 0
            for attempt in range(self.max_retries + 1):
                attempts = attempt + 1
                sampling = prompt.sampling if attempt == 0 else (verdict.retry_sampling or HYMT2_SAMPLING_SAFE)
                raw = self.engine.generate(
                    prompt, max_new_tokens=req.max_new_tokens, sampling=sampling, cancel=cancel
                )
                verdict = validate.check(
                    raw, ch.text, tgt, src, sentinels=prot.spans, max_new_tokens=req.max_new_tokens
                )
                out = verdict.text or raw.strip()
                if verdict.ok:
                    break
            # 还原哨兵 + 术语强制 + 自学习记账
            out = segmenter.restore(out, prot.spans)
            if self.glossary:
                out, problems = self.glossary.apply(out, src_text=ch.text)
                if problems:
                    verdict.reasons.append("glossary_conflict")
            ms = int((time.perf_counter() - t0) * 1000)
            if attempts > 1:
                degraded = True
            results.append(
                ChunkResult(ch.index, ch.text, out, ms, ch.tokens, attempts, list(verdict.reasons))
            )
            outs.append(out)

        final = self._join(outs, src, tgt)
        self.cache.put(ck, final)
        return Translation(final, src, tgt, results, self.engine.id, cache_hit=False, degraded=degraded)

    # ---------- 注入两块上下文 ----------
    def _terminology(self, text: str, tgt: str) -> str:
        if not self.glossary:
            return ""
        hits = [e for e in self.glossary.find(text, (Mode.HINT, Mode.ENFORCE))]
        return render_terminology_block(hits, "zh" if tgt.startswith("zh") else "en")

    def _examples(self, text: str, src: str, tgt: str) -> str:
        if not (self.use_memory and self.learner):
            return ""
        hits = self.learner.recall(text, src, tgt, k=3)
        if not hits:
            return ""
        self.learner.touch_used([h.src for h in hits], src, tgt)
        from .prompt import prompt_lang_for
        return self.learner.render_examples_block(hits, prompt_lang_for(tgt, src))

    @staticmethod
    def _join(parts: Sequence[str], src: str, tgt: str) -> str:
        if not parts:
            return ""
        # 中文不加空格，英文按句读自然拼接
        if tgt.startswith("zh"):
            return "".join(parts)
        return " ".join(p if p.endswith((".", "!", "?", "\n")) else p + "." for p in parts).replace("\n ", "\n")

    # ---------- 采纳反馈（自学习入口，UI 上的"编辑译文"确认后调用） ----------
    def accept(self, chunk: ChunkResult, *, final_text: str, accepted_ms: int, src: str, tgt: str) -> list[tuple[str, str]]:
        """返回本轮新挖到的"待确认术语"，UI 可以直接弹提示条。"""
        if not self.learner:
            return []
        edited = final_text.strip() != chunk.target.strip()
        self.learner.record(chunk.source, final_text, src, tgt, edited=edited, accepted_ms=accepted_ms)
        self._learn_v += 1
        if not edited:
            return []
        self.learner.record_edit(chunk.source, chunk.target, final_text, src, tgt)
        # 只有统计挖掘给出的稳定对应才进"待确认术语"
        cand = self.learner.mine_candidates(src, tgt, min_support=2, limit=3)
        return self.learner.bump_terms([(g, w) for g, w, _n in cand], src, tgt)
