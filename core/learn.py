"""自学习内核（全本地，零网络）。

"自学习"在手机端不可能是梯度更新（462MB 量化权重不可训练，1.8B 全参微调要几十 GB 显存）。
真正可交付的是四层，成本从低到高，全部在 docs/02-自学习设计.md 里有规格：

  L1 翻译记忆 TM      —— 用户接受/编辑过的 (源句 -> 译文) 存 SQLite，
                          下次相似句作为 few-shot 例句注入 prompt。即时生效、零训练。
  L2 术语挖掘          —— 从"用户改动"里对齐出 src->dst 词对，累计 >=2 次自动进术语表(hint)，
                          用户确认后置为 enforce。这就是"越用越准"的主通道。
  L3 风格画像          —— 统计用户在正式/口语、简/繁、人称上的偏好，落到 style 模板。
  L4 离线 LoRA（可选）  —— 用户在电脑上跑 tools/train_lora.sh，产出 adapter 再导回手机。
                          App 只负责加载 GGUF/LoRA，不参与训练。

L1/L2 是本文件的全部实现；L3 由 L1 的统计派生。相似度用字符 3-gram Jaccard + 长度惩罚：
纯 CPU、无 numpy，手机上 <1ms/条候选，够用且可离线验证。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tm (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  src TEXT NOT NULL, tgt TEXT NOT NULL,
  src_lang TEXT NOT NULL, tgt_lang TEXT NOT NULL,
  edited INTEGER NOT NULL DEFAULT 0,   -- 1 = 用户改过（价值最高）
  accepted_ms INTEGER,                 -- 从出结果到点"复制/发送"的时长，用作隐式满意度
  grams TEXT NOT NULL,                 -- 预存的 3-gram（json list），避免每次重算
  src_raw TEXT NOT NULL DEFAULT '',    -- 原文（norm() 会小写化，few-shot 必须用原文）
  created REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0, score REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tm_pair ON tm (src_lang, tgt_lang, score DESC);
CREATE TABLE IF NOT EXISTS edits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  src TEXT NOT NULL, machine TEXT NOT NULL, human TEXT NOT NULL,
  src_lang TEXT NOT NULL, tgt_lang TEXT NOT NULL,
  added TEXT NOT NULL,                 -- json: 用户新增的译文词/字（相对机翻）
  removed TEXT NOT NULL,               -- json: 用户删掉的机翻词
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS edits_pair ON edits (src_lang, tgt_lang);
CREATE TABLE IF NOT EXISTS terms (
  src TEXT NOT NULL, dst TEXT NOT NULL, lang_pair TEXT NOT NULL,
  hits INTEGER NOT NULL DEFAULT 1, note TEXT DEFAULT '', confirmed INTEGER NOT NULL DEFAULT 0,
  updated REAL NOT NULL, PRIMARY KEY (src, lang_pair)
);
"""


