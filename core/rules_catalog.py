"""What the About page says, in one place: business rules, data quality rules, column meanings, assumptions, limits.

This is the single source of truth for the rules text. Each rule names the code that implements it and the
tests that prove it; tests/test_rules_catalog.py fails if a named function or test no longer exists, if a rule has
no test, or if a column, rule id or setting is missing here or in docs/DESIGN.md. So the page cannot quietly drift
from the code.

Rule text may contain {attempt_cap}, {promise_max_days}, {max_approvals}, {default_retry_days} and
{system_error_limit}; they are filled in from the Config sheet (or its defaults), never typed in by hand.
"""
from __future__ import annotations

import dataclasses

from core.schema import (
    AUDIT_COLUMNS, AUDIT_SHEET, CONFIG_SHEET, RUN_COLUMNS, RUN_SHEET, Col, Config,
)


@dataclasses.dataclass(frozen=True)
class BusinessRule:
    id: str
    title: str
    text: str
    example: str
    code: tuple[str, ...]  # "core.rules.decide_action"
    tests: tuple[str, ...]  # "tests/test_rules.py::test_decide_action"

    def rendered(self, config: Config) -> str:
        return self.text.format(**settings(config))

    def rendered_title(self, config: Config) -> str:
        return self.title.format(**settings(config))

    def rendered_example(self, config: Config) -> str:
        return self.example.format(**settings(config))


def settings(config: Config) -> dict[str, int]:
    return {"attempt_cap": config.attempt_cap, "promise_max_days": config.promise_max_days, "max_approvals": config.max_approvals,
            "default_retry_days": config.default_retry_days, "system_error_limit": config.system_error_limit}


R = "tests/test_rules.py::"
A = "tests/test_approvals.py::"
S = "tests/test_sheet_edits.py::"
N = "tests/test_runner.py::"
Q = "tests/test_quality.py::"
U = "tests/test_queue_actions.py::"
T = "tests/test_mock_responses.py::"
H = "tests/test_sheets.py::"
M = "tests/test_metrics.py::"
D = "tests/test_dashboard_page.py::"

