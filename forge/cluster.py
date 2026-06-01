"""Clustering: group similar captures into proposed-skill candidates.

We use a deliberately simple approach:
- Build a TF-IDF vocabulary from (goal + pattern) of each capture.
- Compute pairwise cosine similarity.
- Single-link agglomerative clustering: any pair above threshold joins clusters.
- Keep clusters with >= min_size members.

This avoids sklearn so the install footprint stays tiny. For < 10k captures
it's plenty fast.
"""
from __future__ import annotations
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .capture import Capture


STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "of", "in", "on", "at", "to", "for", "with", "by", "from", "as",
    "and", "or", "but", "if", "then", "else", "not", "no", "yes",
    "i", "you", "we", "they", "it", "this", "that", "these", "those",
    "have", "has", "had", "do", "does", "did", "can", "could", "would",
    "should", "will", "shall", "may", "might", "must",
    "my", "your", "our", "their", "his", "her", "its",
    "me", "us", "them", "him", "self",
    "how", "what", "when", "where", "why", "who", "which",
    "any", "all", "some", "more", "less", "much", "many",
    "just", "also", "only", "even", "still", "very", "too",
    "im", "ive", "id", "ill", "thats", "whats", "dont", "didnt", "cant",
}

TOKEN_RE = re.compile(r"[a-z][a-z0-9_\-\.]{1,}")


def tokenize(text: str) -> list[str]:
    tokens = TOKEN_RE.findall(text.lower())
    return [t for t in tokens if t not in STOPWORDS and len(t) > 1]


@dataclass
class Cluster:
    id: str
    member_ids: list[str]
    fingerprint: str           # stable hash of sorted member_ids — drives "already drafted?"
    top_terms: list[str] = field(default_factory=list)
    representative_goal: str = ""

    def size(self) -> int:
        return len(self.member_ids)


def cluster_captures(
    captures: list[Capture],
    min_size: int = 3,
    threshold: float = 0.45,
) -> list[Cluster]:
    if len(captures) < min_size:
        return []

    # Build doc vectors.
    docs = [tokenize(f"{c.goal} {c.pattern} {' '.join(c.tools)}") for c in captures]

    # Document frequency for IDF.
    df: Counter = Counter()
    for d in docs:
        for term in set(d):
            df[term] += 1
    n_docs = len(docs)

    def tfidf(doc: list[str]) -> dict[str, float]:
        tf = Counter(doc)
        if not tf:
            return {}
        max_tf = max(tf.values())
        # Smoothed IDF: the +1 outside the log ensures terms appearing in every
        # document still contribute (otherwise IDF=0 on small corpora where
        # everything overlaps, yielding zero-norm vectors and 0 similarity).
        return {
            term: (count / max_tf) * (math.log((n_docs + 1) / (df[term] + 1)) + 1.0)
            for term, count in tf.items()
        }

    vectors = [tfidf(d) for d in docs]
    norms = [math.sqrt(sum(v * v for v in vec.values())) for vec in vectors]

    def cosine(i: int, j: int) -> float:
        if norms[i] == 0 or norms[j] == 0:
            return 0.0
        a, b = vectors[i], vectors[j]
        # Iterate over the smaller dict.
        if len(a) > len(b):
            a, b = b, a
        dot = sum(val * b.get(term, 0.0) for term, val in a.items())
        return dot / (norms[i] * norms[j])

    # Union-find for single-link clustering.
    parent = list(range(len(captures)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(len(captures)):
        for j in range(i + 1, len(captures)):
            if cosine(i, j) >= threshold:
                union(i, j)

    # Group by root.
    groups: dict[int, list[int]] = {}
    for i in range(len(captures)):
        groups.setdefault(find(i), []).append(i)

    clusters: list[Cluster] = []
    for root, members in groups.items():
        if len(members) < min_size:
            continue

        # Top terms across the cluster: sum tfidf weights.
        agg: Counter = Counter()
        for m in members:
            for term, weight in vectors[m].items():
                agg[term] += weight
        top_terms = [t for t, _ in agg.most_common(8)]

        # Representative goal: the longest one (more descriptive).
        rep = max((captures[m].goal for m in members), key=len)

        member_ids = sorted(captures[m].id for m in members)
        fingerprint = _fingerprint(member_ids)
        cluster_id = f"cl_{fingerprint[:10]}"
        clusters.append(
            Cluster(
                id=cluster_id,
                member_ids=member_ids,
                fingerprint=fingerprint,
                top_terms=top_terms,
                representative_goal=rep,
            )
        )

    clusters.sort(key=lambda c: c.size(), reverse=True)
    return clusters


def _fingerprint(member_ids: list[str]) -> str:
    import hashlib
    h = hashlib.sha256("|".join(sorted(member_ids)).encode()).hexdigest()
    return h[:16]


def write_clusters(clusters: list[Cluster], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(c) for c in clusters], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def read_clusters(path: Path) -> list[Cluster]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Cluster(**c) for c in data]
