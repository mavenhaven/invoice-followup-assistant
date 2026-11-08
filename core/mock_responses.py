"""Mock contact responses: realistic email replies and phone transcripts for every outcome.

The runner calls generate_response() instead of really sending an email or placing a call.
Output is random but reproducible: pass a seeded random.Random. All text is English; only the
email sign-off follows the contact's country (docs/DESIGN.md assumption A-08).

Dates inside the text always look like "14 Sep 2026" so core.taxonomy.extract_promised_date can
read them back, which the tests rely on.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import random
from typing import Mapping

from core.schema import Channel
from core.taxonomy import Outcome, info, outcomes_for_channel

O = Outcome

# Relative likelihood of each outcome per channel. Every producible outcome has a weight of at least 1
# so all of them show up in testing. Escalation codes are never produced here.
EMAIL_WEIGHTS: dict[Outcome, float] = {
    O.NO_RESPONSE: 26, O.PROMISE_TO_PAY: 14, O.PAID: 6, O.PARTIAL_PAYMENT: 4, O.PAYMENT_IN_PROCESS: 6,
    O.CALLBACK_REQUESTED: 3, O.REQUESTS_INVOICE_COPY: 5, O.SETTLEMENT_REQUESTED: 3, O.DISPUTE: 4,
    O.WRONG_CONTACT: 3, O.REFUSED_TO_PAY: 2, O.OUT_OF_OFFICE: 6, O.EMAIL_BOUNCE_SOFT: 4,
    O.EMAIL_BOUNCE_HARD: 3, O.EMAIL_INVALID_ADDRESS: 2, O.EMAIL_SEND_FAILURE: 2, O.SYSTEM_ERROR: 1,
}
PHONE_WEIGHTS: dict[Outcome, float] = {
    O.NO_ANSWER: 22, O.LEFT_VOICEMAIL: 12, O.PROMISE_TO_PAY: 14, O.PAID: 5, O.PARTIAL_PAYMENT: 4,
    O.PAYMENT_IN_PROCESS: 5, O.CALLBACK_REQUESTED: 6, O.REQUESTS_INVOICE_COPY: 3, O.SETTLEMENT_REQUESTED: 3,
    O.DISPUTE: 4, O.WRONG_CONTACT: 4, O.REFUSED_TO_PAY: 2, O.OUT_OF_OFFICE: 3, O.PHONE_LINE_BUSY: 5,
    O.PHONE_INVALID_NUMBER: 2, O.PHONE_DISCONNECTED: 2, O.PHONE_SYSTEM_ERROR: 2, O.SYSTEM_ERROR: 1,
}
DEFAULT_WEIGHTS: dict[Channel, dict[Outcome, float]] = {Channel.EMAIL: EMAIL_WEIGHTS, Channel.PHONE: PHONE_WEIGHTS}

# Days from today until the date the contact states. A mix of 5 days or fewer (no approval needed)
# and more than 5 (needs human approval) so the approval rule gets exercised.
PROMISE_DAYS = ([2, 5, 7, 14, 30], [3, 3, 2, 2, 1])
PARTIAL_DAYS = ([7, 14], [1, 1])
CALLBACK_DAYS = ([1, 2, 3, 7], [3, 3, 2, 1])
RETURN_DAYS = ([3, 7, 14], [2, 2, 1])

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_SIGNOFFS = {"DE": "Mit freundlichen Grüßen", "AT": "Mit freundlichen Grüßen", "CZ": "S pozdravem",
             "HU": "Üdvözlettel", "RO": "Cu stimă"}
_DISPUTE_REASONS = [
    "40 crates were never delivered", "the prices differ from our contract", "the goods arrived damaged",
    "part of the delivery was already returned", "we were charged twice for the same order",
]


@dataclasses.dataclass(frozen=True)
class ContactInfo:
    name: str
    customer: str
    email: str = ""
    phone: str = ""
    country: str = ""


@dataclasses.dataclass(frozen=True)
class InvoiceInfo:
    invoice_number: str
    amount_outstanding: float
    currency: str
    due_date: dt.date | None = None


@dataclasses.dataclass(frozen=True)
class MockResponse:
    channel: Channel
    outcome: Outcome
    text: str  # full email reply or phone transcript; goes to Audit_Log.response_text
    summary: str  # one line; goes to the Summary column
    promised_days: int | None = None  # days from today to the date the contact stated, if any
    promised_date: dt.date | None = None
    amount_paid: float | None = None  # PAID and PARTIAL_PAYMENT only

    def as_tuple(self) -> tuple[Outcome, str, int | None]:
        return self.outcome, self.text, self.promised_days


def fmt_date(d: dt.date) -> str:
    """'14 Sep 2026', independent of the machine's locale."""
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def fmt_amount(value: float, currency: str) -> str:
    return f"{value:,.2f} {currency}"


