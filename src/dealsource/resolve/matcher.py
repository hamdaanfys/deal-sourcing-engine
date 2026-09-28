"""Pairwise matching and clustering.

Rules, in order (thresholds are explained in DESIGN.md §7.2):

1. Both records have a website domain:
   - same domain  -> merge (a company's domain is the strongest identifier we have). If the
     names are very different the merge still happens but is listed for review.
   - different domains -> never merged automatically. One company can own two domains, so a
     pair with the same name *and* the same location is listed for review.
2. Otherwise compare normalized names (0-100) and locations:
   - similarity >= AUTO_MERGE and same state (and city, if both have one) -> merge
   - similarity >= REVIEW, or a strong name match without a location match -> review only
   - anything else -> treated as different companies
3. Clustering takes accepted merges strongest-first (union-find) and refuses any merge that
   would put two different domains, or an analyst "split" pair, in one company.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import combinations

from rapidfuzz import fuzz

AUTO_MERGE = 93.0
REVIEW = 85.0
DOMAIN_NAME_MISMATCH = 50.0
MAX_BLOCK_SIZE = 2000

MERGE, REVIEW_ONLY, DISTINCT = "merge", "review", "distinct"


@dataclass(frozen=True)
class MatchRecord:
    id: int
    ref: str  # "source:source_record_id", used by overrides and the review file
    name: str
    key: str
    domain: str | None
    state: str | None
    city: str | None
    country: str | None
    uei: str | None = None  # SAM.gov Unique Entity ID, shared by SAM and USAspending records


@dataclass(frozen=True)
class PairDecision:
    a: int
    b: int
    decision: str
    method: str
    score: float  # name similarity, 0-100
    reason: str

    @property
    def strength(self) -> float:
        # Domain matches outrank any name match; forced merges outrank everything.
        bonus = {"override": 1000.0, "uei": 500.0, "domain": 200.0}.get(self.method, 0.0)
        return bonus + self.score


def name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a.replace(" ", "") == b.replace(" ", ""):  # "metal works" vs "metalworks"
        return 100.0
    return float(fuzz.token_sort_ratio(a, b))


def location_relation(a: MatchRecord, b: MatchRecord) -> str:
    """'match', 'partial' (same state, different city), 'conflict' or 'unknown'."""
    if a.country and b.country and a.country != b.country:
        return "conflict"
    if not a.state or not b.state:
        return "unknown"
    if a.state != b.state:
        return "conflict"
    if a.city and b.city and a.city != b.city:
        return "partial"
    return "match"


def compare(a: MatchRecord, b: MatchRecord) -> PairDecision:
    sim = name_similarity(a.key, b.key)
    if a.uei and b.uei and a.uei == b.uei:
        # The same federal registration: one legal entity, whatever the names or websites say.
        return PairDecision(a.id, b.id, MERGE, "uei", sim, "same UEI")
    if a.domain and b.domain:
        if a.domain == b.domain:
            if sim < DOMAIN_NAME_MISMATCH:
                return PairDecision(a.id, b.id, MERGE, "domain", sim, "same domain, names differ")
            return PairDecision(a.id, b.id, MERGE, "domain", sim, "same domain")
        if sim >= AUTO_MERGE and location_relation(a, b) == "match":
            return PairDecision(
                a.id,
                b.id,
                REVIEW_ONLY,
                "domain_conflict",
                sim,
                "same name and location, different domains",
            )
        return PairDecision(a.id, b.id, DISTINCT, "domain_conflict", sim, "different domains")

    loc = location_relation(a, b)
    if sim >= AUTO_MERGE and loc == "match":
        return PairDecision(a.id, b.id, MERGE, "name_location", sim, "similar name, same location")
    if sim >= AUTO_MERGE:
        reason = {
            "conflict": "similar name, different state",
            "partial": "similar name, same state, different city",
            "unknown": "similar name, location unknown",
        }[loc]
        return PairDecision(a.id, b.id, REVIEW_ONLY, "name", sim, reason)
    if sim >= REVIEW:
        return PairDecision(
            a.id, b.id, REVIEW_ONLY, "name", sim, f"possible name match, location {loc}"
        )
    return PairDecision(a.id, b.id, DISTINCT, "name", sim, "names differ")


def blocking_keys(r: MatchRecord) -> set[tuple[str, str]]:
    """Cheap keys that any true match is very likely to share, so we avoid comparing all pairs."""
    keys: set[tuple[str, str]] = set()
    if r.uei:
        keys.add(("uei", r.uei))
    if r.domain:
        keys.add(("domain", r.domain))
    tokens = r.key.split()
    if tokens:
        keys.add(("first", tokens[0]))
        compact = r.key.replace(" ", "")
        keys.add(("prefix", compact[:4]))
    return keys


def candidate_pairs(records: list[MatchRecord]) -> tuple[set[tuple[int, int]], int]:
    """Pairs sharing a blocking key. Returns (pairs, number of oversized blocks skipped)."""
    blocks: dict[tuple[str, str], list[int]] = defaultdict(list)
    for r in records:
        for k in blocking_keys(r):
            blocks[k].append(r.id)
    pairs: set[tuple[int, int]] = set()
    skipped = 0
    for key, ids in blocks.items():
        if len(ids) > MAX_BLOCK_SIZE and key[0] not in ("domain", "uei"):
            skipped += 1
            continue
        for a, b in combinations(sorted(ids), 2):
            pairs.add((a, b))
    return pairs, skipped


@dataclass
class Clustering:
    clusters: list[list[int]]
    accepted: list[PairDecision]
    rejected: list[tuple[PairDecision, str]]  # merges refused by a constraint, with the reason
    review: list[PairDecision]
    oversized_blocks: int = 0
    edges_by_record: dict[int, list[PairDecision]] = field(default_factory=dict)


class _UnionFind:
    def __init__(self, records: Iterable[MatchRecord], cannot_link: dict[int, set[int]]):
        self.parent: dict[int, int] = {}
        self.members: dict[int, set[int]] = {}
        self.domains: dict[int, set[str]] = {}
        self.forbidden: dict[int, set[int]] = {}
        for r in records:
            self.parent[r.id] = r.id
            self.members[r.id] = {r.id}
            self.domains[r.id] = {r.domain} if r.domain else set()
            self.forbidden[r.id] = set(cannot_link.get(r.id, ()))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int, *, ignore_domains: bool = False) -> str | None:
        """Merge the clusters of a and b; return a reason string if a constraint forbids it."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return None
        if self.forbidden[ra] & self.members[rb]:
            return "analyst split override"
        if (
            not ignore_domains
            and self.domains[ra]
            and self.domains[rb]
            and self.domains[ra] != self.domains[rb]
        ):
            return "would join different domains"
        if len(self.members[ra]) < len(self.members[rb]):
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.members[ra] |= self.members.pop(rb)
        self.domains[ra] |= self.domains.pop(rb)
        self.forbidden[ra] |= self.forbidden.pop(rb)
        return None