RULES: tuple[BusinessRule, ...] = (
    BusinessRule("BR-01", "Who is contacted",
                 "A row is contacted only when it is in progress, still has money outstanding, its due date has passed, and any follow-up date "
                 "it was given has been reached.",
                 "INV-2026-4512 is due on 29 Aug, so it is left alone on 20 Aug and contacted once it is due.",
                 ("core.rules.decide_action",), (R + "test_decide_action", N + "test_a_not_yet_due_invoice_is_contacted_once_it_is_due")),
    BusinessRule("BR-02", "Email first",
                 "Every contact starts on email when the row has a valid, working address, because email is cheaper than a call.",
                 "A contact who was already called once is still emailed next.",
                 ("core.rules.choose_channel",), (R + "test_decide_action", N + "test_contacts_without_a_usable_email_are_called")),
    BusinessRule("BR-03", "At most {attempt_cap} attempts per channel",
                 "A contact gets at most {attempt_cap} counted attempts on email and {attempt_cap} on phone. Every attempt counts, including a bounce or a "
                 "busy line; our own system failures do not (BR-14).",
                 "After the fifth unanswered email there is never a sixth.",
                 ("core.rules.choose_channel", "core.rules.apply_outcome"),
                 (R + "test_ten_unanswered_attempts_then_a_human_never_an_eleventh", N + "test_invariants_hold_over_thirty_random_days")),
    BusinessRule("BR-04", "Switch to phone",
                 "When email has used its {attempt_cap} attempts, or is off or invalid, the contact is called instead. Counters belong to the contact: "
                 "one contact with several invoices shares one count.",
                 "INV-2026-4301 has no email address, so its first contact is a call.",
                 ("core.rules.choose_channel",), (R + "test_decide_action", R + "test_fifth_unanswered_email_moves_on_when_phone_is_still_usable")),
    BusinessRule("BR-05", "Then a person takes over",
                 "When no channel is left (both used up, switched off or invalid), the row becomes NEEDING HUMAN FOLLOW UP. If the last unanswered attempt "
                 "uses up the last channel, this happens at once, without waiting for the next retry.",
                 "{attempt_cap} emails and {attempt_cap} calls with no response ends with a person, not an eleventh attempt.",
                 ("core.rules.choose_channel", "core.rules.apply_outcome"),
                 (R + "test_fifth_unanswered_email_escalates_at_once_when_nothing_else_is_usable", R + "test_switching_off_the_last_channel_escalates_at_once",
                  N + "test_a_contact_that_never_answers_gets_five_emails_five_calls_then_a_person")),
    BusinessRule("BR-06", "One contact per day",
                 "The same contact is never contacted twice on one calendar day. A contact with several overdue invoices gets one combined email or call that "
                 "covers all of them, counted as one attempt.",
                 "Veltner Gastro has three invoices and gets one email.",
                 ("core.rules.plan_contact_event", "core.rules.build_event_updates", "core.rules.decide_action"),
                 (R + "test_combined_contact_covers_every_due_invoice_with_the_oldest_as_primary", R + "test_attempts_are_at_least_two_days_apart_and_never_twice_in_a_day",
                  N + "test_a_combined_contact_is_one_attempt_over_all_of_the_contacts_invoices")),
    BusinessRule("BR-07", "Running twice changes nothing",
                 "Every row decided on a date is stamped with that date. A second run on the same date skips them all, however many times the app is opened or run.",
                 "Run twice on 20 Aug: the second run writes nothing to the invoice sheet.",
                 ("core.runner.run_daily", "core.rules.decide_action"),
                 (N + "test_a_second_run_on_the_same_day_changes_nothing", R + "test_same_day_rerun_changes_nothing")),
    BusinessRule("BR-08", "What counts as a real response",
                 "A real response means a person engaged: a payment, a promise, a callback request, an invoice copy request, a dispute, a settlement request, a "
                 "wrong-contact reply or a refusal. An out-of-office reply, a voicemail, a bounce or silence is not a response.",
                 "OUT_OF_OFFICE is recorded and counted as an attempt, but not as a response.",
                 ("core.taxonomy.is_real_response",),
                 (T + "test_real_response_is_exactly_the_contact_made_category", R + "test_responses_are_counted_per_channel_for_real_responses_only")),
    BusinessRule("BR-09", "A date within {promise_max_days} days is followed",
                 "When the contact says when they will pay or want a callback, and that is {promise_max_days} days from today or sooner, the row waits for that date "
                 "(never earlier than tomorrow).",
                 "\"We will pay on Friday\", 3 days away: next contact on that Friday.",
                 ("core.rules.apply_outcome",), (R + "test_br09_stated_date_within_five_days_is_used_without_approval",)),
    BusinessRule("BR-10", "A later date needs approval",
                 "A date more than {promise_max_days} days away needs a person to approve it. Until then the contact is not contacted again. One promise covering "
                 "several invoices is one decision.",
                 "INV-2026-4233 promised payment on 31 Aug after contact on 18 Aug: 13 days, so it waits for approval.",
                 ("core.rules.apply_outcome", "core.rules.approve_contact_extension"),
                 (R + "test_br10_more_than_five_days_needs_human_approval", A + "test_one_promise_over_three_invoices_makes_three_rows_pending_but_needs_one_decision")),
    BusinessRule("BR-11", "At most {max_approvals} approvals per contact",
                 "A contact can have at most {max_approvals} extensions approved. A further request is not offered for approval and goes to a person. Short "
                 "promises (within {promise_max_days} days) never use an approval.",
                 "The fourth long promise from one contact becomes NEEDING HUMAN FOLLOW UP.",
                 ("core.rules.apply_outcome", "core.rules.approve_contact_extension"),
                 (R + "test_br11_fourth_extension_request_is_not_offered_for_approval", A + "test_the_cap_counts_promises_not_rows",
                  R + "test_the_approval_cap_does_not_block_short_promises")),
    BusinessRule("BR-12", "Default wait",
                 "When the contact gives no date, the next attempt is {default_retry_days} days later. Some outcomes have their own wait: payment in process 3 days, "
                 "invoice copy 2 days, a bounce or busy line the next day.",
                 "An unanswered email today means the next attempt in {default_retry_days} days.",
                 ("core.rules.apply_outcome",),
                 (R + "test_unanswered_email_counts_one_attempt_and_retries_after_the_default_gap", R + "test_date_outcomes_without_a_date_fall_back_to_the_default_gap",
                  R + "test_follow_up_gaps_come_from_the_taxonomy")),
    BusinessRule("BR-13", "Dead addresses and numbers are switched off",
                 "A hard bounce or an invalid address switches email off for that contact. An invalid or disconnected number switches phone off. A wrong-contact "
                 "reply switches both off. The switch survives the daily data quality refresh. A person can switch it back on after fixing the details.",
                 "EMAIL_BOUNCE_HARD: the next attempt is a call.",
                 ("core.rules.apply_outcome",),
                 (R + "test_br13_which_errors_switch_a_channel_off", R + "test_a_hard_bounce_moves_the_contact_to_phone_and_the_bounce_survives_the_daily_dq_refresh")),
    BusinessRule("BR-14", "Our own failures never cost an attempt",
                 "When the app itself fails (a mail gateway error, a phone system error, an unexpected exception), the failure is logged, no attempt is used, and the "
                 "contact is retried the next day. {system_error_limit} in a row send the contact to a person. One failing contact never stops the run.",
                 "A mail gateway error leaves the contact at 0 of {attempt_cap} attempts.",
                 ("core.rules.apply_outcome", "core.runner.run_daily"),
                 (R + "test_br14_system_errors_do_not_use_an_attempt_and_three_in_a_row_escalate", N + "test_an_error_in_one_contact_is_logged_and_the_run_carries_on",
                  N + "test_three_failures_in_a_row_send_the_contact_to_a_person")),
    BusinessRule("BR-15", "Payments",
                 "A full payment sets the outstanding amount to 0 and the row is handled. A part payment reduces the balance and the row keeps being followed up for "
                 "the rest. A payment of one invoice never closes the contact's other invoices.",
                 "INV-2026-4199: 1,225 RON left of 9,800 after a part payment.",
                 ("core.rules.apply_outcome",),
                 (R + "test_paid_sets_the_balance_to_zero_and_handles_the_row", R + "test_partial_payment_reduces_the_balance_and_keeps_following_up",
                  A + "test_payment_of_one_invoice_does_not_close_the_others")),
    BusinessRule("BR-16", "Replies that need a person",
                 "A dispute, a request for an instalment plan, a wrong-contact reply or a refusal to pay stops follow-up and sends the invoice to a person with the "
                 "matching reason.",
                 "INV-2026-4177: 40 crates never delivered, passed to the account manager.",
                 ("core.rules.apply_outcome",), (R + "test_br16_escalating_outcomes",)),
    BusinessRule("BR-17", "Marked not ready for contact",
                 "A row with Ready for Contact = No is never contacted. It is shown to a person with the Notes text as the reason. A blank flag is worked out by the "
                 "app and written back as Yes or No.",
                 "INV-2026-3980 (insolvency) and INV-2026-4415 (on hold).",
                 ("core.rules.decide_action", "core.quality.check_row"),
                 (R + "test_decide_action", N + "test_failed_and_human_rows_are_never_contacted_on_day_one")),
    BusinessRule("BR-18", "Rows that fail data quality",
                 "A row with a blocking data quality problem cannot be processed and is never contacted. It is listed separately for a person, and is checked again "
                 "on every run: once the data is fixed it goes back into follow-up.",
                 "A duplicate invoice, a credit note, or a row with no email and no phone.",
                 ("core.quality.check_quality", "core.rules.decide_action"),
                 (Q + "test_real_file_blocked_rows", N + "test_fixing_the_data_brings_a_failed_row_back")),
    BusinessRule("BR-19", "Earlier history is carried over",
                 "Attempts recorded before the app started are kept. If the summary says the contact was called, they count as phone attempts; if it says an email or "
                 "auto-reply, as email; otherwise they are kept as attempts of unknown channel. Unknown-channel attempts at the limit send the row to a person. "
                 "Dates promised in old summaries are honoured and are not sent for approval afterwards.",
                 "INV-2026-3902 has 5 earlier attempts with no known channel: it goes to a person.",
                 ("core.rules.initialize_row", "core.quality.check_row"),
                 (R + "test_baseline_buckets_match_the_design_3_1_6_25", R + "test_baseline_counters_dates_and_derived_flag",
                  Q + "test_dq16_legacy_attempts_are_mapped_by_summary")),
    BusinessRule("BR-20", "Credit notes are never chased",
                 "Anything that is not an invoice (a credit note, or a negative balance) is never contacted. It is listed for a person instead.",
                 "CN-2026-0118, a credit note for returned goods.",
                 ("core.quality.check_row",), (Q + "test_dq03_credit_note_and_negative_amount_block_and_are_excluded",)),
    BusinessRule("BR-21", "People's decisions are recorded, Notes is never touched",
                 "Every approval, rejection and resolution is written to the audit log with the person's name. The Notes column belongs to people: the app never writes "
                 "to it. A resolution note goes in its own column.",
                 "Alex approves a payment date: the audit log shows Alex, the time, and the change.",
                 ("core.actions.decide_extension", "core.actions.resolve_row_action", "core.sheets.SheetStore"),
                 (U + "test_approving_resumes_every_invoice_of_the_promise_and_records_who_did_it", U + "test_closing_a_row_marks_it_handled_with_a_note_and_leaves_notes_alone",
                  N + "test_notes_are_never_changed", H + "test_update_rows_never_changes_notes")),
    BusinessRule("BR-22", "Everything is logged",
                 "Every attempt and every change of status is added to the audit log, and every run adds one row to the run log. Nothing is deleted.",
                 "The run log's email and call counts equal the attempts in the audit log.",
                 ("core.runner.run_daily", "core.sheets.SheetStore"),
                 (N + "test_run_log_matches_the_audit_log", H + "test_append_audit_uses_sheet_header_order",
                  "tests/test_review_fixes.py::test_the_apps_first_time_decisions_are_in_the_audit_log_and_not_repeated")),
    BusinessRule("BR-23", "Money is never added across currencies",
                 "Amounts are shown and totalled per currency (EUR, CZK, HUF, RON). They are never converted or summed together.",
                 "The dashboard lists outstanding amounts per currency.",
                 ("core.metrics.outstanding_by_currency",),
                 (M + "test_outstanding_is_summed_within_a_currency_only", D + "test_currency_table_never_adds_across_currencies")),
    BusinessRule("BR-24", "One run at a time",
                 "Only one run per date can be in progress. People cannot approve or resolve while a run is going. A run that crashed is treated as finished after "
                 "30 minutes, so it cannot block the app for ever.",
                 "A second click on Run during a run is refused with a clear message.",
                 ("core.runner.run_daily", "core.actions.running_run"),
                 (N + "test_a_run_already_in_progress_blocks_a_second_one", N + "test_a_stale_running_row_is_marked_failed_and_does_not_block",
                  U + "test_actions_need_a_name_and_refuse_during_a_run")),
    BusinessRule("BR-25", "Decide in the app or in the sheet",
                 "A payment date can be approved or rejected with the buttons in the app, or by typing APPROVED or REJECTED in the approval_status cell of the Google "
                 "Sheet. Both end in exactly the same state. Editing one row is enough: every invoice of that contact waiting on the same date follows. Conflicting "
                 "edits change nothing and are reported.",
                 "Typing APPROVED on one of three rows approves all three and uses one approval.",
                 ("core.rules.approve_contact_extension", "core.rules.reject_contact_extension", "core.rules.reconcile_sheet_edits"),
                 (S + "test_a_sheet_approval_ends_in_the_same_state_as_the_app_button", S + "test_a_sheet_rejection_ends_in_the_same_state_as_the_app_button",
                  S + "test_approved_and_rejected_in_the_same_promise_is_a_conflict_and_nothing_changes", A + "test_approving_the_same_promise_twice_is_refused")),
    BusinessRule("BR-26", "Every row is in exactly one of four groups",
                 "Each row is either in progress, needing human follow-up, failed data quality, or handled. The four groups always add up to the number of rows; the "
                 "run page and the dashboard both check it and say so if they do not.",
                 "35 rows: 3 failed data quality, 1 handled, 6 with a person, 25 in progress, before the first contact.",
                 ("core.metrics.status_counts", "core.runner.run_daily"),
                 (M + "test_status_counts_add_up_and_unknown_statuses_are_flagged", N + "test_first_run_on_the_real_file_reconciles_and_stamps_every_row")),
    BusinessRule("BR-27", "A run cannot go back in time",
                 "A run cannot be dated earlier than rows that were already processed, because that would contact people out of order. The Run page switches the "
                 "button off and says why; if the runner is called anyway it refuses and logs the attempt as failed. Running the same date again is harmless.",
                 "Rows processed on 25 Aug: a run dated 21 Aug is refused.",
                 ("core.runner.run_daily", "core.runner.RunDateError"),
                 ("tests/test_review_fixes.py::test_a_run_dated_before_rows_already_processed_is_refused_and_logged",
                  "tests/test_review_fixes.py::test_the_run_page_blocks_a_date_before_processed_rows_before_anyone_clicks")),
)