# ---------------------------------------------------------------- templates
# Placeholders: {first} {name} {customer} {invoice} {amount} {date} {paid} {remaining} {reason}
#               {n} (instalments) {email} {phone}

_EMAIL_BODIES: dict[Outcome, list[str]] = {
    O.PAID: [
        "We transferred {amount} for invoice {invoice} today. The bank confirmation is attached.",
        "Payment for {invoice} left our account this morning and should reach you within 1 to 2 working days. Sorry for the delay.",
        "This invoice was settled already. Please check your records, the bank confirmation for {invoice} is attached.",
    ],
    O.PARTIAL_PAYMENT: [
        "We paid {paid} against {invoice} today. The remaining {remaining} will follow by {date}.",
        "A part payment of {paid} was released for {invoice}. We will pay the balance of {remaining} on {date}.",
    ],
    O.PROMISE_TO_PAY: [
        "Thank you for the reminder. Invoice {invoice} is approved and will be included in our payment run on {date}.",
        "Apologies for the delay. Our accounts team will pay {invoice} ({amount}) by {date}.",
        "We expect to release the payment for {invoice} on {date}. Please do not send further reminders until then.",
    ],
    O.PAYMENT_IN_PROCESS: [
        "Payment for {invoice} has been initiated and is waiting for bank approval. The funds should arrive in 3 days.",
        "The transfer for {invoice} is in process on our side. Please allow 3 days for it to show on your account.",
    ],
    O.CALLBACK_REQUESTED: [
        "Could you call me on {date}? I will be at my desk in the afternoon and can talk about {invoice}.",
        "Please phone me on {date} instead of writing. It is easier to clarify {invoice} by phone.",
    ],
    O.REQUESTS_INVOICE_COPY: [
        "Could you please resend {invoice}? We cannot find it in our system. A PDF with our PO number would help.",
        "We do not seem to have received {invoice}. Please send a copy and we will process it.",
    ],
    O.SETTLEMENT_REQUESTED: [
        "We are in a tight cash position at the moment. Could we agree an instalment plan of {n} monthly payments for {invoice}?",
        "Paying {amount} in one go is difficult for us. Can we split {invoice} into {n} monthly instalments?",
    ],
    O.DISPUTE: [
        "We dispute {invoice}: {reason}. Please do not escalate until our account manager has reviewed it.",
        "We cannot approve {invoice} because {reason}. Please send a credit note or supporting documents.",
    ],
    O.WRONG_CONTACT: [
        "I am not responsible for accounts payable. Please contact our finance department. I do not have their details.",
        "You have the wrong person. I work in purchasing and do not handle payments for {invoice}.",
    ],
    O.REFUSED_TO_PAY: [
        "We will not pay {invoice}. The matter is with our lawyers.",
        "We reject this claim in full and consider the matter closed.",
    ],
}

