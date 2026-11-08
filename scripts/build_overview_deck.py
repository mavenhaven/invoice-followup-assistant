"""Build the overview deck (12 slides, python-pptx): python scripts/build_overview_deck.py [output.pptx]

Numbers on the slides are read from the code (rules, outcomes, columns), so the deck cannot drift from the app.
"""
from __future__ import annotations

import sys
from pathlib import Path

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Inches, Pt

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.rules_catalog import DQ_RULES, RULES  # noqa: E402
from core.schema import INVOICES_EXISTING_COLUMNS, INVOICES_NEW_COLUMNS  # noqa: E402
from core.taxonomy import Category, Outcome, outcomes_in  # noqa: E402

DEFAULT_OUT = ROOT / "docs" / "Invoice-Followup-Assistant.pptx"
HEAD, BODY = "Cambria", "Calibri"


def rgb(hex_: str) -> RGBColor:
    return RGBColor.from_string(hex_)


INK, INK2, PAPER, WHITE = "14213D", "1F3358", "F4F6F8", "FFFFFF"
AMBER, TEAL, RED = "FCA311", "2A9D8F", "C8553D"
BLUE, ORANGE, AQUA, YELLOW, GREY = "2A78D6", "EB6834", "1BAF7A", "EDA100", "8D99AE"
MUTED, LINE, SOFT = "5B6577", "D5DAE2", "AEB8CC"


# ---------------------------------------------------------------- helpers


def style_text(tf, size, color, bold=False, align=PP_ALIGN.LEFT, font=BODY, anchor=MSO_ANCHOR.TOP):
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    for p in tf.paragraphs:
        p.alignment = align
        for r in p.runs:
            r.font.size, r.font.bold, r.font.name, r.font.color.rgb = Pt(size), bold, font, rgb(color)


def text(slide, x, y, w, h, content, size=14, color=INK, bold=False, align=PP_ALIGN.LEFT, font=BODY, anchor=MSO_ANCHOR.TOP, name=None):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    if name:
        box.name = name
    lines = content if isinstance(content, list) else [content]
    tf = box.text_frame
    tf.text = lines[0]
    for extra in lines[1:]:
        tf.add_paragraph().text = extra
    style_text(tf, size, color, bold, align, font, anchor)
    return box


def box(slide, x, y, w, h, fill=WHITE, line=LINE, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.08, name=None):
    s = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    if shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        s.adjustments[0] = radius
    s.fill.solid()
    s.fill.fore_color.rgb = rgb(fill)
    if line:
        s.line.color.rgb = rgb(line)
        s.line.width = Pt(0.75)
    else:
        s.line.fill.background()
    s.shadow.inherit = False
    if name:
        s.name = name
    return s


def label(shape, content, size=14, color=INK, bold=False, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE, font=BODY):
    tf = shape.text_frame
    lines = content if isinstance(content, list) else [content]
    tf.text = lines[0]
    for extra in lines[1:]:
        tf.add_paragraph().text = extra
    style_text(tf, size, color, bold, align, font, anchor)
    tf.margin_left = tf.margin_right = Inches(0.1)


def circle(slide, x, y, d, fill, content="", color=WHITE, size=16):
    c = box(slide, x, y, d, d, fill, None, shape=MSO_SHAPE.OVAL)
    if content:
        label(c, content, size, color, bold=True)
        c.text_frame.margin_left = c.text_frame.margin_right = 0  # two-digit numbers must not wrap
        c.text_frame.word_wrap = False
    return c


def arrow(slide, x1, y1, x2, y2, color=MUTED, both=False, width=2.0):
    c = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    c.line.color.rgb = rgb(color)
    c.line.width = Pt(width)
    ln = c.line._get_or_add_ln()
    for tag in (("a:headEnd", "a:tailEnd") if both else ("a:tailEnd",)):
        end = OxmlElement(tag)
        end.set("type", "triangle")
        ln.append(end)
    return c


def new_slide(prs, title, dark=False, notes=""):
    slide = prs.slides.add_slide(prs.slide_layouts[5])  # "Title Only": the title is a real placeholder
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = rgb(INK if dark else PAPER)
    t = slide.shapes.title
    t.left, t.top, t.width, t.height = Inches(0.5), Inches(0.35), Inches(9), Inches(0.8)
    t.text_frame.text = title
    style_text(t.text_frame, 34, WHITE if dark else INK, bold=True, font=HEAD, anchor=MSO_ANCHOR.MIDDLE)
    if notes:
        slide.notes_slide.notes_text_frame.text = notes
    return slide