@dataclasses.dataclass(frozen=True)
class DataQualityRule:
    id: str
    severity: str  # the strongest severity the rule can raise
    title: str
    text: str
    tests: tuple[str, ...]


DQ_RULES: tuple[DataQualityRule, ...] = (
    DataQualityRule("DQ-01", "BLOCK", "Required details", "Invoice number, customer, country, contact name, due date, outstanding amount and currency must all be filled in.", (Q + "test_dq01_each_required_field_blocks_when_blank",)),
    DataQualityRule("DQ-02", "BLOCK", "Duplicate invoices", "The same invoice number twice. The first copy goes on; later exact copies are blocked. If the copies differ in a stable detail, all of them are blocked for review.", (Q + "test_dq02_exact_duplicate_blocks_only_later_copies", Q + "test_dq02_conflicting_duplicates_block_every_copy")),
    DataQualityRule("DQ-03", "BLOCK", "Not an invoice", "A credit note, or any negative balance, is never chased.", (Q + "test_dq03_credit_note_and_negative_amount_block_and_are_excluded",)),
    DataQualityRule("DQ-04", "BLOCK", "No way to reach the contact", "Neither a valid email address nor a valid phone number.", (Q + "test_dq04_no_usable_channel_blocks", Q + "test_dq04_is_not_raised_for_rows_that_are_never_contacted_anyway")),
    DataQualityRule("DQ-05", "WARN", "Email address", "Missing or malformed. The row is still contacted by phone.", (Q + "test_dq05_email_missing_and_invalid_warn_and_keep_the_phone_channel",)),
    DataQualityRule("DQ-06", "WARN", "Phone number", "Missing or malformed. Numbers without a country code are completed from the country; a number for another country is flagged.", (Q + "test_dq06_phone_missing_invalid_normalized_and_mismatch",)),
    DataQualityRule("DQ-07", "BLOCK", "Ready for Contact flag", "Blank is worked out by the app; anything other than Yes or No blocks the row.", (Q + "test_dq07_ready_flag_blank_warns_and_is_derived_invalid_blocks",)),
    DataQualityRule("DQ-08", "INFO", "Marked not ready", "Not a data problem: the row is simply never contacted and goes to a person.", (Q + "test_dq08_ready_no_is_info_excluded_and_not_a_failure",)),
    DataQualityRule("DQ-09", "INFO", "Already settled", "Paid, or nothing outstanding: handled, never contacted.", (Q + "test_dq09_settled_rows_are_excluded_not_failed",)),
    DataQualityRule("DQ-10", "BLOCK", "Amounts", "Not a number, or more outstanding than the invoice total. A part payment (less outstanding) is fine.", (Q + "test_dq10_amount_problems_block",)),
    DataQualityRule("DQ-11", "WARN", "Very small amounts", "Below a minimum per currency set in the Config sheet. Still followed up unless the setting says to skip them.", (Q + "test_dq11_amount_below_minimum_warns_and_can_exclude",)),
    DataQualityRule("DQ-12", "BLOCK", "Dates", "Dates must be yyyy-mm-dd and the due date cannot be before the invoice date. Unusual payment terms are only noted.", (Q + "test_dq12_dates",)),
    DataQualityRule("DQ-13", "BLOCK", "Country and currency", "Only AT, CZ, DE, HU, RO and EUR, CZK, HUF, RON. A currency that is unusual for the country is a warning.", (Q + "test_dq13_country_and_currency",)),
    DataQualityRule("DQ-14", "INFO", "Not yet due", "Due after the run date: not contacted yet.", (Q + "test_dq14_not_yet_due_is_info_and_excluded",)),
    DataQualityRule("DQ-15", "WARN", "History that does not add up", "Attempts without a date or outcome, or the other way round, on rows from before the app started.", (Q + "test_dq15_history_checks", Q + "test_history_checks_are_skipped_for_rows_the_app_manages")),
    DataQualityRule("DQ-16", "WARN", "Earlier attempts with no channel", "Old attempts are assigned to phone or email from their summary; the rest are kept as unknown-channel attempts.", (Q + "test_dq16_legacy_attempts_are_mapped_by_summary", Q + "test_dq16_five_unknown_channel_attempts_suggest_a_human")),
    DataQualityRule("DQ-17", "INFO", "Several invoices, one contact", "Noted. These invoices share attempt counts and are contacted together.", (Q + "test_dq17_contact_shared_across_rows_is_info_and_counts_only_open_rows",)),
    DataQualityRule("DQ-18", "INFO", "Legal form and country", "For example a Polish company form on a Czech customer. Noted only.", (Q + "test_dq18_entity_suffix_versus_country",)),
)

