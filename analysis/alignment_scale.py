"""Common rational scales for two V2 alignment packages.

The merged order is a poset of width at most two: each station contributes one
strict chain of grade levels and reference anchors only identify grades across
the two chains. Feasibility is a union-find check, while the extremal pooled
AUC values are linear extensions found with a grid dynamic program over the
two chains (anchor-equal grades are forced shared checkpoints).
"""
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction

from handoff.alignment import AlignmentEvidence


@dataclass(frozen=True)
class ScaleWitness:
    auc: Fraction
    order: tuple[str, ...]
    values: dict[str, Fraction]


@dataclass(frozen=True)
class ScaleModel:
    stations: tuple[str, ...]
    level_roots: dict[str, tuple[str, ...]]
    nodes: dict[str, tuple[int, int]]
    chain_a: tuple[str, ...]
    chain_b: tuple[str, ...]
    shared: frozenset[str]
    shared_positions: tuple[tuple[int, int], ...]
    tie_weight: int
    positive_total: int
    negative_total: int


class _UnionFind:
    def __init__(self, keys):
        self.parent = {key: key for key in keys}

    def find(self, key):
        root = key
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[key] != key:
            self.parent[key], key = root, self.parent[key]
        return root

    def union(self, left, right):
        self.parent[self.find(left)] = self.find(right)


def build_scale_model(packages: tuple[AlignmentEvidence, ...]) -> ScaleModel | None:
    stations = tuple(sorted(package.source.station for package in packages))
    ordered = sorted(packages, key=lambda package: package.source.station)
    level_keys = {
        station: [f'{station}:{index}' for index in range(len(package.levels))]
        for station, package in zip(stations, ordered, strict=True)
    }
    finder = _UnionFind(key for keys in level_keys.values() for key in keys)
    first_anchors = dict(ordered[0].anchors)
    second_anchors = dict(ordered[1].anchors)
    for ticket, info in first_anchors.items():
        finder.union(level_keys[stations[0]][info.level],
                     level_keys[stations[1]][second_anchors[ticket].level])

    # Strict within-station order may not cross an anchor equality class.
    for keys in level_keys.values():
        for lower, higher in zip(keys, keys[1:]):
            if finder.find(lower) == finder.find(higher):
                return None

    roots = {}
    positions: dict[str, dict[str, int]] = {}
    counts: dict[str, list[int]] = {}
    for station, package in zip(stations, ordered, strict=True):
        for index, (positive, negative) in enumerate(package.levels):
            root = finder.find(level_keys[station][index])
            roots[level_keys[station][index]] = root
            bucket = counts.setdefault(root, [0, 0])
            bucket[0] += positive
            bucket[1] += negative
            positions.setdefault(root, {})[station] = index

    level_roots = {
        station: tuple(dict.fromkeys(roots[key] for key in level_keys[station]))
        for station in stations
    }
    chain_a, chain_b = (level_roots[station] for station in stations)
    shared = frozenset(
        root for root, present in positions.items() if len(present) == 2
    )
    # Feasibility requires shared checkpoints in the same relative order on both chains.
    checkpoints_a = [key for key in chain_a if key in shared]
    checkpoints_b = [key for key in chain_b if key in shared]
    if checkpoints_a != checkpoints_b:
        return None
    shared_positions = tuple(
        (chain_a.index(key), chain_b.index(key)) for key in checkpoints_a
    )

    nodes = {root: (bucket[0], bucket[1]) for root, bucket in counts.items()}
    positive_total = sum(positive for positive, _ in nodes.values())
    negative_total = sum(negative for _, negative in nodes.values())
    # Every positive/negative pair landing on one merged grade value ties,
    # including pairs coming from the two different stations.
    tie_weight = sum(positive * negative for positive, negative in nodes.values())
    return ScaleModel(tuple(stations), level_roots, nodes, tuple(chain_a),
                      tuple(chain_b), shared, shared_positions, tie_weight,
                      positive_total, negative_total)


def _negatives_before(model: ScaleModel, i: int, j: int) -> int:
    # Shared roots appear on both chains but hold one merged count.
    seen = set()
    total = 0
    for key in model.chain_a[:i]:
        if key not in seen:
            seen.add(key)
            total += model.nodes[key][1]
    for key in model.chain_b[:j]:
        if key not in seen:
            seen.add(key)
            total += model.nodes[key][1]
    return total