_EMAIL_STANDALONE: dict[Outcome, list[str]] = {
    O.NO_RESPONSE: ["No reply received."],
    O.OUT_OF_OFFICE: [
        "Automatic reply: I am out of the office until {date} with limited access to email. For urgent matters please contact reception.",
        "Auto-reply: I am on leave and will be back on {date}. Your message will not be forwarded.",
    ],
    O.EMAIL_BOUNCE_SOFT: [
        "Mail Delivery Subsystem: Temporary failure delivering to {email}. Mailbox full (452 4.2.2). The message will be retried.",
        "Delivery delayed: the receiving server for {email} is busy (421 4.7.0). Will retry.",
    ],
    O.EMAIL_BOUNCE_HARD: [
        "Mail Delivery Subsystem: 550 5.1.1 The email account {email} does not exist. Message not delivered.",
        "Undeliverable: the domain of {email} rejected the message permanently (554 5.7.1).",
    ],
    O.EMAIL_INVALID_ADDRESS: [
        "Address rejected: {email} is not a valid email address (501 5.1.3).",
    ],
    O.EMAIL_SEND_FAILURE: ["[Email gateway timeout. Message was not sent.]"],
    O.SYSTEM_ERROR: ["[Unexpected error in the follow-up service. No contact attempt was made.]"],
}

_PHONE_TRANSCRIPTS: dict[Outcome, list[str]] = {
    O.PAID: [
        "Agent: Good morning, this is accounts receivable calling about invoice {invoice} for {amount}.\n"
        "Contact: Yes, that one was paid yesterday. I can send you the bank confirmation.\n"
        "Agent: Thank you, please do. We will close it on receipt.\nContact: Will do.",
    ],
    O.PARTIAL_PAYMENT: [
        "Agent: Hello {first}, I am calling about {invoice}.\n"
        "Contact: We paid {paid} this week. The remaining {remaining} will follow on {date}.\n"
        "Agent: Understood, we will note {date} for the balance.\nContact: Thank you.",
    ],
    O.PROMISE_TO_PAY: [
        "Agent: Good morning, this is accounts receivable calling about invoice {invoice} for {amount}.\n"
        "Contact: Yes, I know about it. Let me check. It is approved and will go out with the payment run on {date}.\n"
        "Agent: Thank you, we will note {date}.\nContact: Fine, goodbye.",
        "Agent: Hello, am I speaking with {name}? I am calling about the open invoice {invoice}.\n"
        "Contact: Sorry, it slipped through. I will make sure it is paid by {date}.\n"
        "Agent: Can you confirm that date?\nContact: Yes, {date}.",
    ],
    O.PAYMENT_IN_PROCESS: [
        "Agent: Hello {first}, a quick call about {invoice}.\n"
        "Contact: The payment is already in process. It should arrive in 3 days.\n"
        "Agent: Great, thank you. We will check in 3 days.\nContact: Okay.",
    ],
    O.CALLBACK_REQUESTED: [
        "Agent: Good afternoon, calling about invoice {invoice}.\n"
        "Contact: I am in a meeting right now. Please call me back on {date} after 14:00.\n"
        "Agent: Of course, we will call on {date}.\nContact: Thanks.",
    ],
    O.REQUESTS_INVOICE_COPY: [
        "Agent: Hello {first}, I am calling about {invoice}.\n"
        "Contact: I do not have that invoice. Can you email it again?\n"
        "Agent: Yes, I will resend it today.\nContact: Thank you, then I can process it.",
    ],
    O.SETTLEMENT_REQUESTED: [
        "Agent: Hello {first}, calling about {invoice} for {amount}.\n"
        "Contact: Honestly we cannot pay that all at once. Could we do {n} monthly instalments?\n"
        "Agent: I cannot agree that myself, but I will pass your request to my colleague.\nContact: Please do.",
    ],
    O.DISPUTE: [
        "Agent: Good morning, I am calling about invoice {invoice}.\n"
        "Contact: We are not paying that one. {reason}.\n"
        "Agent: I understand. I will pass this to the account manager.\nContact: Yes, please do.",
    ],
    O.WRONG_CONTACT: [
        "Agent: Hello, I am looking for {name} about invoice {invoice}.\n"
        "Contact: This is reception. {first} does not handle payments. I do not know who does.\n"
        "Agent: Thank you, I will try another way.\nContact: Sorry I could not help.",
    ],
    O.REFUSED_TO_PAY: [
        "Agent: Hello {first}, I am calling about invoice {invoice}.\n"
        "Contact: We will not pay. Do not call again, speak to our lawyer.\n"
        "Agent: Understood. Thank you for your time.",
    ],
    O.OUT_OF_OFFICE: [
        "Agent: Hello, may I speak to {name}?\n"
        "Contact: {first} is on leave until {date}. Please call after that.\n"
        "Agent: Thank you, we will call then.",
    ],
    O.NO_ANSWER: ["Agent: [call placed to {phone}; rang 8 times; no answer]"],
    O.LEFT_VOICEMAIL: [
        "Agent: [voicemail] Hello {first}, this is accounts receivable calling about invoice {invoice}. "
        "Please call us back or reply to our email. Thank you.",
    ],
    O.PHONE_LINE_BUSY: ["Agent: [call placed to {phone}; line busy; retried once, still busy]"],
    O.PHONE_INVALID_NUMBER: ["Agent: [automated message: the number you have dialled is not in service]"],
    O.PHONE_DISCONNECTED: ["Agent: [number {phone} disconnected; fax tone heard]"],
    O.PHONE_SYSTEM_ERROR: ["Agent: [telephony gateway error SIP 503; call was not placed]"],
    O.SYSTEM_ERROR: ["Agent: [unexpected error in the follow-up service; no call was made]"],
}

