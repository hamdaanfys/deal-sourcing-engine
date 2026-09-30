"""Evaluation against labels (DESIGN.md §11.4-11.5), end to end on synthetic companies."""

import csv
import json

import pytest
import yaml
from synthetic import THESIS, add_company, extraction
from typer.testing import CliRunner

from dealsource import cli
from dealsource.eval.evaluate import (
    EvalRefused,
    load_split_labels,
    match_labels,
    split_for_key,
)
from dealsource.eval.labels import label_key, load_labels

runner = CliRunner()

FIRST = [
    "Kestrel",
    "Copperfield",
    "Juniper",
    "Bluewater",
    "Harborview",
    "Ironbark",
    "Silverpine",
    "Tamarack",
    "Redfern",
    "Ashgrove",
]
SECOND = ["Machining", "Fabrication", "Tooling", "Castings"]
N_DEV, N_TEST = 22, 8


def invoke(*args):
    return runner.invoke(cli.app, list(args))


def companies():
    """40 fictional companies: every third one is a pass with a weaker profile."""
    out = []
    for i, (a, b) in enumerate((a, b) for b in SECOND for a in FIRST):
        out.append(
            {
                "name": f"{a} {b} LLC",
                "domain": f"{a.lower()}{b.lower()}.test",
                "decision": "pass" if i % 3 == 0 else "pursue",
            }
        )
    return out


@pytest.fixture
def setup(settings, conn):
    """Companies in the DB, a labels file (30 labeled), a hand-written split manifest, a thesis."""
    settings.ensure_data_dir()
    cos = companies()
    for i, c in enumerate(cos):
        if c["decision"] == "pursue" or i % 2:
            # Pursue companies, and some passes, look like strong fits.
            add_company(
                conn, c["name"], domain=c["domain"], extraction=extraction(summary="Parts.")
            )
        else:
            add_company(conn, c["name"], domain=c["domain"], naics="541511")
    labeled = cos[: N_DEV + N_TEST]
    rows = [(c["name"], c["domain"], "GA", c["decision"]) for c in labeled]
    # One label without a website: matched by name + state.
    rows[3] = (labeled[3]["name"].replace(" LLC", ", Inc."), "", "GA", labeled[3]["decision"])
    write_labels(settings, rows)
    keys = [label_key(n, w, s) for n, w, s, _ in rows]
    write_manifest(settings, {k: ("dev" if i < N_DEV else "test") for i, k in enumerate(keys)})
    thesis_path = settings.data_dir / "thesis.yaml"
    thesis_path.write_text(yaml.safe_dump(THESIS))
    return {"settings": settings, "thesis": str(thesis_path), "rows": rows, "companies": cos}


def write_labels(settings, rows):
    with settings.labels_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["company_name", "website", "state", "decision"])
        w.writerows(rows)


def write_manifest(settings, assignments):
    settings.split_manifest_path.write_text(
        json.dumps(
            {"version": 1, "seed": 20260927, "test_fraction": 0.3, "assignments": assignments}
        )
    )


def add_unmatched_dev_labels(setup, n):
    """Fictional companies that are not in the database, assigned to dev."""
    s = setup["settings"]
    extra = [(f"Quillmoor Dynamics {i}", f"quillmoor{i}.test", "GA", "pursue") for i in range(n)]
    rows = setup["rows"] + extra
    write_labels(s, rows)
    manifest = json.loads(s.split_manifest_path.read_text())
    for name, web, st, _ in extra:
        manifest["assignments"][label_key(name, web, st)] = "dev"
    s.split_manifest_path.write_text(json.dumps(manifest))


def fixture_strings(setup):
    names = [c["name"] for c in setup["companies"]] + [r[0] for r in setup["rows"]]
    words = [c["name"].split()[0] for c in setup["companies"]]
    domains = [c["domain"] for c in setup["companies"]]
    return names + words + domains + ["Quillmoor", "quillmoor"]


