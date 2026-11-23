"""Decides which ranked chunks count as the same as, or similar to, the question."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from adaptive_rag.app import RagApplication
from adaptive_rag.config import Settings, load_settings
from adaptive_rag.domain import (AccessContext, ChildChunk, Hit, IngestRequest, QueryRequest,
                                 SubQuery, ValidationError)
from adaptive_rag.environment import apply_environment
from adaptive_rag.plugins import default_registry
from adaptive_rag.relevance import RelevanceGate, lexical_overlap, question_terms


def hit(text, *, lane="rrf", score=1.0, rank=1, chunk_id="c1", query_id="q1"):
    chunk = ChildChunk("t", "doc", "v1", "r1", chunk_id, "p1", text, text, "line:1", "store", (), "zh")
    return Hit(query_id, chunk, lane, score, rank)


class QuestionTermsTests(unittest.TestCase):
    def test_question_wording_is_not_a_topic(self):
        terms = question_terms("退货需要什么条件")
        self.assertIn("退货", terms)
        self.assertIn("条件", terms)
        self.assertFalse([term for term in terms if "什么" in term or "么条" in term])

    def test_identifiers_keep_their_whole_form_and_english_filler_is_dropped(self):
        self.assertEqual(question_terms("GPT-4 的参数量是多少"), ("gpt-4", "参数", "数量"))
        self.assertEqual(question_terms("What is the attention mechanism"), ("attention", "mechanism"))
        self.assertEqual(question_terms("ＧＰＴ－４"), ("gpt-4",))

    def test_function_characters_do_not_break_ordinary_words(self):
        self.assertIn("会员", question_terms("会员怎么升级"))
        self.assertIn("到账", question_terms("退款多久能到账"))
        self.assertIn("申请", question_terms("怎么申请退货"))

    def test_terms_are_ordered_and_not_repeated(self):
        self.assertEqual(question_terms("退货 退货 保修"), ("退货", "保修"))

    def test_a_question_made_only_of_filler_has_no_terms(self):
        self.assertEqual(question_terms("这是什么？"), ())
        self.assertEqual(question_terms(""), ())


class LexicalOverlapTests(unittest.TestCase):
    def test_counts_terms_found_in_the_text_ignoring_case_and_width(self):
        self.assertEqual(lexical_overlap(("退货", "条件"), "可以申请七天无理由退货"), (1, 0.5))
        self.assertEqual(lexical_overlap(("gpt-4",), "Uses ＧＰＴ－４ internally"), (1, 1.0))

    def test_no_terms_means_no_overlap(self):
        self.assertEqual(lexical_overlap((), "任何文本"), (0, 0.0))


class RelevanceGateTests(unittest.TestCase):
    question = SubQuery("q1", "退货需要什么条件")

    def test_keeps_a_chunk_that_shares_enough_of_the_question(self):
        near = hit("七天无理由退货需满足商品未使用、包装完好等条件。")
        result = RelevanceGate().assess(self.question, [near])
        self.assertEqual(result.hits, (near,))
        self.assertEqual(result.detail["basis"], "lexical")
        self.assertEqual(result.detail["kept_count"], 1)

    def test_drops_a_chunk_that_only_shares_single_characters(self):
        # 货 and 条 appear, but none of the question's two-character terms does.
        far = hit("货架上的商品按条形码管理。")
        result = RelevanceGate().assess(self.question, [far])
        self.assertEqual(result.hits, ())
        self.assertEqual((result.detail["candidate_count"], result.detail["kept_count"]), (1, 0))
        self.assertFalse(result.detail["rows"][0]["kept"])

    def test_a_long_question_is_kept_by_three_matching_terms_even_below_the_share_floor(self):
        query = SubQuery("q1", "red orange yellow green blue indigo violet cyan magenta black white brown")
        gate = RelevanceGate(lexical_floor=0.3)
        self.assertEqual(gate.assess(query, [hit("red orange")]).hits, ())
        three = hit("red orange yellow")
        self.assertEqual(gate.assess(query, [three]).hits, (three,))

    def test_a_chunk_close_to_a_rewritten_wording_is_kept(self):
        query = SubQuery("q1", "质保")
        chunk = hit("申请材料：订单号和故障照片。")
        gate = RelevanceGate()
        self.assertEqual(gate.assess(query, [chunk]).hits, ())
        result = gate.assess(query, [chunk], ["申请材料"])
        self.assertEqual(result.hits, (chunk,))
        self.assertEqual(result.detail["variant_count"], 1)

    def test_a_reranker_score_keeps_a_chunk_that_shares_no_words(self):
        related = hit("七天内可以把商品寄回。", lane="rerank", score=0.9)
        unrelated = hit("门店营业时间为每天十点。", lane="rerank", score=0.01, chunk_id="c2", rank=2)
        result = RelevanceGate(rerank_floor=0.05).assess(self.question, [related, unrelated])
        self.assertEqual(result.hits, (related,))
        self.assertEqual(result.detail["basis"], "rerank")
        self.assertEqual(result.detail["top_rerank_score"], 0.9)

    def test_dense_retrieval_without_scores_leaves_nothing_to_reject(self):
        far = hit("货架上的商品按条形码管理。")
        result = RelevanceGate(dense=True).assess(self.question, [far])
        self.assertEqual(result.hits, (far,))
        self.assertEqual(result.detail["basis"], "unscored")
        scored = hit("货架上的商品按条形码管理。", lane="rerank", score=0.0)
        self.assertEqual(RelevanceGate(dense=True).assess(self.question, [scored]).hits, ())

    def test_a_question_without_terms_cannot_be_compared_so_nothing_is_rejected(self):
        any_chunk = hit("门店营业时间为每天十点。")
        result = RelevanceGate().assess(SubQuery("q1", "这是什么？"), [any_chunk])
        self.assertEqual(result.hits, (any_chunk,))
        self.assertEqual(result.detail["basis"], "unscored")

    def test_floors_are_validated(self):
        for value in (-0.1, 1.5, True, float("nan"), "0.3", None):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    RelevanceGate(lexical_floor=value)
                with self.assertRaises(ValidationError):
                    RelevanceGate(rerank_floor=value)


class RelevanceSettingsTests(unittest.TestCase):
    def test_defaults_enable_the_gate(self):
        settings = Settings()
        self.assertTrue(settings.relevance_gate)
        self.assertEqual((settings.relevance_lexical_floor, settings.relevance_rerank_floor), (0.3, 0.05))

    def test_settings_reject_invalid_values(self):
        for name, value in (("relevance_gate", "yes"), ("relevance_lexical_floor", 1.2),
                            ("relevance_rerank_floor", -1), ("relevance_lexical_floor", True),
                            ("relevance_rerank_floor", float("inf"))):
            with self.subTest(name=name, value=value), self.assertRaises(ValidationError):
                Settings(**{name: value})

    def test_environment_overrides_the_defaults(self):
        settings = apply_environment(Settings(), {"RAG_RELEVANCE_GATE": "false",
                                                  "RAG_RELEVANCE_LEXICAL_FLOOR": "0.5",
                                                  "RAG_RELEVANCE_RERANK_FLOOR": "0.2"})
        self.assertFalse(settings.relevance_gate)
        self.assertEqual((settings.relevance_lexical_floor, settings.relevance_rerank_floor), (0.5, 0.2))
        untouched = apply_environment(Settings(), {})
        self.assertEqual((untouched.relevance_gate, untouched.relevance_lexical_floor), (True, 0.3))

    def test_environment_rejects_malformed_values_without_echoing_them(self):
        for name, value in (("RAG_RELEVANCE_GATE", "maybe"), ("RAG_RELEVANCE_LEXICAL_FLOOR", "high"),
                            ("RAG_RELEVANCE_RERANK_FLOOR", "2")):
            with self.subTest(name=name), self.assertRaises(ValidationError) as caught:
                apply_environment(Settings(), {name: value})
            self.assertIn(name, str(caught.exception))
            self.assertNotIn(value, str(caught.exception).replace(name, ""))

    def test_toml_accepts_the_new_keys(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "rag.toml"
            path.write_text("relevance_gate = false\nrelevance_lexical_floor = 0.4\n", encoding="utf-8")
            settings = load_settings(path)
        self.assertEqual((settings.relevance_gate, settings.relevance_lexical_floor), (False, 0.4))


class RelevanceQueryTests(unittest.IsolatedAsyncioTestCase):
    """Whole-pipeline behaviour: nothing similar means a plain 'not found', with no model calls."""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.access = AccessContext("shop", "reader")

    def build(self, settings=None, registry=None):
        app = RagApplication(settings or self.settings, registry)
        staged = app.ingest(IngestRequest("shop", "policy", "v1", "退货期限为七天，不支持拆封后退货。", title="policy"))
        app.publish("shop", staged.job_id, None)
        return app

    def stage(self, app, result, name):
        trace = app.get_trace("shop", result.trace_id)
        return next(item for item in trace["stages"] if item["name"] == name)

    async def test_question_sharing_only_single_characters_is_reported_as_not_found(self):
        app = self.build()
        result = await app.query(QueryRequest(self.access, "期货交易所在哪里"))
        self.assertEqual(result.status, "empty")
        self.assertFalse(result.evidence)
        self.assertIn("没有找到", result.message)
        self.assertIn("相同或相似", result.message)
        self.assertIn("词面", result.message)
        gate = self.stage(app, result, "relevance.q1")["output"]
        self.assertGreater(gate["candidate_count"], 0)
        self.assertEqual(gate["kept_count"], 0)
        trace = app.get_trace("shop", result.trace_id)
        self.assertEqual((trace["model_calls"], trace["network_calls"]), (0, 0))

    async def test_a_related_question_still_returns_its_source(self):
        app = self.build()
        result = await app.query(QueryRequest(self.access, "退货期限是多久"))
        self.assertEqual(result.status, "evidence_preview")
        self.assertEqual([item.document_id for item in result.evidence], ["policy"])
        self.assertEqual(self.stage(app, result, "relevance.q1")["output"]["kept_count"], 1)

    async def test_the_gate_can_be_switched_off(self):
        self.build()
        app = RagApplication(replace(self.settings, relevance_gate=False))
        result = await app.query(QueryRequest(self.access, "期货交易所在哪里"))
        self.assertEqual(result.status, "evidence_preview")
        self.assertEqual(self.stage(app, result, "relevance")["status"], "disabled")

    async def test_reranker_scores_decide_without_the_word_hint(self):
        class Reranker:
            def __init__(self, score):
                self.score = score

            async def rank(self, query, hits, limit):
                return tuple(Hit(h.query_id, h.chunk, "rerank", self.score, i) for i, h in enumerate(hits, 1))

        for score, status in ((0.9, "evidence_preview"), (0.001, "empty")):
            with self.subTest(score=score):
                registry = default_registry()
                registry.register("reranker", "fixture", lambda s, value=score: Reranker(value))
                settings = replace(self.settings, data_dir=Path(self.temp.name) / f"score-{score}",
                                   plugins={"reranker": "fixture"})
                app = self.build(settings, registry)
                result = await app.query(QueryRequest(self.access, "期货交易所在哪里"))
                self.assertEqual(result.status, status)
                if status == "empty":
                    self.assertNotIn("词面", result.message)
                    self.assertEqual(self.stage(app, result, "relevance.q1")["output"]["basis"], "rerank")

    async def test_a_second_question_without_a_match_is_listed_as_missing(self):
        app = self.build()
        result = await app.query(QueryRequest(self.access, "退货期限？期货交易所在哪里？"))
        self.assertEqual(result.status, "evidence_preview")
        self.assertEqual(result.missing_query_ids, ("q2",))
        self.assertEqual({q for item in result.evidence for q in item.query_ids}, {"q1"})


if __name__ == "__main__":
    unittest.main()