COLUMN_DOCS: dict[str, str] = {
    Col.INVOICE_NUMBER: "The invoice. Duplicates are detected on it; INV- is an invoice, CN- a credit note.",
    Col.CUSTOMER: "Shown everywhere; used in search.",
    Col.COUNTRY: "The contact location filter on the dashboard, and the default dialling code for phone numbers.",
    Col.CONTACT_NAME: "Who is contacted.",
    Col.EMAIL: "Checked, then used first.",
    Col.PHONE: "Checked and converted to international format, then used when email is used up or off.",
    Col.INVOICE_DATE: "When the invoice was issued.",
    Col.DUE_DATE: "The row is contacted once this date has passed.",
    Col.INVOICE_AMOUNT: "The original amount.",
    Col.AMOUNT_OUTSTANDING: "What is still owed. 0 means paid. Reduced by part payments.",
    Col.CURRENCY: "Amounts are never added across currencies.",
    Col.LAST_CONTACT_DATE: "Date of the latest attempt.",
    Col.CONTACT_ATTEMPTS: "Total attempts: email + phone + earlier attempts of unknown channel.",
    Col.READY_FOR_CONTACT: "No means a person has put the row on hold. Blank is worked out by the app.",
    Col.OUTCOME: "The latest outcome code.",
    Col.SUMMARY: "One line on the latest reply.",
    Col.LAST_UPDATED: "When the app last changed the row.",
    Col.NOTES: "For people only. The app never writes to this column.",
    Col.ROW_KEY: "Stable identifier: invoice number plus a counter (the second copy of an invoice is #2).",
    Col.CONTACT_KEY: "Who the row belongs to: email, else phone, else name plus customer. Several invoices can share one.",
    Col.EMAIL_ATTEMPTS: "Email attempts used by this contact (kept equal on all of the contact's open rows).",
    Col.PHONE_ATTEMPTS: "Phone attempts used by this contact.",
    Col.LEGACY_ATTEMPTS: "Earlier attempts whose channel is unknown.",
    Col.EMAIL_RESPONSES: "Email attempts that got a real response.",
    Col.PHONE_RESPONSES: "Phone attempts that got a real response.",
    Col.LAST_CHANNEL: "Channel of the latest attempt.",
    Col.NEXT_FOLLOWUP_DATE: "Earliest date the row is contacted again. Blank means due now.",
    Col.LAST_PROMISED_DATE: "The date the contact last gave.",
    Col.STATUS: "In progress, needing human follow-up, data quality failed, or handled.",
    Col.DQ_STATUS: "PASS, WARN or FAIL from the data quality checks.",
    Col.DQ_ISSUES: "Which data quality rules were raised, e.g. DQ-05:EMAIL_INVALID.",
    Col.CAN_CONTACT_EMAIL: "Whether the stored email address is valid.",
    Col.CAN_CONTACT_PHONE: "Whether the stored phone number is valid.",
    Col.HUMAN_FLAG: "Yes while a person has to act.",
    Col.HUMAN_REASON: "Why a person has to act.",
    Col.APPROVAL_STATUS: "PENDING, APPROVED or REJECTED for a payment date. You can type APPROVED or REJECTED here.",
    Col.PENDING_APPROVAL_DATE: "The date waiting for approval. You can edit it before approving.",
    Col.EXTENSION_APPROVALS_COUNT: "Approvals used by this contact, out of the maximum.",
    Col.CONSECUTIVE_SYSTEM_ERRORS: "Our own failures in a row for this contact.",
    Col.LAST_PROCESSED_DATE: "The date the row was last decided. This is what makes a second run on the same date do nothing.",
    Col.HUMAN_RESOLUTION_NOTE: "A person's note when resolving a row. Separate from Notes.",
    Col.EMAIL_DISABLED: "Yes once a bounce or invalid address switched email off.",
    Col.PHONE_DISABLED: "Yes once an invalid or disconnected number switched phone off.",
}