@dataclass(frozen=True)
class TMHit:
    src: str
    tgt: str
    sim: float
    edited: bool


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def grams(text: str, n: int = 3) -> list[str]:
    t = norm(text).replace(" ", "")
    if len(t) <= n:
        return [t] if t else []
    return [t[i : i + n] for i in range(len(t) - n + 1)]


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class Learner:
    """一个文件一个 store。App 端 SQLite 路径：
       iOS  Application Support/Learn/localmt.sqlite
       Android getDatabasePath("localmt.sqlite")（自动备份域之外，见 docs/05）"""

    def __init__(self, db_path: str | Path, *, max_rows: int = 20000, cap_warning: bool = True):
        self.path = str(db_path)
        self.max_rows = max_rows
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as con, con:
            con.executescript(SCHEMA)
        self._cache: dict[tuple[str, str], list[tuple[str, str, set[str], int]]] = {}

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=5.0)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    # ---------- L1 写入 ----------
    def record(
        self,
        src: str,
        tgt: str,
        src_lang: str,
        tgt_lang: str,
        *,
        edited: bool = False,
        accepted_ms: Optional[int] = None,
    ) -> None:
        key_src, key_tgt = norm(src), norm(tgt)
        if not key_src or not key_tgt:
            return
        src, tgt = src.strip(), tgt.strip()
        key = hashlib.sha1(f"{src_lang}|{tgt_lang}|{key_src}".encode()).hexdigest()
        with closing(self._conn()) as con, con:
            cur = con.execute(
                "UPDATE tm SET tgt=?, edited=?, accepted_ms=?, score=?, used=used WHERE id="
                "(SELECT id FROM tm WHERE src_lang=? AND tgt_lang=? AND src=?)",
                (tgt, int(edited), accepted_ms, self._score(edited, accepted_ms), src_lang, tgt_lang, src),
            )
            if cur.rowcount == 0:
                con.execute(
                    "INSERT INTO tm (src, tgt, src_lang, tgt_lang, edited, accepted_ms, grams, created, score)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (src, tgt, src_lang, tgt_lang, int(edited), accepted_ms,
                     json.dumps(grams(src)[:120]), time.time(), self._score(edited, accepted_ms)),
                )
            con.execute("INSERT OR REPLACE INTO meta (k,v) VALUES ('last_key',?)", (key,))
            self._enforce_cap(con)
        self._cache.clear()

    @staticmethod
    def _score(edited: bool, accepted_ms: Optional[int]) -> float:
        """用户改过 = 显式监督信号，权重最高；快速采纳算弱正例。"""
        s = 1.0 if edited else 0.4
        if accepted_ms is not None:
            s += 0.3 if accepted_ms > 4000 else 0.0      # 犹豫过 = 他确实读了
        return round(min(s, 2.0), 3)

    def _enforce_cap(self, con: sqlite3.Connection) -> None:
        n = con.execute("SELECT COUNT(*) FROM tm").fetchone()[0]
        if n > self.max_rows:
            con.execute(
                "DELETE FROM tm WHERE id IN (SELECT id FROM tm ORDER BY score ASC, used ASC, created ASC LIMIT ?)",
                (n - self.max_rows,),
            )

    # ---------- L1 召回 ----------
    def recall(self, src: str, src_lang: str, tgt_lang: str, *, k: int = 3, min_sim: float = 0.34) -> list[TMHit]:
        rows = self._load(src_lang, tgt_lang)
        g = set(grams(src))
        qlen = max(1, len(norm(src)))
        scored: list[tuple[float, str, str, int]] = []
        for s, t, sg, edited in rows:
            sim = jaccard(g, sg)
            if sim <= 0:
                continue
            # 长度惩罚：只共享一个短语的长句不该被当例句
            lr = min(qlen, len(s)) / max(qlen, len(s))
            sim *= 0.6 + 0.4 * lr
            if sim >= min_sim:
                scored.append((sim, s, t, edited))
        scored.sort(key=lambda x: (-x[0], -x[3]))
        return [TMHit(s, t, round(sim, 4), bool(edited)) for sim, s, t, edited in scored[:k]]

    def _load(self, src_lang: str, tgt_lang: str) -> list[tuple[str, str, set[str], int]]:
        """缓存条目里的 src 用原文（若有），相似度仍按归一化后的 grams 算。"""
        key = (src_lang, tgt_lang)
        if key not in self._cache:
            with closing(self._conn()) as con:
                raw = con.execute(
                    "SELECT src, src_raw, tgt, grams, edited FROM tm WHERE src_lang=? AND tgt_lang=? "
                    "ORDER BY score DESC LIMIT 4000",
                    (src_lang, tgt_lang),
                ).fetchall()
            self._cache[key] = [(r1 or r0, r2, set(json.loads(r3)), r4) for r0, r1, r2, r3, r4 in raw]
        return self._cache[key]

    def render_examples_block(self, hits: Iterable[TMHit], prompt_lang: str = "zh", max_chars: int = 700) -> str:
        """把 TM 命中渲染成 Hy-MT2 的"参考翻译"块（官方术语模板同构，模型能理解）。"""
        lines, used = [], 0
        zh = prompt_lang == "zh"
        for h in hits:
            a = f"{h.src} 翻译成 {h.tgt}" if zh else f"{h.src} translates to {h.tgt}"
            if used + len(a) > max_chars:
                break
            lines.append(a)
            used += len(a)
        if not lines:
            return ""
        head = "参考下面的翻译：" if zh else "Reference the following translations:"
        return head + "\n" + "\n".join(lines) + "\n"

    # ---------- L2 术语挖掘（跨句统计共现，不做逐句对齐） ----------
    def record_edit(self, src: str, machine: str, human: str, src_lang: str, tgt_lang: str) -> list[str]:
        """UI 里用户"改完并采纳"时调用。返回用户新增的译文片段。

        只记 (源句, 机翻, 人改) 三元组，配对关系交给后续统计挖掘 —— 单句对齐不可靠。
        """
        added, removed = _diff_fragments(machine, human)
        if src.strip() and (added or removed):
            with closing(self._conn()) as con, con:
                con.execute(
                    "INSERT INTO edits (src, machine, human, src_lang, tgt_lang, added, removed, created)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (src.strip(), machine.strip(), human.strip(), src_lang, tgt_lang,
                     json.dumps(added, ensure_ascii=False), json.dumps(removed, ensure_ascii=False), time.time()),
                )
            self._edit_cache = None
        return added

    def mine_candidates(self, src_lang: str, tgt_lang: str, *, min_support: int = 2, limit: int = 20,
                        max_src_ngram: int = 6) -> list[tuple[str, str, int]]:
        """挖掘候选术语：某译文词 w 被用户反复加进译文，且这些源句反复共享同一段 n-gram g
        -> (g, w) 极可能是稳定对应。返回 [(源词, 译法, 支持度)]。

        排除"到处都出现"的 g（停用词/常见动词），否则会把"的/是/the"学成术语。
        """
        with closing(self._conn()) as con:
            rows = con.execute(
                "SELECT src, added FROM edits WHERE src_lang=? AND tgt_lang=?", (src_lang, tgt_lang)
            ).fetchall()
        if not rows:
            return []
        total = len(rows)
        # w -> {支持它的源句索引}
        w_support: dict[str, set[int]] = {}
        for i, (src, added_json) in enumerate(rows):
            for w in json.loads(added_json):
                w_support.setdefault(w, set()).add(i)
        scored: list[tuple[float, str, str, int]] = []
        for w, idxs in w_support.items():
            if len(idxs) < min_support:
                continue
            ngram_freq: dict[str, int] = {}
            hit: dict[str, int] = {}
            for i, (src, _) in enumerate(rows):
                for g in _src_ngrams(src, max_src_ngram):
                    ngram_freq[g] = ngram_freq.get(g, 0) + 1
                    if i in idxs:
                        hit[g] = hit.get(g, 0) + 1
            best = None
            outside = max(1, total - len(idxs))
            for g, c in hit.items():
                if c < min_support:
                    continue
                # 特异性：在"没做这个改动"的句子里出现得越少越像术语
                bg = (ngram_freq[g] - c) / outside
                if bg > 0.5:
                    continue
                cover = c / len(idxs)
                spec = 1.0 - bg
                score = cover * spec * (1 + 0.12 * len(g))
                if best is None or score > best[0]:
                    best = (score, g, w, c)
            if best:
                scored.append(best)
        scored.sort(key=lambda x: -x[0])
        out = [(g, w, c) for _, g, w, c in scored[:limit]]
        # 长词优先，去掉被更长词包含的短候选
        keep: list[tuple[str, str, int]] = []
        for g, w, c in sorted(out, key=lambda t: -len(t[0])):
            if any(g != g2 and g in g2 for g2, _, _ in keep):
                continue
            keep.append((g, w, c))
        return keep

    def bump_terms(self, pairs: list[tuple[str, str]], src_lang: str, tgt_lang: str, *, promote_at: int = 2) -> list[tuple[str, str, int]]:
        """累计命中；到阈值就变成"待确认术语"。返回本轮新达标的条目。"""
        pair_key = f"{src_lang}-{tgt_lang}"
        promoted: list[tuple[str, str, int]] = []
        with closing(self._conn()) as con, con:
            for a, b in pairs:
                row = con.execute(
                    "SELECT hits FROM terms WHERE src=? AND lang_pair=?", (a, pair_key)
                ).fetchone()
                hits = (row[0] + 1) if row else 1
                con.execute(
                    "INSERT INTO terms (src,dst,lang_pair,hits,updated) VALUES (?,?,?,?,?)"
                    " ON CONFLICT(src,lang_pair) DO UPDATE SET dst=excluded.dst, hits=hits+1, updated=excluded.updated",
                    (a, b, pair_key, hits, time.time()),
                )
                if hits == promote_at:
                    promoted.append((a, b, hits))
        return promoted

    def pending_terms(self, src_lang: str, tgt_lang: str) -> list[tuple[str, str]]:
        with closing(self._conn()) as con:
            return [
                (r[0], r[1])
                for r in con.execute(
                    "SELECT src,dst FROM terms WHERE lang_pair=? AND confirmed=0 AND hits>=2 ORDER BY hits DESC LIMIT 50",
                    (f"{src_lang}-{tgt_lang}",),
                ).fetchall()
            ]

    def confirm_term(self, src: str, dst: str, src_lang: str, tgt_lang: str, confirm: bool = True) -> None:
        with closing(self._conn()) as con, con:
            con.execute(
                "UPDATE terms SET confirmed=?, dst=? WHERE src=? AND lang_pair=?",
                (int(confirm), dst, src, f"{src_lang}-{tgt_lang}"),
            )

    def export_glossary_tsv(self, src_lang: str, tgt_lang: str) -> str:
        """把已确认术语导出成 Glossary 的 CSV 格式（用户可见、可编辑、可分享）。"""
        with closing(self._conn()) as con:
            rows = con.execute(
                "SELECT src,dst,note FROM terms WHERE lang_pair=? AND confirmed=1 ORDER BY hits DESC",
                (f"{src_lang}-{tgt_lang}",),
            ).fetchall()
        lines = ["src,dst,mode,note,regex"]
        lines += [f"{a},{b},enforce,{c or '自动学习'},false" for a, b, c in rows]
        return "\n".join(lines) + "\n"

    # ---------- L3 画像 / 统计 ----------
    def stats(self) -> dict[str, object]:
        with closing(self._conn()) as con:
            tm = con.execute("SELECT COUNT(*), SUM(edited) FROM tm").fetchone()
            terms = con.execute("SELECT COUNT(*), SUM(confirmed) FROM terms").fetchone()
            langs = con.execute(
                "SELECT src_lang||'->'||tgt_lang, COUNT(*) FROM tm GROUP BY 1 ORDER BY 2 DESC LIMIT 4"
            ).fetchall()
        return {
            "tm_rows": tm[0] or 0,
            "tm_edited": tm[1] or 0,
            "terms_total": terms[0] or 0,
            "terms_confirmed": terms[1] or 0,
            "by_pair": dict(langs),
        }

    def touch_used(self, srcs: list[str], src_lang: str, tgt_lang: str) -> None:
        if not srcs:
            return
        with closing(self._conn()) as con, con:
            con.executemany(
                "UPDATE tm SET used=used+1 WHERE src=? AND src_lang=? AND tgt_lang=?",
                [(s, src_lang, tgt_lang) for s in srcs],
            )

    def purge(self, older_than_days: int = 365, keep_edited: bool = True) -> int:
        """隐私出口：按年龄清理；用户改过的默认保留（那是最有价值的样本）。"""
        cut = time.time() - older_than_days * 86400
        q = "DELETE FROM tm WHERE created<?" + (" AND edited=0" if keep_edited else "")
        with closing(self._conn()) as con, con:
            return con.execute(q, (cut,)).rowcount

    def wipe(self) -> None:
        with closing(self._conn()) as con, con:
            con.executescript("DELETE FROM tm; DELETE FROM terms; DELETE FROM meta;")
        self._cache.clear()


