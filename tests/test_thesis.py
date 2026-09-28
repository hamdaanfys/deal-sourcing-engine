import pytest
from conftest import EXAMPLE_THESIS

from dealsource.score.thesis import ThesisError, load_thesis


def test_example_thesis_loads():
    thesis, digest = load_thesis(EXAMPLE_THESIS)
    assert thesis.geography.states == ["AL", "FL", "GA", "NC", "SC", "TN"]
    assert {"3323", "3327", "3339", "3119", "3121"} <= set(thesis.sectors.naics_prefixes)
    assert thesis.size.employees.min == 20 and thesis.size.employees.max == 250
    assert thesis.exclusions.ownership == ["pe_or_strategic_backed", "publicly_traded"]
    assert len(digest) == 64


def test_matches_naics():
    thesis, _ = load_thesis(EXAMPLE_THESIS)
    assert thesis.matches_naics("541330,332710")
    assert thesis.matches_naics(["333999"])
    assert not thesis.matches_naics("541511")
    assert not thesis.matches_naics(None)


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("name: x\nsectors: {naics_prefixes: ['33A']}\ngeography: {states: [GA]}\n", "2-6 digits"),
        (
            "name: x\nsectors: {naics_prefixes: ['3327']}\ngeography: {states: [ZZ]}\n",
            "unknown US state",
        ),
        ("name: x\nsectors: {naics_prefixes: []}\ngeography: {states: [GA]}\n", "naics_prefixes"),
        (
            "name: x\nsectors: {naics_prefixes: ['3327']}\ngeography: {states: [GA]}\nsurprise: 1\n",
            "surprise",
        ),
        ("name: [unclosed\n", "not valid YAML"),
    ],
)
def test_invalid_thesis_errors_name_the_problem(tmp_path, yaml_text, message):
    path = tmp_path / "t.yaml"
    path.write_text(yaml_text)
    with pytest.raises(ThesisError, match=message):
        load_thesis(path)


def test_state_names_are_normalized(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text(
        "name: x\nsectors: {naics_prefixes: ['3327']}\ngeography: {states: [Georgia, nc, GA]}\n"
    )
    thesis, _ = load_thesis(path)
    assert thesis.geography.states == ["GA", "NC"]