TABS = (
    ("Invoices", "Your original 18 columns, untouched in name and order, plus the columns the app adds on the right."),
    (AUDIT_SHEET, "One row for every attempt and every change of status, never edited or deleted: when, who or what, channel, outcome, the reply text. " + ", ".join(AUDIT_COLUMNS) + "."),
    (RUN_SHEET, "One row per run: counts of rows decided, skipped, emails, calls, escalations, and any problems. " + ", ".join(RUN_COLUMNS) + "."),
    (CONFIG_SHEET, "The settings below, one per row. Change a value in the sheet and the next run uses it."),
)

SETTING_DOCS: dict[str, str] = {
    "attempt_cap": "Attempts allowed per channel per contact",
    "promise_max_days": "Longest wait a contact can name without needing approval",
    "max_approvals": "Approvals allowed per contact",
    "default_retry_days": "Days to wait after an attempt with no date given",
    "system_error_limit": "Failures of our own in a row before a person is asked",
    "invoice_prefix": "What an invoice number starts with; anything else is not chased",
    "timezone": "Time zone that decides what 'today' is",
    "skip_below_minimum": "Leave amounts below the minimum alone",
    "legacy_migration_done": "Set once the earlier attempts have been carried over",
}

ASSUMPTIONS: tuple[tuple[str, str, str], ...] = (
    ("A-01", "Confirmed", "A row marked Ready for Contact = No counts as needing human follow-up, with the Notes text as the reason. A blank flag is worked out by the app."),
    ("A-02", "Confirmed", "A contact with several invoices gets one combined email or call per day covering all of them."),
    ("A-03", "Confirmed", "Dates promised in old summaries are followed as stated and are not sent for approval afterwards."),
    ("A-04", "Confirmed", "An out-of-office return date needs no approval and does not use one of the approvals."),
    ("A-05", "Assumed", "Failures of our own (mail gateway, phone system) do not use an attempt."),
    ("A-06", "Placeholder", "The default wait of 2 days and the minimum amounts (EUR 10, CZK 250, HUF 4000, RON 50) are placeholders. Change them in the Config sheet."),
    ("A-07", "Assumed", "In this version the app writes the outstanding amount when a mock payment arrives. With real payments the balance would come from your finance system."),
    ("A-08", "Assumed", "Mock replies are in English. Only the sign-off of an email follows the contact's country."),
    ("A-09", "Confirmed", "Existing outcomes DISPUTE, SETTLEMENT_REQUESTED and WRONG_CONTACT move the row to a person when it is first set up."),
    ("A-10", "Recommended", "Protect the deployed app with app_password in the secrets (and, if you can, restrict viewers in Streamlit's sharing settings), and share the sheet only with the service account."),
    ("A-11", "Confirmed", "A row with no channel left gets the outcome NEEDS_HUMAN_FOLLOW_UP; only a used-up attempt cap gets MAX_ATTEMPTS_REACHED."),
    ("A-12", "Confirmed", "A person's approval or resolution does not clear the row's processed date, so the row resumes on its next follow-up date, not the same day."),
    ("A-13", "Confirmed", "A rejected extension keeps the row with a person, marked REJECTED, with the reason code unchanged."),
    ("A-14", "Confirmed", "If the setting to skip very small amounts is on, those rows stay in progress and are never contacted."),
)