def _placement_gain(model: ScaleModel, key: str, i: int, j: int) -> Fraction:
    # Grades are placed from low to high. When grade x is placed above every
    # earlier grade, only its positives can still win a decision, against the
    # negatives already placed; its negatives can only lose to earlier
    # positives, which are counted as losses (zero wins). Same-grade pairs are
    # accounted separately through the model's tie weight.
    positive, _ = model.nodes[key]
    return Fraction(positive * _negatives_before(model, i, j))


def _extremal_order(model: ScaleModel, *, maximize: bool) -> tuple[Fraction, tuple[str, ...]]:
    n_a, n_b = len(model.chain_a), len(model.chain_b)
    end = (n_a, n_b)
    memo: dict[tuple[int, int], tuple[Fraction, tuple[str, ...]] | None] = {end: (Fraction(0), ())}

    def better(candidate: Fraction, incumbent: Fraction | None) -> bool:
        if incumbent is None:
            return True
        return candidate > incumbent if maximize else candidate < incumbent

    def solve(i: int, j: int) -> tuple[Fraction, tuple[str, ...]] | None:
        if (i, j) in memo:
            return memo[(i, j)]
        options: list[tuple[Fraction, str, tuple[int, int]]] = []
        checkpoint = next(
            ((pa, pb) for pa, pb in model.shared_positions if pa == i and pb == j),
            None,
        )
        if checkpoint is not None:
            key = model.chain_a[i]
            gain = _placement_gain(model, key, i, j)
            rest = solve(i + 1, j + 1)
            if rest is not None:
                options.append((gain + rest[0], key, (i + 1, j + 1)))
        else:
            upcoming = next(
                ((pa, pb) for pa, pb in model.shared_positions if pa >= i and pb >= j),
                None,
            )
            if i < n_a and model.chain_a[i] not in model.shared and (
                    upcoming is None or i < upcoming[0]):
                key = model.chain_a[i]
                gain = _placement_gain(model, key, i, j)
                rest = solve(i + 1, j)
                if rest is not None:
                    options.append((gain + rest[0], key, (i + 1, j)))
            if j < n_b and model.chain_b[j] not in model.shared and (
                    upcoming is None or j < upcoming[1]):
                key = model.chain_b[j]
                gain = _placement_gain(model, key, i, j)
                rest = solve(i, j + 1)
                if rest is not None:
                    options.append((gain + rest[0], key, (i, j + 1)))
        if not options:
            memo[(i, j)] = None
            return None
        best_value, best_key, best_state = options[0]
        for value, key, state in options[1:]:
            if better(value, best_value):
                best_value, best_key, best_state = value, key, state
        rest_value, rest_order = memo[best_state]
        result = (best_value, (best_key,) + rest_order)
        memo[(i, j)] = result
        return result

    solved = solve(0, 0)
    if solved is None:
        raise RuntimeError('no linear extension exists for a feasible scale model')
    return solved


def extremal_scale(model: ScaleModel, *, maximize: bool) -> ScaleWitness:
    value, order = _extremal_order(model, maximize=maximize)
    # value counts strict-win positive/negative pairs once; same-value pairs
    # contribute one half. The pooled AUC divides by every positive/negative pair.
    pairs = model.positive_total * model.negative_total
    auc = Fraction(2 * value + model.tie_weight, 2 * pairs)
    values = {key: Fraction(index) for index, key in enumerate(order)}
    return ScaleWitness(auc, order, values)


def station_scales(model: ScaleModel, witness: ScaleWitness) -> dict[str, list[str]]:
    return {
        station: [str(witness.values[root]) for root in model.level_roots[station]]
        for station in model.stations
    }


def pooled_auc(packages: tuple[AlignmentEvidence, ...], scales: Mapping[str, list[str]]) -> Fraction:
    levels = []
    for package in packages:
        station = package.source.station
        for index, (positive, negative) in enumerate(package.levels):
            levels.append((Fraction(scales[station][index]), positive, negative))
    positive_total = sum(positive for _, positive, _ in levels)
    negative_total = sum(negative for _, _, negative in levels)
    doubled = 0
    for positive_score, positive_count, _ in levels:
        for negative_score, _, negative_count in levels:
            multiplicity = positive_count * negative_count
            if positive_score > negative_score:
                doubled += 2 * multiplicity
            elif positive_score == negative_score:
                doubled += multiplicity
    return Fraction(doubled, 2 * positive_total * negative_total)
