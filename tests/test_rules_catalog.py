"""The About page's content cannot drift: every rule must name code and tests that really exist."""
from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

from core import rules_catalog as cat
from core.schema import INVOICES_COLUMNS, Config
from core.taxonomy import Outcome

ROOT = Path(__file__).resolve().parent.parent


def defined_tests(path: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}


def resolve(dotted: str):
    parts = dotted.split(".")
    for i in range(len(parts) - 1, 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except ModuleNotFoundError:
            continue
        for name in parts[i:]:
            obj = getattr(obj, name)
        return obj
    raise AssertionError(f"cannot import {dotted}")


# ---------------------------------------------------------------- business rules


def test_rule_ids_are_unique_and_run_without_gaps():
    ids = [r.id for r in cat.RULES]
    assert ids == [f"BR-{n:02d}" for n in range(1, len(ids) + 1)] and len(ids) == 27


def test_every_rule_is_fully_described():
    for r in cat.RULES:
        assert r.title.strip() and len(r.text) > 40 and r.example.strip(), r.id
        assert r.code and r.tests, f"{r.id} must name its code and its tests"
        assert len(set(r.tests)) == len(r.tests) and len(set(r.code)) == len(r.code), r.id


@pytest.mark.parametrize("rule", cat.RULES, ids=[r.id for r in cat.RULES])
def test_the_code_a_rule_names_exists(rule):
    for dotted in rule.code:
        assert resolve(dotted) is not None, f"{rule.id}: {dotted}"


@pytest.mark.parametrize("rule", cat.RULES, ids=[r.id for r in cat.RULES])
def test_the_tests_a_rule_names_exist(rule):
    for ref in rule.tests:
        path, _, name = ref.partition("::")
        assert (ROOT / path).exists(), f"{rule.id}: {path} is missing"
        assert name in defined_tests(path), f"{rule.id}: {path} has no test called {name}"


def test_every_test_file_a_rule_uses_is_a_real_test_module():
    files = {ref.partition("::")[0] for r in cat.RULES for ref in r.tests}
    assert files and all(f.startswith("tests/test_") and f.endswith(".py") for f in files)


def test_settings_in_the_text_follow_the_config_not_the_typed_numbers():
    default, custom = Config(), Config(attempt_cap=7, promise_max_days=9, max_approvals=2, default_retry_days=4, system_error_limit=6)
    by_id = {r.id: r for r in cat.RULES}
    assert "7 attempts" in by_id["BR-03"].rendered_title(custom) and "7 on phone" in by_id["BR-03"].rendered(custom)
    assert "9 days" in by_id["BR-09"].rendered(custom) and "9 days" in by_id["BR-09"].rendered_title(custom)
    assert "at most 2 extensions" in by_id["BR-11"].rendered(custom)
    assert "4 days later" in by_id["BR-12"].rendered(custom) and "6 in a row" in by_id["BR-14"].rendered(custom)
    for r in cat.RULES:  # nothing is left unfilled, with either config
        for c in (default, custom):
            assert "{" not in r.rendered(c) + r.rendered_title(c) + r.rendered_example(c), r.id
    assert "5 attempts" in by_id["BR-03"].rendered_title(default)


def test_rule_ids_match_the_design_document():
    design = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    in_design = set(re.findall(r"\| (BR-\d\d) \|", design))
    assert in_design == {r.id for r in cat.RULES}


# ---------------------------------------------------------------- data quality rules


def test_dq_rules_are_complete_and_implemented():
    assert [r.id for r in cat.DQ_RULES] == [f"DQ-{n:02d}" for n in range(1, 19)]
    source = (ROOT / "core" / "quality.py").read_text(encoding="utf-8")
    for r in cat.DQ_RULES:
        assert f'"{r.id}"' in source, f"{r.id} is not raised anywhere in core/quality.py"
        assert r.severity in ("BLOCK", "WARN", "INFO") and r.text.strip() and r.tests
        for ref in r.tests:
            path, _, name = ref.partition("::")
            assert name in defined_tests(path), f"{r.id}: no test {name}"
    raised = set(re.findall(r'"(DQ-\d\d)"', source))
    assert raised == {r.id for r in cat.DQ_RULES}  # and nothing in the code is missing from the page


# ---------------------------------------------------------------- columns, settings, assumptions


def test_every_column_is_explained_and_nothing_extra_is():
    assert set(cat.COLUMN_DOCS) == set(INVOICES_COLUMNS)
    assert all(text.strip() for text in cat.COLUMN_DOCS.values())


def test_every_setting_is_explained():
    keys = {k for k in Config.default_rows() if not k.startswith("min_amount_")}
    assert set(cat.SETTING_DOCS) == keys


def test_assumptions_and_limitations_are_well_formed():
    ids = [a[0] for a in cat.ASSUMPTIONS]
    assert ids == [f"A-{n:02d}" for n in range(1, len(ids) + 1)]
    assert {a[1] for a in cat.ASSUMPTIONS} <= {"Confirmed", "Assumed", "Placeholder", "Recommended"}
    assert all(a[2].strip() for a in cat.ASSUMPTIONS) and len(cat.LIMITATIONS) >= 8 and all(x.strip() for x in cat.LIMITATIONS)
    design = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert set(re.findall(r"\| (A-\d\d) \|", design)) <= set(ids)  # every assumption in the spec is on the page


def test_diagrams_are_valid_looking_dot():
    for dot in (cat.ARCHITECTURE_DOT, cat.LIFECYCLE_DOT):
        assert dot.strip().startswith("digraph") and dot.count("{") == dot.count("}") and "->" in dot
    for status in ("In progress", "Needing human", "Data quality", "Handled"):
        assert status in cat.LIFECYCLE_DOT


def test_the_lifecycle_groups_match_the_status_table():
    assert [g for g, _ in cat.STATUS_TABLE] == ["In progress", "Needing human follow-up", "Data quality failed", "Handled"]
    from core.metrics import BUCKET_LABELS
    assert [BUCKET_LABELS[k] for k in ("in_progress", "needs_human", "dq_failed", "handled")] == [g for g, _ in cat.STATUS_TABLE]


def test_pages_listed_match_the_pages_that_exist():
    files = sorted(p.stem.split("_", 1)[1].replace("_", " ") for p in (ROOT / "pages").glob("*.py"))
    assert sorted(name for name, _ in cat.PAGES) == files


def test_every_outcome_is_in_the_taxonomy_the_page_shows():
    from core.taxonomy import taxonomy_table
    assert [r["code"] for r in taxonomy_table()] == [o.value for o in Outcome]
