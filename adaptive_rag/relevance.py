"""Decides whether retrieved content is the same as, or similar to, a question.

Retrieval always ranks something: BM25 scores any chunk that shares one character
with the question, and dense retrieval has no cut-off. These rules say when a
ranked chunk is too far from the question to be shown as a source, so an empty
result can honestly mean "nothing in the readable knowledge base is similar".

Two signals are used. A reranker score is a semantic judgement, so it is trusted
when present. Without one, the check falls back to how many of the question's
topic terms the chunk contains. That fallback sees words, not meaning: it cannot
connect a paraphrase to its source, and it can keep a chunk that merely shares
common words with the question. The check is deliberately lenient because wrongly
saying "not found" is worse than showing a weak source.
"""

from dataclasses import dataclass
import math
import re
import unicodedata
from typing import Sequence

from .domain import Hit, SubQuery, ValidationError
from .figures import plain_text

# A chunk with this many of the question's terms is similar however long the question is.
ANCHOR_TERMS = 3

# Question phrasing carries no topic. It is cut out of the question before the
# remaining text is split into terms, so "是什么" never anchors a match.
_STOP_WORDS = tuple(sorted({
    "什么样", "什么", "怎么样", "怎么办", "怎么", "怎样", "如何", "为什么", "为何", "哪里", "哪儿", "哪个",
    "哪些", "哪种", "哪位", "多少", "多久", "多长", "多大", "几个", "几时", "是否", "能否", "可否", "是不是",
    "有没有", "能不能", "可不可以", "会不会", "要不要", "好不好", "请问", "告诉我", "想知道", "想问", "我想",
    "帮我", "麻烦", "一下", "一些", "一个", "这个", "那个", "这些", "那些", "我们", "你们", "他们", "你的",
    "我的", "还是", "或者", "以及", "并且", "而且", "但是", "因为", "所以", "如果", "那么", "这样", "可以",
    "应该", "到底", "究竟", "关于", "对于", "有关", "的话", "是的",
}, key=len, reverse=True))
# Single characters that almost never carry a topic. Characters that are part of common
# topic words (会员, 到账, 个人, 等级, 申请) are deliberately absent so those words survive.
_STOP_CHARS = frozenset("的了吗呢吧啊呀嘛么哦是在有和与及或也都就还又把被对给向从并而但如若将能可要我你他她它您们这那此哪谁几何怎啥之其为以")
_ENGLISH_STOP = frozenset((
    "a an the is are was were be been am do does did of to in on at for by with from and or not no what which who "
    "whom whose when where why how can could would should will shall may might must it its this that these those "
    "there here as if than then so about into over under me my we our you your he she they them his her their "
    "tell please give show".split()))
_SEGMENT = re.compile(r"[a-z0-9]+(?:[._:-][a-z0-9]+)*|[\u3400-\u9fff]+")


def question_terms(text: str) -> tuple[str, ...]:
    """Topic-bearing terms of a question, in order and without repeats.

    Chinese terms are character pairs that contain no stop character; Latin and
    number identifiers keep their whole form. Single characters never count.
    """
    text = unicodedata.normalize("NFKC", text).casefold()
    for word in _STOP_WORDS:
        text = text.replace(word, " ")
    terms: list[str] = []
    for word in _SEGMENT.findall(text):
        if "\u3400" <= word[0] <= "\u9fff":
            terms.extend(word[i:i + 2] for i in range(len(word) - 1)
                         if word[i] not in _STOP_CHARS and word[i + 1] not in _STOP_CHARS)
        elif len(word) > 1 and word not in _ENGLISH_STOP:
            terms.append(word)
    return tuple(dict.fromkeys(terms))


def _overlap(terms: Sequence[str], normalized: str) -> tuple[int, float]:
    if not terms:
        return 0, 0.0
    matched = sum(1 for term in terms if term in normalized)
    return matched, matched / len(terms)


def lexical_overlap(terms: Sequence[str], text: str) -> tuple[int, float]:
    """How many of the terms appear in the text, and what share of all terms that is."""
    return _overlap(terms, unicodedata.normalize("NFKC", text).casefold())


@dataclass(frozen=True)
class Relevance:
    """Hits that count as similar content, plus the numbers behind the decision."""
    hits: tuple[Hit, ...]
    detail: dict


def _floor(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValidationError(f"{name} must be a finite number between 0 and 1")
    return value


class RelevanceGate:
    """Keeps a ranked hit only when it is lexically close to the question or a reranker scored it relevant.

    lexical_floor: share of the question's terms a chunk must contain (or ANCHOR_TERMS of them).
    rerank_floor: lowest reranker score that still counts as relevant.
    dense: dense retrieval is on, so a chunk can be related without sharing words. When nothing
        scores relevance there is no basis to reject such a chunk and every hit is kept.
    """

    def __init__(self, lexical_floor: float = 0.3, rerank_floor: float = 0.05, *, dense: bool = False):
        self.lexical_floor = _floor(lexical_floor, "lexical_floor")
        self.rerank_floor = _floor(rerank_floor, "rerank_floor")
        self.dense = bool(dense)

    def assess(self, query: SubQuery, hits: Sequence[Hit], variants: Sequence[str] = ()) -> Relevance:
        """Judge hits for a question. A chunk found through a rewrite or a repair query is close
        to the question when it is close to any of its wordings, so those come in as variants."""
        terms = question_terms(query.text)
        term_sets = [found for found in (terms, *(question_terms(text) for text in variants)) if found]
        scored = any(hit.lane == "rerank" for hit in hits)
        # Nothing to compare: dense hits that no reranker scored, or a question without topic terms.
        unscored = not scored and (self.dense or not term_sets)
        kept, rows = [], []
        for hit in hits:
            # A figure line holds a storage key; only its caption is wording a question can match.
            normalized = unicodedata.normalize("NFKC", plain_text(hit.chunk.retrieval_text)).casefold()
            overlaps = [_overlap(found, normalized) for found in term_sets]
            matched, share = max(overlaps, key=lambda value: (value[1], value[0]), default=(0, 0.0))
            lexical = any(count > 0 and (part >= self.lexical_floor or count >= ANCHOR_TERMS)
                          for count, part in overlaps)
            reranked = hit.lane == "rerank"
            keep = unscored or lexical or (reranked and hit.score >= self.rerank_floor)
            rows.append({"chunk_id": hit.chunk.chunk_id, "matched_terms": matched, "term_share": round(share, 4),
                         "rerank_score": hit.score if reranked else None, "kept": keep})
            if keep:
                kept.append(hit)
        scores = [row["rerank_score"] for row in rows if row["rerank_score"] is not None]
        return Relevance(tuple(kept), {
            "query_id": query.query_id, "basis": "rerank" if scored else "unscored" if unscored else "lexical",
            "terms": list(terms), "variant_count": len(variants),
            "candidate_count": len(hits), "kept_count": len(kept),
            "lexical_floor": self.lexical_floor, "rerank_floor": self.rerank_floor,
            "top_term_share": max((row["term_share"] for row in rows), default=0.0),
            "top_rerank_score": max(scores, default=None),
            "rows": rows[:20],
        })
