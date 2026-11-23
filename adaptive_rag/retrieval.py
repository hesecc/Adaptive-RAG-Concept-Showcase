"""Actual lexical retrieval and rank fusion; no simulated embeddings."""

from collections import Counter, defaultdict
import asyncio
from dataclasses import replace
import math
import re
import unicodedata
from typing import Sequence

from .domain import ChildChunk, Hit, SubQuery, ValidationError


def tokenize(text: str) -> tuple[str, ...]:
    """Deterministic CJK uni/bigrams plus intact Latin/model identifiers.

    This is an explicit local baseline, not jieba or model tokenization. Keeping
    alphanumeric identifiers also avoids losing A01-500 to punctuation filters.
    """
    text = unicodedata.normalize("NFKC", text).casefold()
    result = []
    for word in re.findall(r"[a-z0-9]+(?:[._:-][a-z0-9]+)*|[\u3400-\u9fff]+", text):
        if "\u3400" <= word[0] <= "\u9fff":
            result.extend(word)
            result.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            result.append(word)
    return tuple(result)


class BM25Retriever:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (k1, b)) or k1 <= 0 or not 0 <= b <= 1:
            raise ValidationError("BM25 requires finite k1 > 0 and 0 <= b <= 1")
        self.k1, self.b = k1, b

    async def search(self, query: SubQuery, chunks: Sequence[ChildChunk], limit: int) -> tuple[Hit, ...]:
        if not chunks:
            return ()
        query_terms = set(tokenize(query.text))
        if not query_terms:
            return ()
        terms = []
        for index, chunk in enumerate(chunks):
            if index % 64 == 0:
                await asyncio.sleep(0)
            terms.append(Counter(tokenize(chunk.retrieval_text)))
        lengths = [sum(t.values()) for t in terms]
        avg = sum(lengths) / len(lengths) or 1
        dfs = Counter(term for counts in terms for term in counts)
        scored = []
        for index, (chunk, counts, length) in enumerate(zip(chunks, terms, lengths)):
            if index % 64 == 0:
                await asyncio.sleep(0)
            score = 0.0
            for term in query_terms:
                count = counts[term]
                if count:
                    idf = math.log(1 + (len(chunks) - dfs[term] + 0.5) / (dfs[term] + 0.5))
                    score += idf * count * (self.k1 + 1) / (count + self.k1 * (1 - self.b + self.b * length / avg))
            if score > 0:
                scored.append((chunk, score))
        scored.sort(key=lambda item: (-item[1], item[0].chunk_id))
        return tuple(Hit(query.query_id, chunk, "bm25", score, rank) for rank, (chunk, score) in enumerate(scored[:limit], 1))


class ReciprocalRankFusion:
    def __init__(self, k: int = 60):
        if type(k) is not int or k < 1:
            raise ValidationError("RRF k must be positive")
        self.k = k

    def fuse(self, query: SubQuery, lanes: Sequence[Sequence[Hit]], limit: int) -> tuple[Hit, ...]:
        scores = defaultdict(float)
        chunks = {}
        for lane in lanes:
            seen = set()
            rank = 0
            for hit in lane:
                if hit.query_id != query.query_id:
                    raise ValidationError("retrieval lane returned a different query_id")
                key = hit.chunk.chunk_id
                if key in seen:
                    continue
                seen.add(key)
                rank += 1
                chunks[key] = hit.chunk
                scores[key] += 1 / (self.k + rank)
        ordered = sorted(scores, key=lambda key: (-scores[key], key))[:limit]
        return tuple(Hit(query.query_id, chunks[key], "rrf", scores[key], rank) for rank, key in enumerate(ordered, 1))


class PassthroughReranker:
    """Preserves fused order. Capabilities explicitly say model rerank disabled."""
    async def rank(self, query: SubQuery, hits: Sequence[Hit], limit: int) -> tuple[Hit, ...]:
        return tuple(replace(hit, rank=i) for i, hit in enumerate(hits[:limit], 1))
