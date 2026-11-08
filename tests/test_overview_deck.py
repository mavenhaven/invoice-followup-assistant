"""scripts/build_overview_deck.py: the deck builds, follows the brief, and its numbers come from the code."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from pptx import Presentation

from core.rules_catalog import DQ_RULES, RULES
from core.schema import INVOICES_EXISTING_COLUMNS, INVOICES_NEW_COLUMNS
from core.taxonomy import Category, Outcome, outcomes_in

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_overview_deck", ROOT / "scripts" / "build_overview_deck.py")
deck_script = importlib.util.module_from_spec(spec)
sys.modules["build_overview_deck"] = deck_script
spec.loader.exec_module(deck_script)


@pytest.fixture(scope="module")
def deck(tmp_path_factory):
    path = deck_script.build(tmp_path_factory.mktemp("deck") / "deck.pptx")
    return Presentation(path)


def all_text(slide) -> str:
    return " ".join(sh.text_frame.text for sh in slide.shapes if sh.has_text_frame)


def test_the_deck_has_the_slides_the_brief_asks_for_in_order(deck):
    titles = [s.shapes.title.text_frame.text for s in deck.slides]
    assert 8 <= len(titles) <= 12 and len(titles) == len(set(titles))
    assert titles[0] == "Invoice Follow-Up Assistant" and titles[-1] == "Next steps"
    wanted = ["chasing", "pieces fit", "sheet holds everything", "daily run", "outcomes", "business rules", "how it is going", "uilt one prompt", "build took", "online"]
    for word in wanted:  # problem, architecture, data model, run flow, taxonomy, rules, dashboard, how it was built, cost, deployment
        assert any(word in t for t in titles), word


def test_every_slide_has_a_real_title_notes_and_stays_inside_the_margins(deck):
    for number, slide in enumerate(deck.slides, 1):
        assert slide.shapes.title is not None and slide.shapes.title.text_frame.text.strip(), number
        assert slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip(), number
        for shape in slide.shapes:
            assert shape.left >= int(0.49 * 914400) and shape.left + shape.width <= deck.slide_width - int(0.49 * 914400), (number, shape.name)


def test_the_numbers_on_the_slides_come_from_the_code(deck):
    titles = {s.shapes.title.text_frame.text: s for s in deck.slides}
    assert f"{len(RULES)} business rules, all tested" in titles
    assert f"{len(Outcome)} possible outcomes" in titles
    data_model = all_text(titles["One sheet holds everything"])
    assert f"{len(INVOICES_EXISTING_COLUMNS)} + {len(INVOICES_NEW_COLUMNS)}" in data_model
    assert f"{len(RULES)} rules and {len(DQ_RULES)} data quality checks" in all_text(titles[f"{len(RULES)} business rules, all tested"])


def test_the_dashboard_slide_shows_the_verified_starting_position(deck):
    slide = next(s for s in deck.slides if "how it is going" in s.shapes.title.text_frame.text)
    chart = next(sh for sh in slide.shapes if sh.has_chart).chart
    values = dict(zip(chart.plots[0].categories, chart.plots[0].series[0].values))
    assert values == {"In progress": 25, "Needing human follow-up": 6, "Data quality failed": 3, "Handled": 1} and sum(values.values()) == 35
    assert "3 + 1 + 6 + 25 = 35" in all_text(slide)


def test_the_taxonomy_slide_lists_every_outcome_once_in_five_groups(deck):
    slide = next(s for s in deck.slides if s.shapes.title.text_frame.text == f"{len(Outcome)} possible outcomes")
    lists = [[p.text for p in sh.text_frame.paragraphs] for sh in slide.shapes if sh.name.startswith("outcomes ")]
    assert len(lists) == len(Category) == 5
    shown = [line for lines in lists for line in lines]
    assert len(shown) == len(Outcome) == len(set(shown))
    assert shown == [deck_script.outcome_label(o) for c in Category for o in outcomes_in(c)]


def test_the_build_slides_state_the_model_cost_and_prompt_pack(deck):
    built = all_text(next(s for s in deck.slides if s.shapes.title.text_frame.text == "Built one prompt at a time"))
    assert "prompt-pack.md" in built and "Streamlit, Google Sheets, Python" in built and "prompt coach" in built
    assert built.count("This deck") == 1 and len([1 for i in range(1, 13) if f"prompt {i}" in " ".join(sh.name for sh in next(s for s in deck.slides if s.shapes.title.text_frame.text == "Built one prompt at a time").shapes)]) == 12
    took = all_text(next(s for s in deck.slides if s.shapes.title.text_frame.text == "What the build took"))
    for fact in ("Sonnet 5.5", "medium thinking effort", "$22.85", "2h 42m", "12,815"):
        assert fact in took


def test_the_live_slide_gives_the_address_and_never_a_password(deck):
    slide = next(s for s in deck.slides if s.shapes.title.text_frame.text == "It is online, ready to test")
    url = next(sh for sh in slide.shapes if sh.name == "app url")
    assert url.text_frame.text.startswith("https://invoice-followup-assistant-") and url.text_frame.text.endswith(".streamlit.app/")
    assert url.text_frame.paragraphs[0].runs[0].hyperlink.address == url.text_frame.text
    text = all_text(slide)
    assert "password" in text and "private repo" in text and "copy of the sheet" in text