_SUMMARIES: dict[Outcome, str] = {
    O.PAID: "Paid in full.",
    O.PARTIAL_PAYMENT: "Paid {paid}; balance promised by {date}.",
    O.PROMISE_TO_PAY: "Promised payment by {date}.",
    O.PAYMENT_IN_PROCESS: "Payment initiated, expected in 3 days.",
    O.CALLBACK_REQUESTED: "Asked to be called back on {date}.",
    O.REQUESTS_INVOICE_COPY: "Asked for a copy of the invoice.",
    O.SETTLEMENT_REQUESTED: "Asked for an instalment plan.",
    O.DISPUTE: "Disputes the invoice: {reason}.",
    O.WRONG_CONTACT: "Not the right contact for payments.",
    O.REFUSED_TO_PAY: "Refused to pay.",
    O.NO_RESPONSE: "No reply to email.",
    O.NO_ANSWER: "Called, no answer.",
    O.LEFT_VOICEMAIL: "Called, voicemail left.",
    O.OUT_OF_OFFICE: "Out of office until {date}.",
    O.EMAIL_BOUNCE_SOFT: "Email delayed (temporary failure).",
    O.EMAIL_BOUNCE_HARD: "Email bounced (address does not exist).",
    O.EMAIL_INVALID_ADDRESS: "Email address rejected as invalid.",
    O.PHONE_LINE_BUSY: "Called, line busy.",
    O.PHONE_INVALID_NUMBER: "Called, number not in service.",
    O.PHONE_DISCONNECTED: "Called, number disconnected.",
    O.EMAIL_SEND_FAILURE: "System error: email was not sent.",
    O.PHONE_SYSTEM_ERROR: "System error: call was not placed.",
    O.SYSTEM_ERROR: "System error: no contact attempt made.",
}


def _check_templates() -> None:
    for ch, weights in DEFAULT_WEIGHTS.items():
        assert set(weights) == set(outcomes_for_channel(ch)), f"weights for {ch.value} do not match the taxonomy"
    for o in outcomes_for_channel(Channel.EMAIL):
        assert o in _EMAIL_BODIES or o in _EMAIL_STANDALONE, f"no email template for {o.value}"
    for o in outcomes_for_channel(Channel.PHONE):
        assert o in _PHONE_TRANSCRIPTS, f"no phone template for {o.value}"
    assert set(_SUMMARIES) == {o for o in Outcome if info(o).channels}


_check_templates()


# ---------------------------------------------------------------- generation


def make_rng(seed: int | None = None) -> random.Random:
    return random.Random(seed)


def generatable_outcomes(channel: Channel | str) -> list[Outcome]:
    return outcomes_for_channel(Channel(channel))


