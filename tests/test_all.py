"""核心逻辑单测：这是 docs 里所有承诺的来源。跑法：python3 -m unittest discover -s tests -t ."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from core import segmenter, validate
from core.engine import Translator
from core.glossary import Glossary, render_terminology_block
from core.learn import Learner
from core.model_manifest import Manifest, ModelAsset, fmt_size, parse_size, scan_dir_to_manifest
from core.prompt import TranslateRequest, builder_for, lang_name, prompt_lang_for


class StubEngine:
    """可编程假引擎：用来验证编排层（重试、缓存、注入），不验证模型质量。"""

    def __init__(self, script):
        self.id = "hy-mt2-stub"
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    def ready(self):
        return True

    def generate(self, prompt, *, max_new_tokens, sampling, cancel):
        self.calls.append((prompt.user, sampling))
        out = self.script.pop(0) if self.script else ""
        return out(prompt) if callable(out) else out

    def unload(self):
        pass


class TestSegmenter(unittest.TestCase):
    def test_zh_sentences(self):
        sents = segmenter.split_sentences("今天天气很好。我们出去走走！你带伞了吗？")
        self.assertEqual(len(sents), 3)
        self.assertTrue(sents[0].endswith("。"))

    def test_en_abbrev_not_split(self):
        sents = segmenter.split_sentences("Use tools e.g. hammer. Then stop.")
        self.assertEqual(len(sents), 2, sents)

    def test_token_estimate_monotonic(self):
        self.assertGreater(segmenter.estimate_tokens("很长的中文句子需要更多token来处理"),
                           segmenter.estimate_tokens("短"))

    def test_pack_respects_budget(self):
        sents = ["句子%d。" % i for i in range(50)]
        chunks = segmenter.pack(sents, 40)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(c.tokens, 48)          # 允许 1 句超预算但不失控

    def test_protect_and_restore_roundtrip(self):
        p = segmenter.make_protector()
        src = '请发送 {{user_name}} 到 support@x.com，折扣 20%，并看 <b>粗体</b>。'
        prot = p(src)
        self.assertNotIn("{{user_name}}", prot.masked)
        self.assertIn("⟨", prot.masked)
        self.assertEqual(segmenter.restore(prot.masked, prot.spans), src)

    def test_restore_recovers_dropped_sentinel(self):
        p = segmenter.make_protector()
        prot = p("价格 50% 元")
        out = segmenter.restore("The price is yuan", prot.spans)
        self.assertIn("50%", out)
        self.assertTrue(segmenter.has_missing_sentinel("nothing", prot.spans))


class TestValidate(unittest.TestCase):
    def test_meta_leak_stripped(self):
        v = validate.check("好的，以下是翻译：\n\nHello world", "你好世界", "en", "zh")
        self.assertTrue(v.ok, v.reasons)
        self.assertEqual(v.text, "Hello world")

    def test_passthrough_rejected(self):
        v = validate.check("Hello world", "Hello world", "en", "en")
        self.assertFalse(v.ok)
        self.assertIn("passthrough", v.reasons)

    def test_wrong_script(self):
        v = validate.check("这是一个中文输出", "hello world this is a test", "en", "en")
        self.assertIn("wrong_script", v.reasons)

    def test_number_loss_detected(self):
        v = validate.check("The total is dollars.", "总计 1,280 美元", "en", "zh")
        self.assertIn("num_mismatch", v.reasons)

    def test_repeat_loop_detected(self):
        seg = "the system " * 12
        v = validate.check(seg, "系统", "en", "zh")
        self.assertIn("repeat_loop", v.reasons)
        self.assertTrue(v.retry_sampling)

    def test_placeholder_count(self):
        p = segmenter.make_protector()
        prot = p("Hi {{name}}")
        v = validate.check("Hello ⟨fmt1⟩", "Hi ⟨fmt1⟩", "en", "zh", sentinels=prot.spans)
        self.assertNotIn("placeholder", v.reasons)
        v2 = validate.check("Hello", "Hi ⟨fmt1⟩", "en", "zh", sentinels=prot.spans)
        self.assertIn("placeholder", v2.reasons)


class TestPrompt(unittest.TestCase):
    def test_lang_names_follow_prompt_lang(self):
        self.assertEqual(lang_name("en", "zh"), "英语")
        self.assertEqual(lang_name("zh", "en"), "Chinese")
        self.assertEqual(prompt_lang_for("en", "zh"), "zh")
        self.assertEqual(prompt_lang_for("zh", "en"), "zh")  # 含中文的方向统一用中文指令
        self.assertEqual(prompt_lang_for("ja", "ko"), "en")

    def test_hymt2_default_template(self):
        b = builder_for("hy-mt2")
        pr = b.build(TranslateRequest(text="你好", tgt="en", src="zh"))
        self.assertIn("将以下文本翻译为 英语", pr.user)
        self.assertIn("只需要输出翻译后的结果", pr.user)
        self.assertEqual(pr.sampling["temperature"], 0.7)
        self.assertIsNone(pr.system)                 # 模型卡：无默认 system prompt

    def test_terminology_block_preferred_over_default(self):
        b = builder_for("hy-mt2")
        pr = b.build(TranslateRequest(text="向量库", tgt="en", src="zh", terminology_block="参考下面的翻译：\n向量库 翻译成 vector db"))
        self.assertIn("参考下面的翻译", pr.user)
        self.assertIn("将以下文本翻译为 英语", pr.user)
        self.assertEqual(pr.template_id, "terminology")

    def test_structured_template_forbids_keys(self):
        pr = builder_for("hy-mt2").build(TranslateRequest(text='{"a":1}', tgt="en", src="zh", structured="json"))
        self.assertIn("结构锁定", pr.user)
        self.assertIn("键名", pr.user)


class TestGlossary(unittest.TestCase):
    CSV = 'src,dst,mode,note,regex\n向量数据库,vector database,enforce,,false\n向量,vector,hint,,false\nClaude,Claude,keep,,false'

    def test_parse_and_longest_match(self):
        g = Glossary.from_tsv(self.CSV)
        hits = g.find("我们用向量数据库存向量")
        self.assertEqual([h.src for h in hits], ["向量数据库", "向量"])
        self.assertEqual(g.stats()["enforce"], 1)

    def test_enforce_substitution(self):
        g = Glossary.from_tsv(self.CSV)
        out, probs = g.apply("we store vectors in a vector databse", "我们用向量数据库存向量")
        self.assertEqual(probs, [])

    def test_conflict_selfcheck(self):
        g = Glossary.from_tsv(self.CSV)
        errs = g.self_check()
        self.assertTrue(any("歧义" in e for e in errs), errs)

    def test_terminology_block_language(self):
        g = Glossary.from_tsv(self.CSV)
        blk = render_terminology_block(g.find("向量数据库"), "zh")
        self.assertIn("向量数据库 翻译成 vector database", blk)
        blk_en = render_terminology_block(g.find("向量数据库"), "en")
        self.assertIn("Reference the following translations", blk_en)


class TestLearner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.learner = Learner(os.path.join(self.tmp, "learn.sqlite"))

    def tearDown(self):
        for f in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, f))

    def test_record_recall_and_render(self):
        self.learner.record("服务器的响应超时了，请重试。", "The server response timed out. Please retry.", "zh", "en", edited=True)
        hits = self.learner.recall("服务器的响应超时了，请稍后重试", "zh", "en")
        self.assertTrue(hits, "相似句应命中 TM")
        self.assertGreater(hits[0].sim, 0.4)
        blk = self.learner.render_examples_block(hits, "zh")
        self.assertIn("参考下面的翻译", blk)
        self.assertIn("翻译成", blk)

    def test_dissimilar_not_recalled(self):
        self.learner.record("今天天气真好", "The weather is nice today.", "zh", "en")
        self.assertEqual(self.learner.recall("请帮我预订一张明天去上海的机票和酒店", "zh", "en"), [])

    def test_mine_candidates_from_repeated_edits(self):
        """自学习核心：用户改 3 次同一术语，第 3 次就该被学到（无需联网/训练）。"""
        cases = [
            ("请为回调地址配置密钥", "Please configure a key for the callback", "Please configure a key for the webhook"),
            ("回调地址在哪里修改", "Where do I change the callback", "Where do I change the webhook"),
            ("打开回调地址页面", "Open the callback page", "Open the webhook page"),
            ("请打开设置页", "Please open settings", "Please open the settings tab"),   # 干扰项
        ]
        for src, m, h in cases:
            added = self.learner.record_edit(src, m, h, "zh", "en")
            self.assertTrue(added)
        cand = self.learner.mine_candidates("zh", "en", min_support=2)
        pairs = {(g, w): n for g, w, n in cand}
        self.assertIn(("回调地址", "webhook"), pairs, cand)
        self.assertEqual(pairs[("回调地址", "webhook")], 3)
        self.assertNotIn(("请打开", "webhook"), pairs)

    def test_single_edit_is_not_enough(self):
        self.learner.record_edit("请为回调地址配置密钥", "for the callback", "for the webhook", "zh", "en")
        self.assertEqual(self.learner.mine_candidates("zh", "en", min_support=2), [])

    def test_mined_term_flows_into_glossary(self):
        for src, m, h in [("回调地址", "the callback", "the webhook")] * 2:
            self.learner.record_edit(src, m, h, "zh", "en")
        cand = self.learner.mine_candidates("zh", "en", min_support=2)
        self.assertTrue(cand)
        self.learner.bump_terms([(g, w) for g, w, _ in cand], "zh", "en", promote_at=1)
        self.learner.confirm_term(cand[0][0], cand[0][1], "zh", "en", True)
        g = Glossary.from_tsv(self.learner.export_glossary_tsv("zh", "en"))
        self.assertEqual(g.stats()["enforce"], 1)
        self.assertEqual(g.entries[0].src, cand[0][0])

    def test_promote_threshold_and_export(self):
        for _ in range(2):
            self.learner.bump_terms([("向量库", "vector store")], "zh", "en", promote_at=2)
        self.assertEqual(self.learner.pending_terms("zh", "en"), [("向量库", "vector store")])
        self.learner.confirm_term("向量库", "vector store", "zh", "en", True)
        tsv = self.learner.export_glossary_tsv("zh", "en")
        self.assertIn("enforce", tsv)
        g = Glossary.from_tsv(tsv)
        self.assertEqual(g.stats()["enforce"], 1)

    def test_cap_and_stats_and_wipe(self):
        learner = Learner(os.path.join(self.tmp, "cap.sqlite"), max_rows=10)
        for i in range(30):
            learner.record("句子 %d 内容" % i, "sentence %d content" % i, "zh", "en")
        st = learner.stats()
        self.assertLessEqual(st["tm_rows"], 10)
        self.assertGreaterEqual(st["tm_rows"], 5)
        learner.wipe()
        self.assertEqual(learner.stats()["tm_rows"], 0)


class TestManifest(unittest.TestCase):
    ASSET = ModelAsset(
        id="hy-mt2-1.8b-1.25bit", url="https://cdn.local/Hy-MT2-1.8B-1.25Bit.gguf",
        sha256="a" * 64, size_bytes=parse_size("462MB"), quant="1.25bit", params_b=1.8,
        min_ram_bytes=parse_size("3GB"), min_free_bytes=parse_size("600MB"),
        languages=("zh", "en"), engine="llama-cpp",
    )

    def test_size_fmt_roundtrip(self):
        self.assertEqual(parse_size("462 MB"), 462 * 1000**2)
        self.assertEqual(fmt_size(parse_size("462MB")), "462 MB")
        self.assertEqual(fmt_size(900), "900 B")

    def test_validate_flags_bad_asset(self):
        bad = Manifest(version="1", updated_at="", assets=[ModelAsset(id="x", url="http://a", sha256="zz",
                                                                     size_bytes=10, languages=())])
        errs = bad.validate()
        self.assertTrue(any("sha256" in e for e in errs))
        self.assertTrue(any("https" in e for e in errs))
        self.assertTrue(any("zh/en" in e for e in errs))

    def test_select_for_device_downgrades(self):
        big = ModelAsset(id="q4", url="https://c/q4.gguf", sha256="b" * 64, size_bytes=parse_size("1133MB"),
                         quant="Q4_K_M", params_b=1.8, min_ram_bytes=parse_size("5GB"),
                         min_free_bytes=parse_size("1.3GB"), languages=("zh", "en"), tier=0)
        m = Manifest(version="2026.09", updated_at="", assets=[self.ASSET, big])
        self.assertEqual(m.validate(), [])
        # iPhone 15: 6GB RAM, 40GB free -> 质量优先应选 Q4
        pick = m.select_for_device(parse_size("6GB"), parse_size("40GB"), prefer="quality")
        self.assertEqual(pick.id, "q4")
        # 4GB 低端机 -> 只能吃 462MB 包
        pick = m.select_for_device(parse_size("4GB"), parse_size("2GB"), prefer="quality")
        self.assertEqual(pick.id, self.ASSET.id)
        # 磁盘不够 -> None（App 要提示清理，而不是崩）
        self.assertIsNone(m.select_for_device(parse_size("6GB"), parse_size("100MB")))

    def test_scan_dir_builds_manifest_with_real_hashes(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "Hy-MT2-1.8B-1.25Bit.gguf")
            with open(p, "wb") as fh:
                fh.write(b"x" * (2 * 1000 * 1000))
            m = scan_dir_to_manifest(d, "https://cdn.local/models/", "2026.09.05", "2026-09-05T00:00:00Z",
                                     extra={"Hy-MT2-1.8B-1.25Bit": {"languages": ["zh", "en"], "min_free_bytes": 3 * 1000**2}})
            self.assertEqual(len(m.assets), 1)
            a = m.assets[0]
            self.assertEqual(a.quant, "1.25bit")
            self.assertEqual(a.size_bytes, 2 * 1000**2)
            self.assertEqual(len(a.sha256), 64)
            json.loads(m.to_json())

    def test_unknown_field_rejected(self):
        with self.assertRaises(ValueError):
            Manifest.from_dict({"version": "1", "assets": [{"id": "x", "url": "https://a", "sha256": "a" * 64,
                                                            "size_bytes": 2000000, "typo_field": 1}]})


class TestOrchestrator(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(Translator.detect("你好世界 hello"), ("zh", "en"))
        self.assertEqual(Translator.detect("please send the file"), ("en", "zh"))

    def test_end_to_end_with_retry_and_cache(self):
        # 第 1 次返回垃圾（触发重试），第 2 次返回合格译文
        eng = StubEngine(["好的，这是翻译：", "The total is 1,280 dollars."])
        t = Translator(eng, chunk_tokens=400, max_retries=1)
        r = t.translate("总计 1,280 美元。")
        self.assertEqual(len(eng.calls), 2, "第一次应被判失败并重试")
        self.assertIn("1,280", r.text)
        self.assertTrue(r.degraded)
        # 缓存：同文本第二次不再调用引擎
        eng2_calls = len(eng.calls)
        r2 = t.translate("总计 1,280 美元。")
        self.assertEqual(len(eng.calls), eng2_calls)
        self.assertTrue(r2.cache_hit)

    def test_glossary_change_invalidates_cache(self):
        eng = StubEngine(["we use vector database.", "we use vec db."])
        t = Translator(eng, chunk_tokens=400, max_retries=0)
        t.translate("我们用向量数据库。")
        t.set_glossary(Glossary.from_tsv("src,dst,mode\n向量数据库,vec db,enforce,,false"))
        r = t.translate("我们用向量数据库。")
        self.assertEqual(len(eng.calls), 2, "改术语表后必须重新生成")
        self.assertIn("vec db", r.text)

    def test_long_text_split_into_multiple_calls(self):
        eng = StubEngine(["a", "b", "c", "d", "e", "f"])
        t = Translator(eng, chunk_tokens=12, max_retries=0)
        t.translate("第一句话。第二句话。第三句话。第四句话。第五句话。第六句话。")
        self.assertGreater(len(eng.calls), 1)

    def test_cancel_stops_loop(self):
        eng = StubEngine(["x", "y"])
        t = Translator(eng, chunk_tokens=8, max_retries=0)
        r = t.translate("一。二。三。四。", cancel=lambda: True)
        self.assertEqual(len(r.chunks), 0)
        self.assertEqual(len(eng.calls), 0)

    def test_learn_injection_reaches_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            learner = Learner(os.path.join(d, "l.sqlite"))
            learner.record("服务器的响应超时了，请重试。", "The server timed out. Please retry.", "zh", "en", edited=True)
            captured = {}

            def spy(prompt):
                captured["user"] = prompt.user
                return "The server timed out. Please retry."

            eng = StubEngine([spy])
            t = Translator(eng, learner=learner, chunk_tokens=400, max_retries=0)
            t.translate("服务器的响应超时了，请稍后重试")
            self.assertIn("参考下面的翻译", captured["user"], "TM 命中必须以 few-shot 注入")
            self.assertIn("The server timed out", captured["user"])

    def test_accept_feeds_self_learning(self):
        with tempfile.TemporaryDirectory() as d:
            learner = Learner(os.path.join(d, "l.sqlite"))
            eng = StubEngine(["Please set the timeout to 30 seconds"])
            t = Translator(eng, learner=learner, chunk_tokens=400, max_retries=0)
            r = t.translate("请把超时时间设置为30秒")
            chunk = r.chunks[0]
            t.accept(chunk, final_text="Please configure the timeout to 30 seconds",
                     accepted_ms=6000, src="zh", tgt="en")
            self.assertGreaterEqual(learner.stats()["tm_rows"], 1)
            self.assertGreaterEqual(learner.stats()["tm_edited"], 1)
            # 编辑差异要落到 edits 表里，供后续统计挖掘
            self.assertTrue(learner.mine_candidates("zh", "en", min_support=1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