def _tokens(text: str) -> list[str]:
    """切"值得当术语"的单位：拉丁词 + CJK 连续段（2 字起）。"""
    out = re.findall(r"[A-Za-z][A-Za-z0-9_\-.']{3,}|[\u4e00-\u9fff]{2,}", text or "")
    return out


_TOK = re.compile(r"[A-Za-z0-9_.\-']+|[\u4e00-\u9fff]|[^\sA-Za-z0-9_.\u4e00-\u9fff]+")


def _wordize(text: str) -> list[tuple[str, str]]:
    """切成 (kind, token)：拉丁词 'w'、单个汉字 'c'、标点 'p'。
    逐字符 diff 会把 "callback"->"webhook" 学成 "we"/"hoo"，必须先词级对齐。"""
    out = []
    for m in _TOK.finditer(text or ""):
        t = m.group(0)
        kind = "w" if t[0].isascii() and t[0].isalnum() else ("c" if "\u4e00" <= t[0] <= "\u9fff" else "p")
        out.append((kind, t))
    return out


def _render(pieces: list[tuple[str, str]]) -> str:
    buf = ""
    for kind, tok in pieces:
        if not buf:
            buf = tok
        elif kind == "w" and buf[-1].isascii() and buf[-1].isalnum():
            buf += " " + tok
        elif kind == "p":
            buf += tok
        else:
            buf += (" " if not buf.endswith(" ") else "") + tok if kind == "w" else buf + tok
    return re.sub(r"\s+", " ", buf).strip(" ,，。.\n;；:：")


