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
        (
            "name: x\nsectors: {naics_prefixes: ['33A']}\ngeography: {states: [GA]}\n",
            "sectors.naics_prefixes: naics_prefix_format",
        ),
        (
            "name: x\nsectors: {naics_prefixes: ['3327']}\ngeography: {states: [ZZ]}\n",
            "geography.states: unknown_state",
        ),
        (
            "name: x\nsectors: {naics_prefixes: []}\ngeography: {states: [GA]}\n",
            "sectors.naics_prefixes: too_short",
        ),
        (
            "name: x\nsectors: {naics_prefixes: ['3327']}\ngeography: {states: [GA]}\nsurprise: 1\n",
            "<key>: unknown_field",
        ),
        ("name: [unclosed\n", "not valid YAML"),
    ],
)
def test_invalid_thesis_errors_name_the_problem(tmp_path, yaml_text, message):
    path = tmp_path / "t.yaml"
    path.write_text(yaml_text)
    with pytest.raises(ThesisError, match=message) as err:
        load_thesis(path)
    for value in ("33A", "ZZ", "surprise", "unclosed"):  # values from the file never appear
        assert value not in str(err.value) and value not in " ".join(err.value.problems)


def test_state_names_are_normalized(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text(
        "name: x\nsectors: {naics_prefixes: ['3327']}\ngeography: {states: [Georgia, nc, GA]}\n"
    )
    thesis, _ = load_thesis(path)
    assert thesis.geography.states == ["GA", "NC"]


# --- dealsource thesis check --------------------------------------------------------------------


def check(path):
    from typer.testing import CliRunner

    from dealsource import cli

    return CliRunner().invoke(cli.app, ["thesis", "check", "--thesis", str(path)])


def test_check_passes_a_valid_thesis():
    r = check(EXAMPLE_THESIS)
    assert r.exit_code == 0 and r.output == "PASS\n"


def test_check_lists_broken_rules_without_values(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text(
        "name: Confidential Fund IV\n"
        "sectors: {naics_prefixes: ['3327', 'X99'], include_keywords: [secret widget]}\n"
        "geography: {states: [GA, Atlantis]}\n"
        "ownership: {prefer: [family_run]}\n"
        "weights: {sector: 1, synergy: 2}\n"
        "shortlist_threshold: 250\n"
        "codename: bluebird\n"
    )
    r = check(path)
    assert r.exit_code == 1
    assert r.output.splitlines() == [
        "FAIL: 6 rule(s) broken",
        "  sectors.naics_prefixes: naics_prefix_format",
        "  geography.states: unknown_state",
        "  ownership.prefer: unknown_ownership_signal",
        "  weights: unknown_weight",
        "  shortlist_threshold: less_than_equal",
        "  <key>: unknown_field",
    ]
    for value in (
        "Confidential",
        "X99",
        "secret",
        "Atlantis",
        "family_run",
        "synergy",
        "250",
        "codename",
        "bluebird",
    ):
        assert value not in r.output


def test_check_bad_weight_value_hides_the_key(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text(
        "name: x\nsectors: {naics_prefixes: ['3327']}\ngeography: {states: [GA]}\n"
        "weights: {sector: lots}\n"
    )
    r = check(path)
    assert (
        r.exit_code == 1 and "  weights.<key>: float_parsing" in r.output and "lots" not in r.output
    )


def test_check_missing_and_unparsable_files(tmp_path):
    r = check(tmp_path / "nope.yaml")
    assert r.exit_code == 1 and "(file): not_found" in r.output
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: [unclosed\n")
    r = check(bad)
    assert r.exit_code == 1 and "(file): invalid_yaml" in r.output and "unclosed" not in r.output
