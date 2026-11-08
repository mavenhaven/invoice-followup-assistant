"""Tests for core/taxonomy.py and core/mock_responses.py."""
from __future__ import annotations

import collections
import datetime as dt

import pytest

from core.mock_responses import (
    ContactInfo, InvoiceInfo, fmt_date, generatable_outcomes, generate_response, make_rng,
)
from core.schema import Channel, HumanReason, Status
from core.taxonomy import (
    Category, Outcome, category_of, counts_as_attempt, extract_promised_date, info, is_channel_error,
    is_real_response, is_system_error, outcomes_for_channel, outcomes_in, parse_outcome, promised_days,
    taxonomy_table,
)

TODAY = dt.date(2026, 8, 20)
CONTACT = ContactInfo(name="Thomas Berger", customer="Veltner Gastro GmbH", email="t.berger@veltner-gastro.at",
                      phone="+43 662 771 3345", country="AT")
INVOICE = InvoiceInfo("INV-2026-4390", 1150.0, "EUR", dt.date(2026, 7, 29))
EXISTING_SHEET_OUTCOMES = ["CALLBACK_REQUESTED", "DISPUTE", "NO_ANSWER", "OUT_OF_OFFICE", "PAID",
                           "PARTIAL_PAYMENT", "PROMISE_TO_PAY", "SETTLEMENT_REQUESTED", "WRONG_CONTACT"]


# ---------------------------------------------------------------- taxonomy


def test_every_outcome_has_info_and_category_counts_match_the_design():
    assert len(Outcome) == 26
    sizes = {c: len(outcomes_in(c)) for c in Category}
    assert sizes == {Category.CONTACT_MADE: 10, Category.NO_CONTACT: 4, Category.CHANNEL_ERROR: 6,
                     Category.SYSTEM_ERROR: 3, Category.ESCALATION: 3}


def test_real_response_is_exactly_the_contact_made_category():
    for o in Outcome:
        assert is_real_response(o) == (category_of(o) is Category.CONTACT_MADE)


def test_system_errors_do_not_use_an_attempt_but_channel_errors_do():
    for o in outcomes_in(Category.SYSTEM_ERROR):
        assert is_system_error(o) and not counts_as_attempt(o)
    for o in outcomes_in(Category.CHANNEL_ERROR):
        assert is_channel_error(o) and counts_as_attempt(o)
    for o in outcomes_in(Category.ESCALATION):
        assert not counts_as_attempt(o) and not info(o).channels


def test_channels_are_disabled_by_the_right_codes():
    disabling = {o: info(o).disables_channels for o in Outcome if info(o).disables_channels}
    assert disabling == {
        Outcome.EMAIL_BOUNCE_HARD: frozenset({Channel.EMAIL}),
        Outcome.EMAIL_INVALID_ADDRESS: frozenset({Channel.EMAIL}),
        Outcome.PHONE_INVALID_NUMBER: frozenset({Channel.PHONE}),
        Outcome.PHONE_DISCONNECTED: frozenset({Channel.PHONE}),
        Outcome.WRONG_CONTACT: frozenset({Channel.EMAIL, Channel.PHONE}),
    }


def test_escalating_outcomes_set_needs_human_with_a_reason():
    for o, reason in [(Outcome.DISPUTE, HumanReason.DISPUTE), (Outcome.SETTLEMENT_REQUESTED, HumanReason.SETTLEMENT),
                      (Outcome.WRONG_CONTACT, HumanReason.WRONG_CONTACT), (Outcome.REFUSED_TO_PAY, HumanReason.REFUSED)]:
        assert info(o).status_effect is Status.NEEDS_HUMAN and info(o).human_reason is reason
    assert info(Outcome.PAID).status_effect is Status.HANDLED
    assert info(Outcome.DQ_FAILED).status_effect is Status.DQ_FAILED


def test_out_of_office_is_exempt_from_approval_and_is_not_a_real_response():
    assert info(Outcome.OUT_OF_OFFICE).exempt_from_approval
    assert not is_real_response(Outcome.OUT_OF_OFFICE)
    assert [o.value for o in Outcome if info(o).exempt_from_approval] == ["OUT_OF_OFFICE"]


def test_channel_rules():
    assert Outcome.NO_RESPONSE in outcomes_for_channel("email") and Outcome.NO_RESPONSE not in outcomes_for_channel("phone")
    assert Outcome.NO_ANSWER in outcomes_for_channel(Channel.PHONE) and Outcome.NO_ANSWER not in outcomes_for_channel(Channel.EMAIL)
    assert all(o not in outcomes_for_channel("email") + outcomes_for_channel("phone") for o in outcomes_in(Category.ESCALATION))


