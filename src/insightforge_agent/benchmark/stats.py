"""Statistics, standard library only. Every function says when a result is undefined instead of
returning zero."""

import math
import random
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from statistics import NormalDist

Z95 = NormalDist().inv_cdf(0.975)
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20261010


@dataclass(frozen=True)
class Interval:
    low: float
    high: float


def wilson_interval(successes: int, trials: int) -> Interval | None:
    """Wilson score 95% interval for a proportion; None when there are no trials."""
    if trials < 0 or not 0 <= successes <= trials:
        raise ValueError("need 0 <= successes <= trials")
    if trials == 0:
        return None
    p, z2 = successes / trials, Z95 * Z95
    denom = 1 + z2 / trials
    centre = (p + z2 / (2 * trials)) / denom
    half = Z95 * math.sqrt(p * (1 - p) / trials + z2 / (4 * trials * trials)) / denom
    return Interval(max(0.0, centre - half), min(1.0, centre + half))


def _binom_cdf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))


def clopper_pearson_upper(failures: int, trials: int, alpha: float = 0.05) -> float | None:
    """One-sided (1 - alpha) upper bound on a failure rate; None when there are no trials.

    Zero failures use the closed form 1 - alpha ** (1/n). Otherwise the bound is the p at which
    P(X <= failures | p) = alpha, found by bisection on the binomial tail."""
    if trials < 0 or not 0 <= failures <= trials:
        raise ValueError("need 0 <= failures <= trials")
    if trials == 0:
        return None
    if failures == trials:
        return 1.0
    if failures == 0:
        return 1 - alpha ** (1 / trials)
    low, high = failures / trials, 1.0  # the cdf falls from >alpha at low to 0 at high
    for _ in range(200):
        mid = (low + high) / 2
        if _binom_cdf(failures, trials, mid) > alpha:
            low = mid
        else:
            high = mid
    return (low + high) / 2


@dataclass(frozen=True)
class Kappa:
    value: float | None
    reason: str | None = None  # why it is undefined


def cohens_kappa(rater_a: Sequence[Hashable], rater_b: Sequence[Hashable]) -> Kappa:
    """Cohen's kappa between two aligned label sequences. Undefined (never 0) when there are no
    pairs or when chance agreement is 1 (both raters used a single identical label)."""
    if len(rater_a) != len(rater_b):
        raise ValueError("raters must label the same items")
    n = len(rater_a)
    if n == 0:
        return Kappa(None, "no paired labels")
    observed = sum(a == b for a, b in zip(rater_a, rater_b)) / n
    labels = set(rater_a) | set(rater_b)
    chance = sum((rater_a.count(label) / n) * (rater_b.count(label) / n) for label in labels)
    if math.isclose(chance, 1.0):
        return Kappa(None, "chance agreement is 1: both raters used one identical label")
    return Kappa((observed - chance) / (1 - chance))


# ---- F1 --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Outcome:
    """One case for one candidate. `predicted_positive` is None when no prediction was made
    (ABSTAIN, ERROR, NOT_APPLICABLE, not recorded): a miss on a positive, never a hit."""

    expected_positive: bool
    predicted_positive: bool | None
    expected_label: str | None = None  # compared across candidates when paired


def f1_score(outcomes: Sequence[Outcome]) -> float | None:
    """2TP / (2TP + FP + FN); missing predictions on expected positives count as FN.
    None when TP + FP + FN is 0 (no positives expected and none predicted)."""
    tp = sum(o.expected_positive and o.predicted_positive is True for o in outcomes)
    fp = sum(not o.expected_positive and o.predicted_positive is True for o in outcomes)
    fn = sum(o.expected_positive and o.predicted_positive is not True for o in outcomes)
    denominator = 2 * tp + fp + fn
    return None if denominator == 0 else 2 * tp / denominator


@dataclass(frozen=True)
class BootstrapF1:
    point: float | None
    interval: Interval | None
    defined_resamples: int
    resamples: int


@dataclass(frozen=True)
class PairedBootstrap:
    candidates: dict[str, BootstrapF1]
    differences: dict[tuple[str, str], BootstrapF1]  # first minus second


def _percentile(sorted_values: list[float], q: float) -> float:
    position = q * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


def _interval(values: list[float]) -> Interval | None:
    if not values:
        return None
    ordered = sorted(values)
    return Interval(_percentile(ordered, 0.025), _percentile(ordered, 0.975))


def paired_bootstrap_f1(
    outcomes: Mapping[str, Mapping[str, Outcome]],
    *,
    seed: int = BOOTSTRAP_SEED,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> PairedBootstrap:
    """95% percentile intervals for each candidate's F1 and for every pairwise difference.

    `outcomes` maps candidate -> case id -> Outcome. Every candidate must cover the same case
    ids; each resample draws case ids once and applies them to all candidates, so the pairing
    is preserved. A resample in which an F1 is undefined is left out of that interval and
    counted in `resamples - defined_resamples`."""
    names = sorted(outcomes)
    if not names:
        raise ValueError("no candidates")
    case_ids = sorted(outcomes[names[0]])
    if any(sorted(outcomes[name]) != case_ids for name in names):
        raise ValueError("candidates are not aligned on the same case ids")
    for case_id in case_ids:  # the same reviewed truth must sit behind every candidate's row
        reference = outcomes[names[0]][case_id]
        for name in names[1:]:
            other = outcomes[name][case_id]
            if (other.expected_positive, other.expected_label) != (
                    reference.expected_positive, reference.expected_label):
                raise ValueError(f"expected labels differ for case {case_id!r} "
                                 f"between {names[0]!r} and {name!r}")
    rng = random.Random(seed)
    n = len(case_ids)
    scores: dict[str, list[float | None]] = {name: [] for name in names}
    if n:
        for _ in range(resamples):
            drawn = [case_ids[rng.randrange(n)] for _ in range(n)]
            for name in names:
                scores[name].append(f1_score([outcomes[name][c] for c in drawn]))
    else:
        resamples = 0

    def summarise(values: list[float | None], point: float | None) -> BootstrapF1:
        defined = [v for v in values if v is not None]
        return BootstrapF1(point, _interval(defined), len(defined), resamples)

    candidates = {
        name: summarise(scores[name], f1_score([outcomes[name][c] for c in case_ids]))
        for name in names
    }
    differences: dict[tuple[str, str], BootstrapF1] = {}
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            pairs = [(a, b) for a, b in zip(scores[first], scores[second])]
            deltas = [a - b for a, b in pairs if a is not None and b is not None]
            pa, pb = candidates[first].point, candidates[second].point
            point = None if pa is None or pb is None else pa - pb
            differences[(first, second)] = BootstrapF1(point, _interval(deltas), len(deltas), resamples)
    return PairedBootstrap(candidates, differences)