LABELS = {Outcome.REQUESTS_INVOICE_COPY: "Wants invoice copy", Outcome.SETTLEMENT_REQUESTED: "Requests settlement",
          Outcome.EMAIL_INVALID_ADDRESS: "Invalid email", Outcome.PHONE_INVALID_NUMBER: "Invalid phone number",
          Outcome.NEEDS_HUMAN_FOLLOW_UP: "Needs a person", Outcome.DQ_FAILED: "Data quality failed",
          Outcome.EMAIL_BOUNCE_SOFT: "Email soft bounce", Outcome.EMAIL_BOUNCE_HARD: "Email hard bounce"}


def outcome_label(o: Outcome) -> str:
    return LABELS.get(o) or o.value.replace("_", " ").capitalize()


# ---------------------------------------------------------------- the slides


def build(out: Path) -> Path:
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(10), Inches(5.625)
    n_rules, n_dq = len(RULES), len(DQ_RULES)
    n_old, n_new = len(INVOICES_EXISTING_COLUMNS), len(INVOICES_NEW_COLUMNS)

    # 1 Title -------------------------------------------------------------
    s = new_slide(prs, "Invoice Follow-Up Assistant", dark=True,
                  notes="An app that chases overdue invoices by email first and then phone, records every attempt, and hands hard cases to a person. One Google Sheet is the only data store. No real email or calls yet: replies are simulated.")
    s.shapes.title.left, s.shapes.title.top, s.shapes.title.width, s.shapes.title.height = Inches(0.5), Inches(1.4), Inches(5.6), Inches(1.6)
    style_text(s.shapes.title.text_frame, 44, WHITE, bold=True, font=HEAD, anchor=MSO_ANCHOR.TOP)
    text(s, 0.5, 3.2, 5.6, 0.5, "Overview: what it does and how it is built", 22, AMBER)
    text(s, 0.5, 4.6, 6, 0.35, "Streamlit  |  Google Sheets  |  Python", 14, SOFT)
    for i, (colr, name) in enumerate([(TEAL, "PAID"), (AMBER, "PROMISE_TO_PAY"), (RED, "NEEDS HUMAN")]):
        y = 1.5 + i * 1.05
        box(s, 6.5, y, 3.0, 0.8, INK2, None, name=f"row {name}")
        circle(s, 6.7, y + 0.24, 0.32, colr)
        text(s, 7.2, y, 2.2, 0.8, name, 14, WHITE, bold=True, anchor=MSO_ANCHOR.MIDDLE)

    # 2 Problem and goal --------------------------------------------------
    s = new_slide(prs, "Overdue invoices need chasing", notes="The case-study file has 35 overdue invoices in 5 countries and 4 currencies. Follow-up today is manual and uneven; nothing records what was tried or what the contact said.")
    for i, (head, body, colr) in enumerate([("The problem", "Follow-ups are manual and uneven. Nobody can see what was tried or what the contact said.", RED),
                                            ("The goal", "Follow up automatically, keep every attempt on record, and send hard cases to a person.", TEAL)]):
        x = 0.5 + i * 4.6
        box(s, x, 1.4, 4.4, 2.0, name=f"card {head}")
        circle(s, x + 0.25, 1.62, 0.4, colr)
        text(s, x + 0.8, 1.62, 3.4, 0.4, head, 20, INK, bold=True, anchor=MSO_ANCHOR.MIDDLE)
        text(s, x + 0.25, 2.25, 3.9, 1.0, body, 16, MUTED)
    for i, (num, lab) in enumerate([("35", "overdue invoices"), ("5", "countries"), ("4", "currencies")]):
        x = 0.5 + i * 3.05
        box(s, x, 3.7, 2.9, 1.3, name=f"stat {lab}")
        text(s, x + 0.25, 3.78, 1.3, 1.1, num, 44, INK if i != 1 else TEAL, bold=True, font=HEAD, anchor=MSO_ANCHOR.MIDDLE)
        text(s, x + 1.4, 3.78, 1.4, 1.1, lab, 16, MUTED, anchor=MSO_ANCHOR.MIDDLE)

    # 3 Architecture ------------------------------------------------------
    s = new_slide(prs, "How the pieces fit", notes="People use the Streamlit app. It calls plain-Python engine modules, which are the only code that reads and writes the Google Sheet. People can also edit the sheet directly, for example to approve a payment date.")
    cols = [(0.5, "Streamlit app", "what people use", INK, ["Run", "Human Queue", "Dashboard", "About"]),
            (3.7, "Engine", "plain Python, fully tested", INK2, ["Daily runner", "Rules engine", "Mock responder", "Data quality"]),
            (6.9, "Google Sheet", "the only data store", TEAL, ["Invoices", "Audit_Log", "Run_Log", "Config"])]
    for x, head, sub, colr, chips in cols:
        box(s, x, 1.35, 2.6, 3.2, name=f"panel {head}")
        box(s, x, 1.35, 2.6, 0.85, colr, None, name=f"header {head}")
        text(s, x + 0.15, 1.42, 2.3, 0.4, head, 16, WHITE, bold=True)
        text(s, x + 0.15, 1.8, 2.3, 0.3, sub, 12, "DDE3EE")
        for i, chip in enumerate(chips):
            c = box(s, x + 0.15, 2.4 + i * 0.52, 2.3, 0.42, PAPER, LINE, radius=0.15, name=f"chip {chip}")
            label(c, chip, 14, INK)
    arrow(s, 3.15, 2.95, 3.65, 2.95)
    arrow(s, 6.35, 2.95, 6.85, 2.95, both=True)
    text(s, 0.5, 4.75, 9, 0.4, "People can also edit the sheet directly. The engine picks those edits up.", 14, MUTED)

    # 4 Data model --------------------------------------------------------
    s = new_slide(prs, "One sheet holds everything", notes=f"Your {n_old} original columns are never renamed, reordered or overwritten, and the Notes column is never written by the app. The app adds {n_new} columns on the right, and three tabs.")
    tabs = [("Invoices", f"{n_old} + {n_new}", "your columns, plus the app's", BLUE),
            ("Audit_Log", "1 row", "per attempt and decision", ORANGE),
            ("Run_Log", "1 row", "per run", AQUA),
            ("Config", "13", "settings you can edit", YELLOW)]
    for i, (name, big, sub, colr) in enumerate(tabs):
        x = 0.5 + i * 2.283
        box(s, x, 1.4, 2.15, 2.4, name=f"tab {name}")
        box(s, x, 1.4, 2.15, 0.5, colr, None, name=f"tab header {name}")
        text(s, x + 0.15, 1.4, 1.9, 0.5, name, 16, WHITE if colr in (BLUE, ORANGE) else INK, bold=True, anchor=MSO_ANCHOR.MIDDLE)
        text(s, x + 0.15, 2.1, 1.9, 0.8, big, 30, INK, bold=True, font=HEAD, anchor=MSO_ANCHOR.MIDDLE)
        text(s, x + 0.15, 2.95, 1.9, 0.7, sub, 14, MUTED)
    n = box(s, 0.5, 4.1, 9.0, 0.9, INK, None, name="promise")
    label(n, "Your original columns and the Notes column are never changed by the app.", 16, WHITE, bold=True)

    # 5 Daily run flow ----------------------------------------------------
    s = new_slide(prs, "What happens in a daily run", notes="The run reads the sheet once and writes in batches to stay inside Google's request limits. Every row decided on a date is stamped, so running again on the same date changes nothing.")
    steps = ["Check the sheet", "Check data quality", "Pick up sheet edits", "Contact who is due", "Apply the rules", "Write and log"]
    for i, step in enumerate(steps):
        col, row = i % 3, i // 3
        x, y = 0.5 + col * 3.05, 1.4 + row * 1.55
        box(s, x, y, 2.9, 1.3, name=f"step {i + 1}")
        circle(s, x + 0.2, y + 0.45, 0.42, AMBER if i == 3 else INK, str(i + 1), INK if i == 3 else WHITE)
        text(s, x + 0.8, y, 2.0, 1.3, step, 16, INK, bold=True, anchor=MSO_ANCHOR.MIDDLE)
    n = box(s, 0.5, 4.6, 9.0, 0.6, TEAL, None, name="idempotent")
    label(n, "Run it twice on the same day: the second run changes nothing.", 16, WHITE, bold=True)

    # 6 Outcome taxonomy --------------------------------------------------
    s = new_slide(prs, f"{len(Outcome)} possible outcomes", notes="Every attempt ends in exactly one of these outcomes, grouped by who or what produced it. Contact made: the person answered, from PAID to REFUSED_TO_PAY. No contact: nothing came back. Channel error: the email or phone route failed on the recipient's side, and it uses up an attempt. System error: our own failure, which never uses up an attempt. Escalation: set by the app itself when a row must go to a person. The About page lists every outcome with its meaning and what the app does next.")
    groups = [(Category.CONTACT_MADE, BLUE, WHITE, "uses an attempt"),
              (Category.NO_CONTACT, ORANGE, WHITE, "uses an attempt"),
              (Category.CHANNEL_ERROR, AQUA, INK, "uses an attempt"),
              (Category.SYSTEM_ERROR, YELLOW, INK, "no attempt used"),
              (Category.ESCALATION, GREY, INK, "set by the app")]
    for i, (cat, colr, fg, sub) in enumerate(groups):
        x = 0.5 + i * 1.825
        members = outcomes_in(cat)
        box(s, x, 1.3, 1.7, 3.5, name=f"group {cat.value}")
        box(s, x, 1.3, 1.7, 0.8, colr, None, name=f"group header {cat.value}")
        head = text(s, x + 0.1, 1.3, 1.5, 0.8, [f"{cat.value}: {len(members)}", sub], 14, fg, bold=True, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        for r in head.text_frame.paragraphs[1].runs:  # the small second line
            r.font.bold, r.font.size = False, Pt(12)
        items = text(s, x + 0.1, 2.22, 1.55, 2.5, [outcome_label(o) for o in members], 12, INK, name=f"outcomes {cat.value}")
        for p in items.text_frame.paragraphs:
            p.space_after = Pt(3)
    text(s, 0.5, 4.95, 9, 0.35, "System errors never use up an attempt. Escalations are set by the app, never by a contact.", 14, MUTED)

    # 7 Business rules ----------------------------------------------------
    s = new_slide(prs, f"{n_rules} business rules, all tested", notes=f"The About page lists all {n_rules} rules, and each names the code that implements it and the tests that prove it. A test fails if the page and the code drift apart. Plus {n_dq} data quality checks run before anything is contacted.")
    cards = [("Who and how often", ["Email first, then phone", "At most 5 attempts per channel", "One contact per day"], BLUE),
             ("When to wait", ["A date within 5 days is followed", "A later date needs approval", "At most 3 approvals per contact"], ORANGE),
             ("When a person decides", ["Disputes and refusals", "No channel left", "Rows that fail data quality"], RED),
             ("Always safe", ["Run twice, nothing changes", "Notes never written", "Money never added across currencies"], TEAL)]
    for i, (head, lines, colr) in enumerate(cards):
        col, row = i % 2, i // 2
        x, y = 0.5 + col * 4.6, 1.35 + row * 1.85
        box(s, x, y, 4.4, 1.7, name=f"rules {head}")
        circle(s, x + 0.2, y + 0.2, 0.3, colr)
        text(s, x + 0.65, y + 0.15, 3.6, 0.4, head, 16, INK, bold=True, anchor=MSO_ANCHOR.MIDDLE)
        text(s, x + 0.25, y + 0.7, 4.0, 0.95, lines, 14, MUTED)
    text(s, 0.5, 5.05, 9, 0.3, f"{n_rules} rules and {n_dq} data quality checks, covered by 594 automated tests.", 12, MUTED)

    # 8 Dashboard ---------------------------------------------------------
    s = new_slide(prs, "One page shows how it is going", notes="The dashboard filters by country and by customer, contact, email or invoice number, and drills down to one contact's full history. The chart shows the starting position of the 35-row file before any follow-up: 3 failed data quality, 1 already paid, 6 with a person, 25 in progress. They always add up to the total.")
    for i, k in enumerate(["Attempts", "Response rate", "Success rate", "Failure rate", "Sent to a person"]):
        box(s, 0.5, 1.35 + i * 0.72, 3.6, 0.6, name=f"kpi {k}")
        text(s, 0.7, 1.35 + i * 0.72, 3.3, 0.6, k, 16, INK, bold=True, anchor=MSO_ANCHOR.MIDDLE)
    text(s, 0.5, 5.0, 3.6, 0.5, "Filter by country and by contact. Drill down to one contact.", 12, MUTED)
    box(s, 4.4, 1.35, 5.1, 3.7, name="chart card")
    text(s, 4.6, 1.45, 4.7, 0.35, "Where the 35 rows start", 14, INK, bold=True)
    data = CategoryChartData()
    data.categories = ["In progress", "Needing human follow-up", "Data quality failed", "Handled"]
    data.add_series("Rows", (25, 6, 3, 1))
    gf = s.shapes.add_chart(XL_CHART_TYPE.BAR_CLUSTERED, Inches(4.5), Inches(1.85), Inches(4.9), Inches(2.6), data)
    gf.name = "chart rows by status"
    gf._element.nvGraphicFramePr.cNvPr.set("descr", "Bar chart of the 35 rows by status at the start: 25 in progress, 6 needing human follow-up, 3 failed data quality, 1 handled.")
    ch = gf.chart
    ch.has_legend, ch.has_title = False, False
    plot = ch.plots[0]
    plot.gap_width = 60
    plot.has_data_labels = True
    dl = plot.data_labels
    dl.position, dl.show_value = XL_LABEL_POSITION.OUTSIDE_END, True
    dl.font.size, dl.font.name, dl.font.color.rgb = Pt(14), BODY, rgb(INK)
    plot.series[0].format.fill.solid()
    plot.series[0].format.fill.fore_color.rgb = rgb(BLUE)
    ch.category_axis.reverse_order = True
    ch.category_axis.tick_labels.font.size, ch.category_axis.tick_labels.font.name = Pt(12), BODY
    ch.category_axis.tick_labels.font.color.rgb = rgb(INK)
    ch.category_axis.format.line.color.rgb = rgb(LINE)
    ch.category_axis.has_major_gridlines = False
    ch.value_axis.visible = False
    ch.value_axis.has_major_gridlines = False
    ch.value_axis.maximum_scale = 30
    text(s, 4.6, 4.55, 4.7, 0.4, "3 + 1 + 6 + 25 = 35. They always add up.", 14, TEAL, bold=True)

    # 9 How it was built --------------------------------------------------
    s = new_slide(prs, "Built one prompt at a time", notes="The tech stack (Streamlit, Google Sheets, Python) was chosen up front. One opening prompt asked the model to act as a prompt coach and write a set of separate prompts, each small enough that the context window never became a problem. Those prompts were saved in prompt-pack.md and run one by one, each building on the files the last one left behind. The deck itself was the twelfth prompt.")
    flow = [("Stack chosen first", "Streamlit, Google Sheets, Python"),
            ("One prompt to start", "Act as a prompt coach and split the build into small prompts"),
            ("A prompt pack", "prompt-pack.md: 12 prompts, run in order")]
    for i, (head, body) in enumerate(flow):
        x = 0.5 + i * 3.1
        box(s, x, 1.3, 2.8, 1.45, name=f"flow {head}")
        circle(s, x + 0.2, 1.45, 0.38, INK, str(i + 1), WHITE, 14)
        text(s, x + 0.7, 1.45, 2.0, 0.38, head, 14, INK, bold=True, anchor=MSO_ANCHOR.MIDDLE)
        text(s, x + 0.2, 1.95, 2.45, 0.75, body, 12, MUTED)
        if i < 2:
            arrow(s, x + 2.82, 2.02, x + 3.08, 2.02)
    prompts = ["Design spec", "Sheets layer", "Outcome taxonomy", "Data quality", "Rules engine", "Daily runner",
               "Run and queue pages", "Dashboard", "About page", "Final review", "Deploy guide", "This deck"]
    text(s, 0.5, 3.0, 9, 0.35, "The 12 prompts, in order", 14, INK, bold=True)
    for i, name in enumerate(prompts):
        col, row = i % 6, i // 6
        x, y = 0.5 + col * 1.5, 3.4 + row * 0.8
        box(s, x, y, 1.44, 0.7, AMBER if i == 0 else WHITE, None if i == 0 else LINE, radius=0.12, name=f"prompt {i + 1}")
        circle(s, x + 0.08, y + 0.2, 0.3, INK, str(i + 1), WHITE, 12)
        text(s, x + 0.43, y, 0.98, 0.7, name, 12, INK, bold=True, anchor=MSO_ANCHOR.MIDDLE)

    # 10 What it took -----------------------------------------------------
    s = new_slide(prs, "What the build took", notes="Figures are from the Claude Code usage screen for the build session: Claude Sonnet 5.5 with medium thinking effort, 2 hours 42 minutes of wall-clock time (1 hour 15 minutes of model time), 12,815 lines added across code, tests and documents, and a total cost of 22.85 US dollars. A cheaper Haiku model handled a few small helper tasks for under one cent.")
    stats = [("Sonnet 5.5", "model, medium thinking effort", BLUE),
             ("$22.85", "total cost of the session", ORANGE),
             ("2h 42m", "elapsed time, start to finish", AQUA),
             ("12,815", "lines added: code, tests, docs", YELLOW)]
    for i, (big, sub, colr) in enumerate(stats):
        x = 0.5 + i * 2.283
        box(s, x, 1.4, 2.15, 2.3, name=f"stat {sub}")
        box(s, x, 1.4, 2.15, 0.12, colr, None, shape=MSO_SHAPE.RECTANGLE, name=f"stat bar {sub}")
        text(s, x + 0.15, 1.75, 1.85, 0.8, big, 26, INK, bold=True, font=HEAD, anchor=MSO_ANCHOR.MIDDLE)
        text(s, x + 0.15, 2.65, 1.85, 0.9, sub, 14, MUTED)
    n = box(s, 0.5, 4.0, 9.0, 0.9, INK, None, name="build summary")
    label(n, f"One person, one session: a tested app with {n_rules} documented rules and a deck.", 16, WHITE, bold=True)

    # 11 Live --------------------------------------------------------------
    s = new_slide(prs, "It is online, ready to test", notes="The app is hosted on Streamlit Community Cloud from a private GitHub repository. It reads and writes a copy of the case-study Google Sheet held in the owner's Google account, so testing never touches the original. Anyone testing opens the address, enters the password the owner gives them, and uses the Run tab. The About tab explains how the app works and lists the outcome taxonomy.")
    box(s, 0.5, 1.3, 9.0, 0.7, WHITE, LINE, name="app address")
    text(s, 0.7, 1.3, 1.1, 0.7, "App address", 12, MUTED, bold=True, anchor=MSO_ANCHOR.MIDDLE)
    url = "https://invoice-followup-assistant-8rd6kozoxwh9hrxu9drpqb.streamlit.app/"
    tb = text(s, 1.8, 1.3, 7.6, 0.7, url, 14, INK, bold=True, anchor=MSO_ANCHOR.MIDDLE, name="app url")
    tb.text_frame.paragraphs[0].runs[0].hyperlink.address = url
    steps = [("Open the address", "Enter the password the owner shares"),
             ("Go to the Run tab", "Press Run follow-ups on the shared sheet"),
             ("Check the results", "Human Queue and Dashboard show what happened"),
             ("Read the About tab", "How it works, every rule, the outcome taxonomy")]
    for i, (head, body) in enumerate(steps):
        x = 0.5 + i * 2.283
        box(s, x, 2.3, 2.15, 1.9, name=f"test step {i + 1}")
        circle(s, x + 0.15, 2.45, 0.4, AMBER, str(i + 1), INK, 14)
        text(s, x + 0.15, 3.0, 1.85, 0.4, head, 14, INK, bold=True)
        text(s, x + 0.15, 3.4, 1.85, 0.75, body, 12, MUTED)
    n = box(s, 0.5, 4.5, 9.0, 0.6, TEAL, None, name="live facts")
    label(n, "Streamlit Community Cloud  |  private repo  |  password  |  a copy of the sheet", 14, WHITE, bold=True)

    # 12 Next steps -------------------------------------------------------
    s = new_slide(prs, "Next steps", dark=True, notes="The first two are the immediate path: have testers work through the app, then point it at the real sheet once the first-run checklist is clean. The last two would turn the simulation into the real thing; they are deliberately out of scope for this version.")
    nxt = [("Test it", "Testers run it on the sheet copy and report what they see"),
           ("Go live", "Point it at the real sheet when the first-run checklist is clean"),
           ("Send for real", "Connect an email service and a phone provider"),
           ("Automate", "A daily scheduler and balances from the finance system")]
    for i, (head, body) in enumerate(nxt):
        col, row = i % 2, i // 2
        x, y = 0.5 + col * 4.6, 1.4 + row * 1.85
        box(s, x, y, 4.4, 1.7, INK2, None, name=f"next {head}")
        circle(s, x + 0.25, y + 0.25, 0.42, AMBER if i < 2 else GREY, str(i + 1) if i < 2 else "+", INK)
        text(s, x + 0.85, y + 0.22, 3.4, 0.45, head, 18, WHITE, bold=True, anchor=MSO_ANCHOR.MIDDLE)
        text(s, x + 0.25, y + 0.85, 3.9, 0.8, body, 14, SOFT)

    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(out)
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    print("Saved", build(target))