def test_all_existing_sheet_outcomes_parse():
    for code in EXISTING_SHEET_OUTCOMES:
        assert parse_outcome(code) is Outcome(code)
    assert parse_outcome("") is None and parse_outcome(None) is None and parse_outcome("  ") is None
    assert parse_outcome(" promise to pay ") is Outcome.PROMISE_TO_PAY
    with pytest.raises(ValueError, match="Unknown outcome"):
        parse_outcome("MAYBE_LATER")
    assert parse_outcome("MAYBE_LATER", strict=False) is None


def test_taxonomy_table_lists_every_outcome():
    rows = taxonomy_table()
    assert [r["code"] for r in rows] == [o.value for o in Outcome]
    assert {"code", "category", "description", "channels", "counts_as_attempt", "real_response", "next_action"} <= set(rows[0])


# ---------------------------------------------------------------- stated dates


@pytest.mark.parametrize("text, ref, expected", [
    ("Promised payment by 30 June. Not received.", dt.date(2026, 6, 15), dt.date(2026, 6, 30)),
    ("Confirmed payment run on 31 Aug 2026.", dt.date(2026, 8, 18), dt.date(2026, 8, 31)),
    ("Asked to be called back after 15 Aug.", dt.date(2026, 7, 9), dt.date(2026, 8, 15)),
    ("Auto-reply: back 1 Sep 2026.", dt.date(2026, 8, 17), dt.date(2026, 9, 1)),
    ("I will pay on Sep 3rd, 2026", dt.date(2026, 8, 1), dt.date(2026, 9, 3)),
    ("payment date is 2026-09-12", dt.date(2026, 8, 1), dt.date(2026, 9, 12)),
    ("2 January", dt.date(2026, 12, 30), dt.date(2027, 1, 2)),  # rolls into next year
    ("It will arrive in 7 days", dt.date(2026, 8, 1), dt.date(2026, 8, 8)),
    ("we can pay in two weeks", dt.date(2026, 8, 1), dt.date(2026, 8, 15)),
    ("tomorrow", dt.date(2026, 8, 1), dt.date(2026, 8, 2)),
    ("next week", dt.date(2026, 8, 1), dt.date(2026, 8, 8)),
])
def test_extract_promised_date(text, ref, expected):
    assert extract_promised_date(text, ref) == expected


@pytest.mark.parametrize("text", ["", None, "No date here", "30 February 2026", "40 market stalls", "Five attempts since Feb, no contact."])
def test_extract_promised_date_returns_none_when_there_is_no_date(text):
    assert extract_promised_date(text, dt.date(2026, 8, 1)) is None


def test_promised_days():
    assert promised_days("pay on 31 Aug 2026", dt.date(2026, 8, 18)) == 13
    assert promised_days("nothing", dt.date(2026, 8, 18)) is None


# ---------------------------------------------------------------- mock responses


def _draws(channel: str, n: int = 5000, seed: int = 7) -> list:
    rng = make_rng(seed)
    return [generate_response(channel, CONTACT, INVOICE, rng, today=TODAY) for _ in range(n)]


@pytest.mark.parametrize("channel", ["email", "phone"])
def test_every_producible_outcome_appears_in_5000_draws(channel):
    seen = {r.outcome for r in _draws(channel)}
    assert seen == set(generatable_outcomes(channel))


def test_every_outcome_in_the_taxonomy_is_produced_by_some_channel_except_escalations():
    produced = {r.outcome for ch in ("email", "phone") for r in _draws(ch)}
    assert produced == {o for o in Outcome if info(o).channels}
    assert {o for o in Outcome} - produced == set(outcomes_in(Category.ESCALATION))


def test_same_seed_gives_same_responses_and_different_seeds_differ():
    a = [r.text for r in _draws("email", 50, seed=1)]
    assert a == [r.text for r in _draws("email", 50, seed=1)]
    assert a != [r.text for r in _draws("email", 50, seed=2)]


def test_forced_outcome_is_honoured_on_both_channels():
    rng = make_rng(3)
    for ch in ("email", "phone"):
        for o in outcomes_for_channel(ch):
            assert generate_response(ch, CONTACT, INVOICE, rng, today=TODAY, forced_outcome=o).outcome is o


def test_forced_outcome_must_fit_the_channel_and_not_be_an_escalation():
    rng = make_rng(1)
    with pytest.raises(ValueError, match="cannot be produced"):
        generate_response("phone", CONTACT, INVOICE, rng, forced_outcome=Outcome.EMAIL_BOUNCE_HARD)
    with pytest.raises(ValueError, match="cannot be produced"):
        generate_response("email", CONTACT, INVOICE, rng, forced_outcome=Outcome.NO_ANSWER)
    with pytest.raises(ValueError):
        generate_response("email", CONTACT, INVOICE, rng, forced_outcome=Outcome.DQ_FAILED)


def test_promise_days_cover_both_sides_of_the_five_day_rule():
    days = collections.Counter(
        r.promised_days for r in _draws("email") + _draws("phone") if r.outcome is Outcome.PROMISE_TO_PAY)
    assert set(days) == {2, 5, 7, 14, 30}
    assert any(d <= 5 for d in days) and any(d > 5 for d in days)