LIMITATIONS: tuple[str, ...] = (
    "No real email is sent and no real call is made. Every reply is simulated, which is what lets all outcomes be tested.",
    "The app only works when someone opens it and presses Run. Streamlit Community Cloud has no background scheduler, and an app that is not visited goes to sleep.",
    "The Google Sheet is the only store. Google limits writes to about 60 requests a minute, so the app writes in batches. Large sheets will run more slowly.",
    "The sheet does not say who edited a cell. Decisions typed into the sheet are logged as 'sheet edit'; Google's version history shows who.",
    "Attempt counters belong to the contact, and the app reads the highest value across the contact's rows. Lowering one row by hand does nothing: use Resolve in the Human Queue to reset them.",
    "Pages cache what they read for 30 seconds, so a colleague's page can be slightly behind. Every approval, rejection and resolution re-reads the sheet first, so a stale page cannot cause a wrong write.",
    "Amounts are never converted between currencies, so there is no grand total.",
    "Themes in a contact's replies come from matching phrases, not from understanding the text. Real replies worded differently may be missed.",
    "If the app stops between writing a row and writing its audit entry, that one audit entry is lost. The sheet itself stays correct. The gap is a fraction of a second per batch.",
    "Dates follow the time zone in the Config sheet (default Europe/Berlin), not the server's clock.",
    "Dates in the sheet must be shown as yyyy-mm-dd, as in your file. If the sheet shows another date format, every row is blocked with a clear message.",
    "Every page load reads the whole Audit_Log (cached for 30 seconds). That is fine for thousands of rows; for a much larger log, move old rows to another file.",
)

