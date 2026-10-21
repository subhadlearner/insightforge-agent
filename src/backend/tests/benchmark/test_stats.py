import math

import pytest

from insightforge_agent.benchmark.stats import (
    Outcome,
    clopper_pearson_upper,
    cohens_kappa,
    f1_score,
    paired_bootstrap_f1,
    wilson_interval,
)

# ---- Wilson: published reference values (Newcombe 1998, and the standard n=10 table) --------

@pytest.mark.parametrize("successes,trials,low,high", [
    (81, 263, 0.2553, 0.3662),   # Newcombe (1998), Table I
    (5, 10, 0.2366, 0.7634),
    (0, 10, 0.0, 0.2775),
    (10, 10, 0.7225, 1.0),
])
def test_wilson_matches_reference_values(successes, trials, low, high):
    interval = wilson_interval(successes, trials)
    assert interval.low == pytest.approx(low, abs=5e-4)
    assert interval.high == pytest.approx(high, abs=5e-4)


def test_wilson_with_no_trials_is_undefined_not_zero():
    assert wilson_interval(0, 0) is None
    with pytest.raises(ValueError):
        wilson_interval(3, 2)


# ---- Clopper-Pearson one-sided upper bound ---------------------------------------------------

@pytest.mark.parametrize("n", [1, 5, 10, 20, 59, 100])
def test_zero_failures_use_the_exact_closed_form(n):
    assert clopper_pearson_upper(0, n) == 1 - 0.05 ** (1 / n)


def test_zero_failure_reference_values():
    assert clopper_pearson_upper(0, 10) == pytest.approx(0.2589, abs=5e-5)
    assert clopper_pearson_upper(0, 20) == pytest.approx(0.1391, abs=5e-5)
    assert clopper_pearson_upper(0, 59) == pytest.approx(0.0495, abs=5e-5)  # the "n=59" rule: just under 5%


def test_one_failure_in_ten_matches_the_published_bound():
    assert clopper_pearson_upper(1, 10) == pytest.approx(0.3942, abs=5e-5)


@pytest.mark.parametrize("k,n", [(1, 10), (2, 20), (3, 10), (5, 40), (1, 100)])
def test_the_bound_inverts_the_binomial_tail(k, n):
    p = clopper_pearson_upper(k, n)
    tail = sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))
    assert tail == pytest.approx(0.05, abs=1e-9)


def test_clopper_pearson_edges():
    assert clopper_pearson_upper(4, 4) == 1.0
    assert clopper_pearson_upper(0, 0) is None
    with pytest.raises(ValueError):
        clopper_pearson_upper(5, 4)


# ---- Cohen's kappa ---------------------------------------------------------------------------

def test_kappa_reference_example():
    # 50 items: both yes 20, A yes/B no 5, A no/B yes 10, both no 15 -> po 0.7, pe 0.5, kappa 0.4
    a = ["y"] * 20 + ["y"] * 5 + ["n"] * 10 + ["n"] * 15
    b = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
    assert cohens_kappa(a, b).value == pytest.approx(0.4)


def test_kappa_perfect_agreement_chance_agreement_and_disagreement():
    assert cohens_kappa(list("aabb"), list("aabb")).value == pytest.approx(1.0)
    assert cohens_kappa(list("aabb"), list("abab")).value == pytest.approx(0.0)  # chance level
    assert cohens_kappa(list("aabb"), list("bbaa")).value == pytest.approx(-1.0)


def test_kappa_is_explicitly_undefined_not_zero():
    empty = cohens_kappa([], [])
    assert empty.value is None and "no paired" in empty.reason
    single = cohens_kappa(list("aaa"), list("aaa"))
    assert single.value is None and "chance agreement" in single.reason
    with pytest.raises(ValueError):
        cohens_kappa(["a"], ["a", "b"])


# ---- F1 and the paired bootstrap -------------------------------------------------------------

def out(expected, predicted):
    return Outcome(expected, predicted)


def test_f1_counts_missing_predictions_as_misses():
    outcomes = [out(True, True), out(True, None), out(False, False), out(False, True)]
    assert f1_score(outcomes) == pytest.approx(2 / (2 + 1 + 1))  # TP1, FP1, FN1 (the missing one)
    assert f1_score([out(True, None)]) == 0.0                   # defined: a real zero
    assert f1_score([out(False, False), out(False, None)]) is None  # nothing to find, nothing said


def make_outcomes(n, hit_every):
    return {f"c{i}": out(i % 2 == 0, (i % 2 == 0) if i % hit_every else (i % 2 != 0)) for i in range(n)}


def test_bootstrap_is_deterministic_for_a_fixed_seed():
    data = {"a": make_outcomes(40, 5), "b": make_outcomes(40, 3)}
    first, second = paired_bootstrap_f1(data), paired_bootstrap_f1(data)
    assert first == second
    assert first.candidates["a"].resamples == 2000
    assert paired_bootstrap_f1(data, seed=1) != first


def test_bootstrap_interval_brackets_the_point_estimate():
    result = paired_bootstrap_f1({"a": make_outcomes(60, 4)}).candidates["a"]
    assert result.interval.low <= result.point <= result.interval.high
    assert result.defined_resamples == 2000


def test_bootstrap_keeps_cases_aligned_between_candidates():
    data = {"a": make_outcomes(30, 4), "b": make_outcomes(30, 4)}  # identical candidates
    diff = paired_bootstrap_f1(data).differences[("a", "b")]
    assert diff.point == 0.0 and (diff.interval.low, diff.interval.high) == (0.0, 0.0)
    with pytest.raises(ValueError):
        paired_bootstrap_f1({"a": make_outcomes(30, 4), "b": make_outcomes(29, 4)})


def test_bootstrap_degenerate_samples():
    perfect = {f"c{i}": out(True, True) for i in range(10)}
    result = paired_bootstrap_f1({"p": perfect}).candidates["p"]
    assert result.point == 1.0 and (result.interval.low, result.interval.high) == (1.0, 1.0)
    nothing_to_find = {f"c{i}": out(False, False) for i in range(10)}
    undefined = paired_bootstrap_f1({"n": nothing_to_find}).candidates["n"]
    assert undefined.point is None and undefined.interval is None and undefined.defined_resamples == 0
    empty = paired_bootstrap_f1({"e": {}}).candidates["e"]
    assert empty.point is None and empty.interval is None
    all_missed = {f"c{i}": out(True, None) for i in range(10)}
    assert paired_bootstrap_f1({"m": all_missed}).candidates["m"].point == 0.0
