"""T18 Stage B-1 case file (tests/fixtures/t18/stage_b1_cases.json), checked offline."""

import json
from pathlib import Path

CASES = json.loads((Path(__file__).resolve().parent / "fixtures" / "t18" /
                    "stage_b1_cases.json").read_text(encoding="utf-8"))


def test_the_b1_case_file_holds_ten_adjudicated_positives_and_forty_negatives():
    groups = CASES["groups"]
    assert len(groups) == 10 and set(CASES["adjudication"]) == {g["ref"] for g in groups}
    negatives = [n for g in groups for n in g["negatives"]]
    assert len(negatives) == 40 and len({n["id"] for n in negatives}) == 40
    assert all(len(g["negatives"]) == 4 for g in groups)
    assert sum("hard" in n["kind"] for n in negatives) == 14  # 13 reach the judge, N31 does not