# --- dev evaluation -------------------------------------------------------------------------------


def test_dev_eval_prints_aggregates_only_and_writes_the_report(setup):
    assert invoke("score", "--thesis", setup["thesis"]).exit_code == 0
    r = invoke("eval", "--thesis", setup["thesis"])
    assert r.exit_code == 0, r.output
    assert "split: dev" in r.output
    assert f"labeled companies: {N_DEV}; matched to scored companies: {N_DEV} (100.0%)" in r.output
    assert "UNMATCHED: 0" in r.output
    for needle in ("average precision", "ROC AUC", "precision@10", "recall@10", "TP=", "F1"):
        assert needle in r.output
    for s in fixture_strings(setup):
        assert s not in r.output, s
    report = next((setup["settings"].evals_dir).glob("*_dev.csv"))
    text = report.read_text()
    assert text.startswith("# split: dev")
    rows = list(csv.DictReader(line for line in text.splitlines() if not line.startswith("#")))
    assert len(rows) == N_DEV and {r["decision"] for r in rows} == {"pursue", "pass"}
    assert {r["matched"] for r in rows} == {"domain", "name_state"}
    # Disagreements sort first.
    flags = [r["disagreement"] != "" for r in rows]
    assert flags == sorted(flags, reverse=True)


def test_eval_records_a_run_and_never_reads_test_labels(setup):
    invoke("score", "--thesis", setup["thesis"])
    assert invoke("eval", "--thesis", setup["thesis"]).exit_code == 0
    import sqlite3

    conn = sqlite3.connect(setup["settings"].db_path)
    split, metrics = conn.execute("SELECT split, metrics_json FROM eval_runs").fetchone()
    assert split == "dev" and json.loads(metrics)["labeled"] == N_DEV
    # The report holds dev keys only.
    manifest = json.loads(setup["settings"].split_manifest_path.read_text())
    report = next(setup["settings"].evals_dir.glob("*_dev.csv")).read_text()
    for key, sp in manifest["assignments"].items():
        assert (key in report) is (sp == "dev")


def test_unmatched_within_five_percent_are_left_out_and_reported(setup):
    add_unmatched_dev_labels(setup, 1)  # 1 of 23 = 4.3%
    invoke("score", "--thesis", setup["thesis"])
    r = invoke("eval", "--thesis", setup["thesis"])
    assert r.exit_code == 0, r.output
    assert f"labeled companies: {N_DEV + 1}; matched to scored companies: {N_DEV}" in r.output
    assert "UNMATCHED: 1 (1 pursue, 0 pass) - left out of every metric below" in r.output
    assert f"metrics on {N_DEV} matched companies" in r.output
    report = next(setup["settings"].evals_dir.glob("*_dev.csv")).read_text()
    assert "unmatched" in report


def test_more_than_five_percent_unmatched_refuses(setup):
    add_unmatched_dev_labels(setup, 2)  # 2 of 24 = 8.3%
    invoke("score", "--thesis", setup["thesis"])
    r = invoke("eval", "--thesis", setup["thesis"])
    assert r.exit_code == 1
    assert "2 of 24 labeled companies in the dev split (8.3%) match no scored company" in r.output
    assert "the limit is 5%" in r.output and "No metrics were computed" in r.output
    assert "Quillmoor" not in r.output
    detail = setup["settings"].evals_dir / "unmatched_dev.csv"
    assert detail.read_text().count("Quillmoor") == 2
    reports = [p for p in setup["settings"].evals_dir.glob("*_dev.csv") if p != detail]
    assert reports == []  # no company-level report when nothing was evaluated


def test_eval_refuses_without_scores(setup):
    r = invoke("eval", "--thesis", setup["thesis"])
    assert r.exit_code == 1 and "No scores for this thesis" in r.output


# --- the held-out test set ----------------------------------------------------------------------