def _diff_fragments(machine: str, human: str) -> tuple[list[str], list[str]]:
    """机翻 vs 人改，词级对齐 -> (用户新增片段, 用户删掉片段)。"""
    a, b = _wordize(machine), _wordize(human)
    sm = difflib.SequenceMatcher(a=[t for _, t in a], b=[t for _, t in b], autojunk=False)
    added: list[str] = []
    removed: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for bucket, span in ((removed, a[i1:i2]), (added, b[j1:j2])):
            content = [p for p in span if p[0] != "p"]
            if not content:
                continue
            seg = _render(span)
            if 1 <= len(seg) <= 40 and seg not in bucket:
                bucket.append(seg)
    return added, removed


def _src_ngrams(src: str, max_len: int) -> list[str]:
    """源句候选 n-gram：拉丁词（1~2 词）+ CJK 2..max_len 字滑窗。"""
    out: list[str] = []
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_\-.']+", src or ""):
        out.append(m.group(0))
    run = re.findall(r"[\u4e00-\u9fff]+", src or "")
    for chunk in run:
        for n in range(2, max_len + 1):
            out.extend(chunk[i : i + n] for i in range(0, max(0, len(chunk) - n + 1)))
    seen, uniq = set(), []
    for g in out:
        g = g.strip()
        if g and g not in seen and len(g) <= 24:
            seen.add(g)
            uniq.append(g)
    return uniq