def cluster(
    records: list[MatchRecord],
    *,
    force_merge: Iterable[tuple[int, int]] = (),
    force_split: Iterable[tuple[int, int]] = (),
) -> Clustering:
    by_id = {r.id: r for r in records}
    force_merge = [tuple(sorted(p)) for p in force_merge]
    force_split = {tuple(sorted(p)) for p in force_split}
    contradictions = force_split & set(force_merge)
    if contradictions:
        raise ValueError(f"Overrides both merge and split {len(contradictions)} pair(s)")

    cannot_link: dict[int, set[int]] = defaultdict(set)
    for a, b in force_split:
        cannot_link[a].add(b)
        cannot_link[b].add(a)

    pairs, oversized = candidate_pairs(records)
    decisions = [compare(by_id[a], by_id[b]) for a, b in sorted(pairs)]
    merges = [d for d in decisions if d.decision == MERGE and (d.a, d.b) not in force_split]

    forced = [
        PairDecision(
            a,
            b,
            MERGE,
            "override",
            name_similarity(by_id[a].key, by_id[b].key),
            "analyst merge override",
        )
        for a, b in force_merge
    ]

    uf = _UnionFind(records, cannot_link)
    accepted: list[PairDecision] = []
    rejected: list[tuple[PairDecision, str]] = []
    # Strongest evidence first, so when a constraint forces a cut, the weakest link is the one cut.
    for d in sorted(forced + merges, key=lambda d: (-d.strength, d.a, d.b)):
        why = uf.union(d.a, d.b, ignore_domains=d.method in ("override", "uei"))
        if why is None:
            accepted.append(d)
        elif uf.find(d.a) != uf.find(d.b):
            rejected.append((d, why))

    # Review: near-misses that ended up in different companies, plus domain merges whose names
    # disagree (merged, but worth a look).
    review = [
        d
        for d in decisions
        if d.decision == REVIEW_ONLY
        and (d.a, d.b) not in force_split
        and uf.find(d.a) != uf.find(d.b)
    ]
    review += [d for d in accepted if d.method == "domain" and d.score < DOMAIN_NAME_MISMATCH]

    groups: dict[int, list[int]] = defaultdict(list)
    for r in records:
        groups[uf.find(r.id)].append(r.id)
    clusters = sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])

    edges: dict[int, list[PairDecision]] = defaultdict(list)
    for d in accepted:
        edges[d.a].append(d)
        edges[d.b].append(d)
    return Clustering(clusters, accepted, rejected, review, oversized, dict(edges))