PAGES = (
    ("Run", "Run the day's follow-ups, see what happened, and the data quality results. Running again the same day does nothing."),
    ("Human Queue", "Payment dates waiting for a decision, rows needing human follow-up, and rows that failed data quality, in separate sections."),
    ("Dashboard", "Attempts, response, success and failure rates, filters by country and contact, and a drill-down to one contact."),
    ("About", "This page."),
)

STATUS_TABLE = (
    ("In progress", "Being followed up, or waiting for its next date."),
    ("Needing human follow-up", "The app stopped. A person must decide, approve, or fix something."),
    ("Data quality failed", "Cannot be processed until the data in the sheet is fixed."),
    ("Handled", "Paid, or closed by a person. No more follow-up."),
)

ARCHITECTURE_DOT = """
digraph G {
  rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#f4f4f1", color="#898781", fontname="Helvetica", fontsize=11];
  edge [color="#52514e", fontname="Helvetica", fontsize=10];
  people [label="People\\n(browser)"];
  app    [label="Streamlit app\\nRun | Human Queue | Dashboard | About"];
  core   [label="core/ (plain Python, tested)\\ndata quality | rules | mock replies\\ndaily run | metrics"];
  sheet  [label="Google Sheet (the only store)\\nInvoices | Audit_Log | Run_Log | Config", fillcolor="#e4eefb"];
  people -> app; app -> core; core -> sheet [label="service account"];
  people -> sheet [label="may edit cells\\ndirectly", style=dashed];
}
"""

LIFECYCLE_DOT = """
digraph G {
  rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#f4f4f1", color="#898781", fontname="Helvetica", fontsize=11];
  edge [color="#52514e", fontname="Helvetica", fontsize=10];
  new [label="First run\\n(set up)"]; ip [label="In progress", fillcolor="#e4eefb"]; nh [label="Needing human\\nfollow-up", fillcolor="#fdebd9"];
  dq [label="Data quality\\nfailed", fillcolor="#fbe0e0"]; ha [label="Handled", fillcolor="#dff3ea"];
  new -> ip; new -> nh [label="on hold,\\nearlier cap"]; new -> dq; new -> ha [label="paid"];
  ip -> ha [label="paid"]; ip -> nh [label="dispute, 5+5 attempts,\\ndate needs approval"]; ip -> dq [label="data changes"];
  nh -> ip [label="approved or\\nresolved"]; nh -> ha [label="closed"]; dq -> ip [label="data fixed"];
}
"""
