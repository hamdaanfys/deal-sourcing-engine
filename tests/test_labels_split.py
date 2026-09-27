"""Labels loading and the one-time dev/test split. Synthetic labels only."""

import csv
import json
import os
import random
import stat

import pytest
from conftest import FIXTURES
from typer.testing import CliRunner

from dealsource import cli, db
from dealsource.eval.labels import PASS, PURSUE, LabelsError, group_labels, label_key, load_labels
from dealsource.eval.split import (
    DEV,
    TEST,
    SplitRefused,
    assign,
    make_split,
    n_test_for,
)

LABELS = FIXTURES / "labels.csv"
runner = CliRunner()

# Every identifying string in the fixture: none may ever appear in terminal output.
with LABELS.open() as _f:
    _rows = list(csv.DictReader(_f))
FIXTURE_NAMES = [r["Company Name"] for r in _rows]
FIXTURE_SITES = [r["Website"] for r in _rows if r["Website"]]
FIXTURE_EMAILS = [r["Contact Email"] for r in _rows if r["Contact Email"]]


def install_labels(settings, source=LABELS):
    settings.ensure_data_dir()
    settings.labels_path.write_bytes(source.read_bytes())


def split(settings, **kw):
    return make_split(
        labels_path=settings.labels_path,
        manifest_path=settings.split_manifest_path,
        db_path=settings.db_path,
        conflicts_path=settings.data_dir / "evals" / "label_conflicts.csv",
        **kw,
    )


# --- Loading and grouping ----------------------------------------------------------------


def test_load_labels_maps_columns_and_decisions():
    rows = load_labels(LABELS)
    assert len(rows) == 26
    assert {r.decision for r in rows} == {PURSUE, PASS}
    assert rows[0].key == "d:acme-mfg.test"


def test_duplicate_rows_for_one_company_form_one_group():
    groups = group_labels(load_labels(LABELS))
    assert len(groups.decisions) == 24 and not groups.conflicts
    assert len(groups.rows_per_key["d:acme-mfg.test"]) == 2  # www. and path normalize away
    # A Facebook page is not a website, so it groups with the same company's name-only row.
    assert len(groups.rows_per_key["n:oakmont valve service|tn"]) == 2


def test_label_key_prefers_domain_then_name_and_state():
    assert label_key("Acme Mfg. LLC", "https://www.acme-mfg.test", "GA") == "d:acme-mfg.test"
    assert label_key("Acme Mfg. LLC", None, "Georgia") == "n:acme manufacturing|ga"
    assert label_key("Acme Mfg. LLC", "facebook.com/acme", None) == "n:acme manufacturing|"


def test_contact_columns_are_never_loaded():
    rows = load_labels(LABELS)
    blob = repr(rows)
    assert not any(email in blob for email in FIXTURE_EMAILS)
    assert "Strong fit" not in blob  # notes are not loaded either


def test_invalid_decisions_are_reported_by_row_number_only(tmp_path):
    path = tmp_path / "labels.csv"
    path.write_text("company_name,decision\nZephyr Secret Holdings,maybe\nQuill Industries,1\n")
    with pytest.raises(LabelsError) as exc:
        load_labels(path)
    assert "rows 1" in str(exc.value)
    assert "Zephyr" not in str(exc.value)


def test_missing_required_column_and_contact_mapping_are_errors(tmp_path):
    path = tmp_path / "labels.csv"
    path.write_text("company_name,email\nQuill Industries,q@quill.test\n")
    with pytest.raises(LabelsError, match="decision"):
        load_labels(path)
    with pytest.raises(LabelsError, match="contact"):
        load_labels(path, column_map={"decision": "email"})


# --- The split itself --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "k"), [(0, 0), (1, 0), (2, 1), (3, 1), (4, 1), (5, 2), (10, 3), (14, 4), (100, 30)]
)
def test_n_test_rounds_half_up_and_keeps_both_sides(n, k):
    assert n_test_for(n, 0.3) == k


def test_split_is_stratified_by_decision(settings):
    install_labels(settings)
    result = split(settings)
    # 9 pursue and 15 pass companies: 0.3 x 9 = 2.7 -> 3 and 0.3 x 15 = 4.5 -> 5 (half up) go to test.
    assert result.counts == {DEV: {PURSUE: 6, PASS: 10}, TEST: {PURSUE: 3, PASS: 5}}
    assert (result.rows, result.companies) == (26, 24)


def test_same_seed_same_split_regardless_of_row_order(tmp_path):
    rows = load_labels(LABELS)
    decisions = group_labels(rows).decisions
    shuffled = list(decisions.items())
    random.Random(1).shuffle(shuffled)
    assert assign(decisions, seed=20260927, test_fraction=0.3) == assign(
        dict(shuffled), seed=20260927, test_fraction=0.3
    )
    assert assign(decisions, seed=20260927, test_fraction=0.3) != assign(
        decisions, seed=7, test_fraction=0.3
    )


def test_row_order_in_the_file_does_not_matter(settings, tmp_path):
    lines = LABELS.read_text().splitlines()
    header, body = lines[0], lines[1:]
    random.Random(3).shuffle(body)
    shuffled = tmp_path / "shuffled.csv"
    shuffled.write_text("\n".join([header, *body]) + "\n")
    install_labels(settings)
    split(settings)
    original = json.loads(settings.split_manifest_path.read_text())["assignments"]

    other = settings.data_dir.parent / "other"
    from dealsource.config import Settings

    s2 = Settings(data_dir=other)
    install_labels(s2, shuffled)
    split(s2)
    assert json.loads(s2.split_manifest_path.read_text())["assignments"] == original