def _choose_days(outcome: Outcome, rng: random.Random) -> int | None:
    kind = info(outcome).date_kind
    if kind is None:
        return None
    if outcome is O.PARTIAL_PAYMENT:
        choices = PARTIAL_DAYS
    elif kind == "payment":
        choices = PROMISE_DAYS
    elif kind == "callback":
        choices = CALLBACK_DAYS
    else:
        choices = RETURN_DAYS
    return rng.choices(choices[0], weights=choices[1])[0]


def generate_response(
    channel: Channel | str,
    contact: ContactInfo,
    invoice: InvoiceInfo,
    rng: random.Random,
    *,
    today: dt.date | None = None,
    forced_outcome: Outcome | str | None = None,
    promised_days: int | None = None,
    weights: Mapping[Outcome, float] | None = None,
) -> MockResponse:
    """Simulate one contact attempt.

    forced_outcome pins the outcome (for tests); it must be producible on this channel.
    promised_days overrides the random day count for outcomes where a contact can state a date.
    weights overrides the per-outcome likelihood.
    """
    ch = Channel(channel)
    today = today or dt.date.today()
    allowed = outcomes_for_channel(ch)

    if forced_outcome is not None:
        outcome = Outcome(forced_outcome)
        if outcome not in allowed:
            raise ValueError(f"{outcome.value} cannot be produced on the {ch.value} channel")
    else:
        table = dict(DEFAULT_WEIGHTS[ch])
        if weights:
            table.update(weights)
        outcome = rng.choices(allowed, weights=[max(table.get(o, 0), 0) for o in allowed])[0]

    if promised_days is not None and info(outcome).date_kind is None:
        raise ValueError(f"{outcome.value} does not carry a stated date")
    days = promised_days if promised_days is not None else _choose_days(outcome, rng)
    date = None if days is None else today + dt.timedelta(days=days)

    first = (contact.name.split() or ["colleague"])[0]
    paid = None
    if outcome is O.PAID:
        paid = round(invoice.amount_outstanding, 2)
    elif outcome is O.PARTIAL_PAYMENT:
        paid = round(invoice.amount_outstanding * rng.choice([0.25, 0.5, 0.75]), 2)
    ctx = {
        "first": first, "name": contact.name or first, "customer": contact.customer,
        "invoice": invoice.invoice_number,
        "amount": fmt_amount(invoice.amount_outstanding, invoice.currency),
        "date": fmt_date(date) if date else "",
        "paid": fmt_amount(paid, invoice.currency) if paid is not None else "",
        "remaining": fmt_amount(invoice.amount_outstanding - (paid or 0), invoice.currency),
        "reason": rng.choice(_DISPUTE_REASONS), "n": rng.choice([3, 6, 12]),
        "email": contact.email or "the recipient", "phone": contact.phone or "the number on file",
    }

    if ch is Channel.PHONE:
        text = rng.choice(_PHONE_TRANSCRIPTS[outcome]).format(**ctx)
    elif outcome in _EMAIL_BODIES:
        body = rng.choice(_EMAIL_BODIES[outcome]).format(**ctx)
        subject = rng.choice([f"RE: Overdue invoice {invoice.invoice_number}",
                              f"Re: Payment reminder {invoice.invoice_number}",
                              f"RE: {invoice.invoice_number} - open balance"])
        greeting = rng.choice(["Hello,", "Good morning,", "Dear colleague,", "Hi,"])
        signoff = _SIGNOFFS.get(contact.country.upper(), rng.choice(["Best regards", "Kind regards", "Regards"]))
        text = f"Subject: {subject}\n\n{greeting}\n\n{body}\n\n{signoff},\n{ctx['name']}\n{contact.customer}"
    else:
        text = rng.choice(_EMAIL_STANDALONE[outcome]).format(**ctx)

    return MockResponse(
        channel=ch, outcome=outcome, text=text, summary=_SUMMARIES[outcome].format(**ctx),
        promised_days=days, promised_date=date, amount_paid=paid,
    )