def test_stated_dates_only_on_outcomes_that_carry_one():
    for r in _draws("email", 1500) + _draws("phone", 1500):
        if info(r.outcome).date_kind:
            assert r.promised_days is not None and r.promised_date == TODAY + dt.timedelta(days=r.promised_days)
        else:
            assert r.promised_days is None and r.promised_date is None


def test_stated_date_can_be_read_back_from_the_text():
    for r in _draws("email", 1500) + _draws("phone", 1500):
        if r.promised_days is not None:
            assert extract_promised_date(r.text, TODAY) == r.promised_date, r.text
            assert promised_days(r.text, TODAY) == r.promised_days


def test_promised_days_override():
    rng = make_rng(0)
    r = generate_response("email", CONTACT, INVOICE, rng, today=TODAY, forced_outcome=Outcome.PROMISE_TO_PAY, promised_days=6)
    assert r.promised_days == 6 and fmt_date(TODAY + dt.timedelta(days=6)) in r.text
    with pytest.raises(ValueError, match="does not carry a stated date"):
        generate_response("email", CONTACT, INVOICE, rng, forced_outcome=Outcome.DISPUTE, promised_days=3)


def test_weights_override_changes_the_mix():
    rng = make_rng(5)
    only_paid = {o: 0 for o in outcomes_for_channel("email")}
    only_paid[Outcome.PAID] = 1
    results = [generate_response("email", CONTACT, INVOICE, rng, weights=only_paid) for _ in range(50)]
    assert {r.outcome for r in results} == {Outcome.PAID}


def test_texts_are_complete_and_look_like_the_right_channel():
    for r in _draws("email", 1500):
        assert r.text.strip() and "{" not in r.text and "}" not in r.text
        if info(r.outcome).is_real_response:
            assert r.text.startswith("Subject:") and CONTACT.name in r.text and CONTACT.customer in r.text
            assert "Mit freundlichen Grüßen" in r.text  # country AT
    for r in _draws("phone", 1500):
        assert r.text.strip() and "{" not in r.text and "}" not in r.text
        assert r.text.startswith("Agent:")
        if info(r.outcome).is_real_response:
            assert "\nContact:" in r.text or "Contact:" in r.text


def test_texts_vary_for_the_same_outcome():
    rng = make_rng(11)
    texts = {generate_response("email", CONTACT, INVOICE, rng, today=TODAY, forced_outcome=Outcome.PROMISE_TO_PAY).text
             for _ in range(60)}
    assert len(texts) >= 10


def test_bounce_text_mentions_the_address_and_falls_back_when_missing():
    rng = make_rng(2)
    r = generate_response("email", CONTACT, INVOICE, rng, forced_outcome=Outcome.EMAIL_BOUNCE_HARD)
    assert CONTACT.email in r.text
    bare = ContactInfo(name="", customer="X Ltd")
    r2 = generate_response("email", bare, INVOICE, rng, forced_outcome=Outcome.EMAIL_BOUNCE_HARD)
    assert "the recipient" in r2.text
    # a contact with no name still produces a complete reply
    r3 = generate_response("email", bare, INVOICE, rng, today=TODAY, forced_outcome=Outcome.PROMISE_TO_PAY)
    assert "colleague" in r3.text and "{" not in r3.text


def test_payment_amounts():
    rng = make_rng(4)
    paid = generate_response("email", CONTACT, INVOICE, rng, forced_outcome=Outcome.PAID)
    assert paid.amount_paid == 1150.0
    for _ in range(30):
        part = generate_response("phone", CONTACT, INVOICE, rng, today=TODAY, forced_outcome=Outcome.PARTIAL_PAYMENT)
        assert part.amount_paid in {287.5, 575.0, 862.5}
        assert part.amount_paid < INVOICE.amount_outstanding
    assert generate_response("email", CONTACT, INVOICE, rng, forced_outcome=Outcome.DISPUTE).amount_paid is None


def test_summary_is_one_line_and_names_the_stated_date():
    for r in _draws("email", 800) + _draws("phone", 800):
        assert r.summary and "\n" not in r.summary and "{" not in r.summary
        if r.outcome in (Outcome.PROMISE_TO_PAY, Outcome.CALLBACK_REQUESTED, Outcome.OUT_OF_OFFICE):
            assert fmt_date(r.promised_date) in r.summary


def test_as_tuple_matches_the_three_value_contract():
    r = generate_response("phone", CONTACT, INVOICE, make_rng(9), today=TODAY, forced_outcome=Outcome.PROMISE_TO_PAY)
    outcome, text, days = r.as_tuple()
    assert (outcome, text, days) == (r.outcome, r.text, r.promised_days)


def test_fmt_date_ignores_locale():
    assert fmt_date(dt.date(2026, 9, 1)) == "1 Sep 2026"
    assert fmt_date(dt.date(2026, 12, 31)) == "31 Dec 2026"