def test_test_set_needs_final(setup):
    invoke("score", "--thesis", setup["thesis"])
    r = invoke("eval", "--thesis", setup["thesis"], "--set", "test")
    assert r.exit_code == 2 and "--final" in r.output
    assert not setup["settings"].test_eval_log_path.exists()
    r = invoke("eval", "--thesis", setup["thesis"], "--final")
    assert r.exit_code != 0 and "only applies to --set test" in r.output


def test_test_set_runs_once_then_reprints(setup):
    invoke("score", "--thesis", setup["thesis"])
    first = invoke("eval", "--thesis", setup["thesis"], "--set", "test", "--final")
    assert first.exit_code == 0, first.output
    assert "split: test" in first.output and f"labeled companies: {N_TEST}" in first.output
    log = setup["settings"].test_eval_log_path
    assert len(log.read_text().splitlines()) == 1
    second = invoke("eval", "--thesis", setup["thesis"], "--set", "test", "--final")
    assert second.exit_code == 1 and "already evaluated" in second.output
    assert f"labeled companies: {N_TEST}" in second.output  # the recorded metrics, reprinted
    assert len(log.read_text().splitlines()) == 1
    for s in fixture_strings(setup):
        assert s not in first.output and s not in second.output


# --- units --------------------------------------------------------------------------------------


def test_load_split_labels_requires_a_valid_split(setup):
    s = setup["settings"]
    manifest = json.loads(s.split_manifest_path.read_text())
    with pytest.raises(ValueError):
        load_split_labels(s.labels_path, manifest, "all", s.evals_dir / "c.csv")
    dev = load_split_labels(s.labels_path, manifest, "dev", s.evals_dir / "c.csv")
    assert len(dev) == N_DEV


def test_conflicting_labels_refuse(setup):
    s = setup["settings"]
    rows = setup["rows"] + [
        (
            setup["rows"][0][0],
            setup["rows"][0][1],
            "GA",
            "pass" if setup["rows"][0][3] == "pursue" else "pursue",
        )
    ]
    write_labels(s, rows)
    manifest = json.loads(s.split_manifest_path.read_text())
    with pytest.raises(EvalRefused, match="conflicting decisions"):
        load_split_labels(s.labels_path, manifest, "dev", s.evals_dir / "c.csv")


def test_keys_added_after_the_split_use_the_stable_hash_rule():
    manifest = {"seed": 20260927, "test_fraction": 0.3, "assignments": {"d:a.test": "test"}}
    assert split_for_key("d:a.test", manifest) == "test"
    keys = [f"d:new{i}.test" for i in range(400)]
    first = [split_for_key(k, manifest) for k in keys]
    assert first == [split_for_key(k, manifest) for k in keys]
    assert 0.2 < first.count("test") / len(keys) < 0.4


def test_name_match_needs_state_and_a_unique_best(settings, conn, tmp_path):
    from dealsource.eval.evaluate import Scored

    def scored(cid, name, state, domain=None):
        from dealsource.resolve.normalize import name_key

        return Scored(cid, name_key(name), name, domain, state, 50.0, False, None, "low", "{}", "r")

    cos = [
        scored(1, "Ironbark Tooling LLC", "GA"),
        scored(2, "Ironbark Tooling Inc", "NC"),
        scored(3, "Redfern Castings", "GA"),
        scored(4, "Redfern Castings Co", "GA"),  # same name key: a tie
    ]
    path = tmp_path / "labels.csv"
    path.write_text(
        "company_name,website,state,decision\n"
        "Ironbark Tooling,,GA,pursue\n"
        "Ironbark Tooling,,,pursue\n"
        "Redfern Castings,,GA,pass\n"
    )
    rows = load_labels(path)
    groups = {r.key: [r] for r in rows}
    got = {
        k: (s.company_id if s else None, how) for k, (s, how) in match_labels(groups, cos).items()
    }
    assert sorted(got.values(), key=str) == sorted(
        [(1, "name_state"), (None, "unmatched"), (None, "unmatched")], key=str
    )