def test_manifest_contents_and_read_only(settings):
    install_labels(settings)
    split(settings)
    path = settings.split_manifest_path
    m = json.loads(path.read_text())
    assert m["seed"] == 20260927 and m["test_fraction"] == 0.3
    assert m["stratified_by"] == "decision" and m["db_had_runs"] is False
    assert len(m["assignments"]) == len(m["decisions"]) == 24
    assert set(m["assignments"].values()) == {DEV, TEST}
    import hashlib

    assert m["labels_sha256"] == hashlib.sha256(LABELS.read_bytes()).hexdigest()
    assert not os.stat(path).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)


def test_second_run_is_refused_and_manifest_unchanged(settings):
    install_labels(settings)
    split(settings)
    before = settings.split_manifest_path.read_bytes()
    with pytest.raises(SplitRefused, match="already exists"):
        split(settings, seed=1)
    assert settings.split_manifest_path.read_bytes() == before


def test_refuses_without_labels_file_and_writes_nothing(settings):
    with pytest.raises(SplitRefused, match="No labels file"):
        split(settings)
    assert not settings.split_manifest_path.exists()
    assert not settings.db_path.exists()


def test_refuses_when_pipeline_has_already_run(settings):
    install_labels(settings)
    conn = db.connect(settings.db_path)
    with db.record_run(conn, "resolve", {}):
        pass
    conn.close()
    with pytest.raises(SplitRefused, match="pipeline runs"):
        split(settings)
    assert not settings.split_manifest_path.exists()


def test_empty_database_does_not_block_the_split(settings):
    install_labels(settings)
    db.connect(settings.db_path).close()
    split(settings)
    assert settings.split_manifest_path.exists()


def test_conflicting_decisions_refuse_and_write_details_privately(settings):
    settings.ensure_data_dir()
    settings.labels_path.write_text(
        "company_name,website,decision\nZephyr Secret Holdings,zephyr.test,1\nZephyr Secret Holdings Inc,www.zephyr.test,0\nQuill Industries,quill.test,1\n"
    )
    with pytest.raises(SplitRefused) as exc:
        split(settings)
    assert "1 company(ies) have conflicting decisions" in str(exc.value)
    assert "Zephyr" not in str(exc.value)
    assert not settings.split_manifest_path.exists()
    details = (settings.data_dir / "evals" / "label_conflicts.csv").read_text()
    assert "Zephyr Secret Holdings" in details  # the analyst's private review file


@pytest.mark.parametrize("fraction", [0, 1, 1.5, -0.1])
def test_invalid_test_fraction(settings, fraction):
    install_labels(settings)
    with pytest.raises(ValueError):
        split(settings, test_fraction=fraction)


# --- CLI ------------------------------------------------------------------------------


def assert_no_company_data(output):
    for s in FIXTURE_NAMES + FIXTURE_SITES + FIXTURE_EMAILS:
        assert s not in output, f"company-level data leaked: {s!r}"
    assert "d:" not in output and "n:" not in output  # no label keys


def test_cli_split_prints_counts_only(settings):
    install_labels(settings)
    result = runner.invoke(cli.app, ["labels", "split"])
    assert result.exit_code == 0, result.output
    assert "26 label rows -> 24 companies" in result.output
    lines = [line.split() for line in result.output.splitlines() if line.strip()]
    assert ["total", "24", "companies", "pursue", "9", "pass", "15"] in lines
    assert ["dev", "16", "companies", "pursue", "6", "pass", "10"] in lines
    assert ["test", "8", "companies", "pursue", "3", "pass", "5"] in lines
    assert_no_company_data(result.output)


def test_cli_second_run_refuses_with_counts_only(settings):
    install_labels(settings)
    runner.invoke(cli.app, ["labels", "split"])
    result = runner.invoke(cli.app, ["labels", "split"])
    assert result.exit_code == 1
    assert "already exists" in result.output and "never redone" in result.output
    assert "pursue" in result.output
    assert_no_company_data(result.output)


def test_cli_without_labels_file(settings):
    result = runner.invoke(cli.app, ["labels", "split"])
    assert result.exit_code == 1
    assert "No labels file" in result.output
    assert not settings.split_manifest_path.exists()


def test_cli_conflicts_print_count_only(settings):
    settings.ensure_data_dir()
    settings.labels_path.write_text(
        "company_name,website,decision\nZephyr Secret Holdings,zephyr.test,1\nZephyr Holdings,zephyr.test,0\n"
    )
    result = runner.invoke(cli.app, ["labels", "split"])
    assert result.exit_code == 1
    assert "1 company(ies) have conflicting decisions" in result.output
    assert "Zephyr" not in result.output


def test_split_unlocks_the_pipeline(settings):
    assert runner.invoke(cli.app, ["resolve"]).exit_code == 2
    install_labels(settings)
    assert runner.invoke(cli.app, ["labels", "split"]).exit_code == 0
    result = runner.invoke(cli.app, ["ingest", "csv", str(FIXTURES / "companies_messy.csv")])
    assert result.exit_code == 0, result.output
