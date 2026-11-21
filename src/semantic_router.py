"""Lightweight TF-IDF + char-ngram semantic difficulty router (zero external dependencies)."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, List, Sequence, Tuple

HARD_ANCHORS: Tuple[str, ...] = (
    "Prove that the time complexity or correctness of an algorithm holds under edge cases",
    "Analyze race conditions, deadlocks, memory ordering, or distributed consensus trade-offs",
    "Compare CAP theorem, Raft, Paxos, and eventual consistency failure modes in production",
    "Derive a multi-step mathematical proof, recurrence relation, or formal verification argument",
    "Debug a subtle concurrency bug, async event loop starvation, or cache invalidation anomaly",
    "Design a fault-tolerant multi-region architecture with backpressure and SLA guarantees",
    "Explain why two seemingly equivalent SQL or distributed transactions behave differently under isolation",
    "分析高併發下的競態條件、死鎖或分散式系統一致性權衡與容錯設計",
    "推導演算法時間複雜度證明、邊界條件或嚴格數學歸納法",
    "比較不同系統架構在延遲、吞吐量與成本之間的取捨與潛在風險",
)

SIMPLE_ANCHORS: Tuple[str, ...] = (
    "Summarize this short support ticket in two sentences",
    "Translate this short paragraph into Traditional Chinese or English",
    "Extract the user name, status, and priority as JSON",
    "What is the capital of France or a basic definition of HTTP status 404",
    "Explain what a circuit breaker is in two simple sentences",
    "Format this list of items into clean bullet points",
    "Write a polite short email reply confirming the meeting time",
    "用兩句話摘要這段客服工單內容",
    "將以下句子翻譯成英文或繁體中文",
    "從文字中擷取姓名與日期並輸出 JSON",
)

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def _featurize(text: str) -> Counter[str]:
    norm = re.sub(r"\s+", " ", text.lower().strip())
    tokens = _TOKEN_RE.findall(norm)
    feats: Counter[str] = Counter(tokens)
    # Add character 3-grams for morphological / CJK compound robustness
    if len(norm) >= 3:
        for i in range(len(norm) - 2):
            gram = norm[i : i + 3]
            if not gram.isspace():
                feats[f"c3:{gram}"] += 1
    return feats


class SemanticDifficultyRouter:
    """Zero-dependency cosine-similarity router over hard vs. simple semantic prototypes."""

    def __init__(
        self,
        hard_anchors: Sequence[str] = HARD_ANCHORS,
        simple_anchors: Sequence[str] = SIMPLE_ANCHORS,
        threshold: float = 0.045,
    ) -> None:
        self.threshold = threshold
        all_docs = list(hard_anchors) + list(simple_anchors)
        n_docs = len(all_docs)
        df: Counter[str] = Counter()
        doc_feats: List[Counter[str]] = []
        for doc in all_docs:
            f = _featurize(doc)
            doc_feats.append(f)
            for term in f:
                df[term] += 1

        self._idf: Dict[str, float] = {
            term: math.log((1.0 + n_docs) / (1.0 + freq)) + 1.0
            for term, freq in df.items()
        }
        split = len(hard_anchors)
        self._hard_vecs = [self._to_tfidf(f) for f in doc_feats[:split]]
        self._simple_vecs = [self._to_tfidf(f) for f in doc_feats[split:]]

    def _to_tfidf(self, feats: Counter[str]) -> Dict[str, float]:
        vec: Dict[str, float] = {}
        norm_sq = 0.0
        for term, tf in feats.items():
            idf = self._idf.get(term)
            if idf is None:
                continue
            w = (1.0 + math.log(tf)) * idf
            vec[term] = w
            norm_sq += w * w
        if norm_sq > 0.0:
            inv = 1.0 / math.sqrt(norm_sq)
            for term in vec:
                vec[term] *= inv
        return vec

    @staticmethod
    def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
        if len(a) > len(b):
            a, b = b, a
        return sum(val * b.get(term, 0.0) for term, val in a.items())

    def score(self, prompt: str) -> Tuple[float, float, float]:
        """Return (margin, max_hard_sim, max_simple_sim) where margin > 0 means harder."""
        q_vec = self._to_tfidf(_featurize(prompt))
        if not q_vec:
            return 0.0, 0.0, 0.0
        hard_sim = max(self._cosine(q_vec, hv) for hv in self._hard_vecs)
        simple_sim = max(self._cosine(q_vec, sv) for sv in self._simple_vecs)
        return round(hard_sim - simple_sim, 4), round(hard_sim, 4), round(simple_sim, 4)

    def is_hard(self, prompt: str) -> Tuple[bool, float]:
        margin, _, _ = self.score(prompt)
        return margin >= self.threshold, margin


_DEFAULT_SEMANTIC_ROUTER = SemanticDifficultyRouter()


def classify_semantic_difficulty(prompt: str) -> Tuple[bool, float]:
    """Classify whether a prompt semantically resembles complex reasoning tasks."""
    return _DEFAULT_SEMANTIC_ROUTER.is_hard(prompt)
