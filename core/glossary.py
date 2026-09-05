"""术语表 / 热词 / 不译词典（Glossary）。

微软 Translator 的自定义翻译器有"术语列表 + 检测列表"，本产品对齐并做得更强：

  enforce   : 必须这样译（命中即后替换，硬保证）
  hint      : 建议这样译（注入到 prompt 的 Terminology 模板里，让模型自己吞）
  keep      : 不译（品牌名 / 代码标识符）→ 交给 protector 兜底
  forbid    : 禁止出现的译法（出现即判失败重试）

两种生效路径是有原因的：纯后替换会破坏语法（"服务器"→"server" 强行替换后可能变成
"the serv er"），纯 prompt 注入又不保证 100%。所以对 enforce 走「注入 + 后校验」。

文件格式（TSV/CSV，第一行表头，UTF-8）:
    src,dst,mode,note,regex
    向量数据库,vector database,enforce,,false
    中台,middle platform,hint,避免直译 mid-platform,false
    Claude,Claude,keep,,false

对应 iOS/Android: Glossary.swift / Glossary.kt（同一套语义）
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional


class Mode(str, Enum):
    ENFORCE = "enforce"
    HINT = "hint"
    KEEP = "keep"
    FORBID = "forbid"


@dataclass(frozen=True)
class Entry:
    src: str
    dst: str = ""
    mode: Mode = Mode.HINT
    note: str = ""
    regex: bool = False

    @property
    def key_len(self) -> int:
        return len(self.src)


@dataclass
class Glossary:
    name: str = "default"
    version: int = 1
    entries: list[Entry] = field(default_factory=list)

    # ---------- 载入 ----------
    @classmethod
    def from_tsv(cls, text: str, name: str = "default") -> "Glossary":
        body = text.strip()
        if not body:
            return cls(name=name)
        head = next((ln for ln in body.splitlines() if ln.strip() and not ln.startswith("#")), "")
        delim = "\t" if "\t" in head else ("," if "," in head else "\t")
        rows = list(csv.DictReader(io.StringIO(body), delimiter=delim, quotechar='"'))
        out: list[Entry] = []
        for r in rows:
            src = (r.get("src") or "").strip()
            if not src or src.startswith("#"):
                continue
            try:
                mode = Mode((r.get("mode") or "hint").strip().lower())
            except ValueError:
                mode = Mode.HINT
            out.append(
                Entry(
                    src=src,
                    dst=(r.get("dst") or "").strip(),
                    mode=mode,
                    note=(r.get("note") or "").strip(),
                    regex=(r.get("regex") or "false").strip().lower() in ("1", "true", "yes"),
                )
            )
        return cls(name=name, entries=out)

    @classmethod
    def from_json(cls, blob: str | bytes) -> "Glossary":
        raw = json.loads(blob)
        return cls(
            name=raw.get("name", "default"),
            version=int(raw.get("version", 1)),
            entries=[
                Entry(
                    src=e["src"],
                    dst=e.get("dst", ""),
                    mode=Mode(e.get("mode", "hint")),
                    note=e.get("note", ""),
                    regex=bool(e.get("regex", False)),
                )
                for e in raw.get("entries", [])
            ],
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "version": self.version,
                "entries": [
                    {"src": e.src, "dst": e.dst, "mode": e.mode.value, "note": e.note, "regex": e.regex}
                    for e in self.entries
                ],
            },
            ensure_ascii=False,
            indent=2,
        )

    # ---------- 查询 ----------
    def _sorted(self, mode: Mode) -> list[Entry]:
        # 最长匹配优先，避免 "向量数据库" 被 "向量" 抢先命中
        return sorted([e for e in self.entries if e.mode == mode], key=lambda e: -e.key_len)

    def keep_patterns(self) -> list[re.Pattern[str]]:
        pats = [re.escape(e.src) for e in self._sorted(Mode.KEEP) if not e.regex]
        pats += [e.src for e in self._sorted(Mode.KEEP) if e.regex]
        return [re.compile(p, re.I) for p in pats] if pats else []

    def find(self, text: str, modes: Iterable[Mode] = (Mode.HINT, Mode.ENFORCE)) -> list[Entry]:
        """命中条目，全局按最长优先（注入 prompt 时先长后短，避免碎词抢占长词）。"""
        wanted = set(modes)
        hits: list[Entry] = []
        seen: set[str] = set()
        for e in sorted((e for e in self.entries if e.mode in wanted), key=lambda e: -e.key_len):
            if e.src in seen:
                continue
            ok = bool(re.search(e.src, text, re.I)) if e.regex else (e.src in text)
            if ok:
                hits.append(e)
                seen.add(e.src)
        return hits

    # ---------- 输出后处理 ----------
    def apply(self, text: str, src_text: str = "") -> tuple[str, list[str]]:
        """在译文里强制 enforce 译法；返回 (新文本, 违规说明列表)。"""
        out = text
        problems: list[str] = []
        for e in self._sorted(Mode.ENFORCE):
            if e.regex:
                out = re.sub(e.src, e.dst, out)
            elif e.dst and e.dst not in out:
                # 只有当源文确实含该词、且目标译法完全缺席时才补一刀
                if not e.src or e.src in src_text:
                    out = re.sub(re.escape(e.src), e.dst, out)
                    if e.src in out:
                        problems.append(f"未替换掉源词: {e.src}")
        for e in self._sorted(Mode.FORBID):
            if e.src in out or (e.regex and re.search(e.src, out)):
                problems.append(f"出现禁用译法: {e.src}")
        return out, problems

    def stats(self) -> dict[str, int]:
        return {m.value: sum(1 for e in self.entries if e.mode == m) for m in Mode}

    def self_check(self) -> list[str]:
        """术语表自检：编辑界面点"保存"时跑，返回错误列表。"""
        errs: list[str] = []
        by_src: dict[str, Entry] = {}
        for e in self.entries:
            if not e.src.strip():
                errs.append("存在空的源词")
                continue
            if e.mode in (Mode.ENFORCE, Mode.HINT) and not e.dst:
                errs.append(f"{e.src}: {e.mode.value} 需要目标译法")
            if e.regex:
                try:
                    re.compile(e.src)
                except re.error as exc:
                    errs.append(f"{e.src}: 正则非法 ({exc})")
            if e.src in by_src:
                errs.append(f"{e.src}: 重复条目")
            else:
                by_src[e.src] = e
        # 遮蔽检查：短词覆盖长词
        srt = sorted([e for e in self.entries if not e.regex], key=lambda e: -e.key_len)
        for i, longer in enumerate(srt):
            for shorter in srt[i + 1 :]:
                if shorter.src in longer.src:
                    errs.append(f"歧义: “{shorter.src}” 被 “{longer.src}” 包含，命中顺序按最长匹配优先")
                    break
        return errs


def render_terminology_block(hits: list[Entry], lang: str, limit: int = 12) -> str:
    """按 Hy-MT2 官方 Terminology 模板生成"参考译文"块。

    lang="zh" -> 中文前缀；其它 -> 英文前缀。（模板来源：tencent/Hy-MT2-1.8B 模型卡）
    """
    hits = [h for h in hits if h.dst][:limit]
    if not hits:
        return ""
    lines = [f"{h.src} 翻译成 {h.dst}" for h in hits] if lang == "zh" else [f"{h.src} translates to {h.dst}" for h in hits]
    head = "参考下面的翻译：" if lang == "zh" else "Reference the following translations:"
    return head + "\n" + "\n".join(lines) + "\n"
