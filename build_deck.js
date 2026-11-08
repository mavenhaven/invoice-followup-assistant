const pptxgen = require("pptxgenjs");
// Build the architecture deck. Usage: npm install pptxgenjs, then: node build_deck.js
const fs = require("fs");
const path = require("path");
const JSZip = require(require.resolve("jszip", { paths: [require.resolve("pptxgenjs")] })); // ships with pptxgenjs

const OUT = path.join(__dirname, "Invoice-Followup-Architecture.pptx");

// pptxgenjs hard-codes Office's color palette; write this deck's own colors and name into the theme part.
// Also repairs <c:crossesAt val="autoZero"/>, which PowerPoint would offer to repair, and drops folder entries.
async function applyTheme(deckPath, theme) {
  const slots = ["dk1", "lt1", "dk2", "lt2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink"];
  for (const k of slots) {
    if (!/^[0-9A-Fa-f]{6}$/.test(String(theme.colors[k]))) throw new Error(`theme.colors.${k} must be six hex digits, no "#"`);
  }
  const name = theme.name.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const scheme = `<a:clrScheme name="${name}">` + slots.map((k) => `<a:${k}><a:srgbClr val="${theme.colors[k].toUpperCase()}"/></a:${k}>`).join("") + "</a:clrScheme>";
  const zip = await JSZip.loadAsync(fs.readFileSync(deckPath));
  const part = "ppt/theme/theme1.xml";
  const xml = await zip.file(part).async("string");
  zip.file(part, xml
    .replace(/<a:clrScheme\b[\s\S]*?<\/a:clrScheme>/, () => scheme)
    .replace(/(<a:(?:theme|fontScheme)\b[^>]*?\bname=")[^"]*"/g, (_, head) => `${head}${name}"`));
  for (const f of Object.keys(zip.files)) {
    if (!f.endsWith(".xml")) continue;
    const x = await zip.file(f).async("string");
    if (x.includes('<c:crossesAt val="autoZero"/>')) zip.file(f, x.replaceAll('<c:crossesAt val="autoZero"/>', '<c:crosses val="autoZero"/>'));
  }
  for (const f of Object.keys(zip.files)) if (zip.files[f].dir) delete zip.files[f];
  fs.writeFileSync(deckPath, await zip.generateAsync({ type: "nodebuffer", compression: "DEFLATE" }));
}

const INK = "14213D", INK2 = "1F3358", AMBER = "FCA311", TEAL = "2A9D8F", RED = "C8553D";
const PAPER = "F4F6F8", WHITE = "FFFFFF", MUTED = "5B6577", LINE = "D5DAE2";
const HEAD = "Cambria", BODY = "Calibri";

const THEME = {
  name: "Invoice Follow-Up",
  headFontFace: HEAD,
  bodyFontFace: BODY,
  colors: {
    dk1: INK, lt1: WHITE, dk2: INK2, lt2: PAPER,
    accent1: AMBER, accent2: TEAL, accent3: RED, accent4: "5C7AEA", accent5: "8D99AE", accent6: LINE,
    hlink: "1B6EC2", folHlink: "6B5B95",
  },
};

const pres = new pptxgen();
pres.layout = "LAYOUT_16x9"; // 10 x 5.625 in
pres.title = "Invoice Follow-Up Assistant: architecture and business rules";
pres.theme = { headFontFace: HEAD, bodyFontFace: BODY };

pres.defineSlideMaster({
  title: "CONTENT",
  background: { color: PAPER },
  slideNumber: { x: 9.0, y: 5.25, w: 0.5, h: 0.25, fontFace: BODY, fontSize: 10, color: MUTED, align: "right" },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: 0.5, y: 0.35, w: 9, h: 0.75, fontFace: HEAD, fontSize: 34, bold: true, color: INK, align: "left", valign: "middle", margin: 0 }, text: "" } },
  ],
});
pres.defineSlideMaster({
  title: "DARK",
  background: { color: INK },
  objects: [
    { placeholder: { options: { name: "title", type: "title", x: 0.5, y: 0.35, w: 9, h: 0.75, fontFace: HEAD, fontSize: 34, bold: true, color: WHITE, align: "left", valign: "middle", margin: 0 }, text: "" } },
  ],
});

const shadow = () => ({ type: "outer", color: "000000", opacity: 0.12, blur: 6, offset: 2, angle: 90 });

function card(slide, x, y, w, h, name, fill = WHITE) {
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, rectRadius: 0.08, fill: { color: fill }, line: { color: fill === WHITE ? LINE : fill, width: 0.75 }, shadow: fill === WHITE ? shadow() : undefined, objectName: name });
}
function txt(slide, text, opts) {
  slide.addText(text, Object.assign({ isTextBox: true, fontFace: BODY, color: INK, margin: 0, valign: "top" }, opts));
}
function badge(slide, n, x, y, fill, color = WHITE, d = 0.42) {
  slide.addShape(pres.shapes.OVAL, { x, y, w: d, h: d, fill: { color: fill }, line: { color: fill }, objectName: "badge " + n });
  txt(slide, String(n), { x, y, w: d, h: d, align: "center", valign: "middle", fontSize: 16, bold: true, color });
}
function arrow(slide, x1, y1, x2, y2, color = MUTED) {
  slide.addShape(pres.shapes.LINE, { x: Math.min(x1, x2), y: Math.min(y1, y2), w: Math.abs(x2 - x1), h: Math.abs(y2 - y1), flipH: x2 < x1, flipV: y2 < y1, line: { color, width: 2, endArrowType: "triangle" }, objectName: "arrow" });
}

// ---------- 1. Title ----------
{
  const s = pres.addSlide({ masterName: "DARK" });
  txt(s, "Invoice Follow-Up Assistant", { x: 0.5, y: 1.5, w: 5.6, h: 1.5, fontFace: HEAD, fontSize: 44, bold: true, color: WHITE, valign: "top" });
  txt(s, "Architecture and business rules", { x: 0.5, y: 3.2, w: 5.6, h: 0.5, fontSize: 22, color: AMBER });
  txt(s, "Streamlit  |  Google Sheets  |  Streamlit Community Cloud", { x: 0.5, y: 4.5, w: 6, h: 0.35, fontSize: 14, color: "AEB8CC" });
  // motif: stack of "invoice rows"
  const rows = [[TEAL, "PAID"], [AMBER, "PROMISE_TO_PAY"], [RED, "NEEDS HUMAN"]];
  rows.forEach(([c, label], i) => {
    const y = 1.5 + i * 1.05;
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 6.5, y, w: 3.0, h: 0.8, rectRadius: 0.1, fill: { color: INK2 }, line: { color: INK2 }, objectName: "row " + label });
    s.addShape(pres.shapes.OVAL, { x: 6.7, y: y + 0.24, w: 0.32, h: 0.32, fill: { color: c }, line: { color: c }, objectName: "dot " + label });
    txt(s, label, { x: 7.2, y, w: 2.2, h: 0.8, valign: "middle", fontSize: 14, bold: true, color: WHITE });
  });
  s.addNotes("Purpose: automate follow-up on overdue invoices with email first, then phone, and escalate to a human whenever the rules say so. All data stays in one Google Sheet.");
}

// ---------- 2. Problem and goal ----------
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  s.addText("Chase overdue invoices", { placeholder: "title" });
  txt(s, "Contacts get follow-ups until the invoice is paid or no longer needs one. Anything the app cannot handle goes to a person.", { x: 0.5, y: 1.2, w: 9, h: 0.65, fontSize: 16, color: MUTED });
  const stats = [
    ["35", "invoice rows in the sample file"],
    ["5", "countries: AT, CZ, DE, HU, RO"],
    ["4", "currencies: EUR, CZK, HUF, RON"],
    ["13", "rows already have a recorded outcome"],
  ];
  const w = 2.05, gap = 0.2;
  stats.forEach(([n, label], i) => {
    const x = 0.5 + i * (w + gap);
    card(s, x, 2.1, w, 2.0, "stat card " + (i + 1));
    txt(s, n, { x: x + 0.2, y: 2.3, w: w - 0.4, h: 0.9, fontFace: HEAD, fontSize: 56, bold: true, color: i % 2 ? TEAL : INK });
    txt(s, label, { x: x + 0.2, y: 3.3, w: w - 0.4, h: 0.7, fontSize: 14, color: MUTED });
  });
  txt(s, "Single source of truth: one Google Sheet holds the data, audit trail, run log and status. No other data store.", { x: 0.5, y: 4.4, w: 9, h: 0.6, fontSize: 14, bold: true, color: INK });
  s.addNotes("The sample file is the case-study export. Outstanding amounts are in four currencies and are never added together.");
}

// ---------- 3. Architecture ----------
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  s.addText("Architecture", { placeholder: "title" });
  const cols = [
    { x: 0.5, head: "Streamlit app", sub: "Streamlit Community Cloud", fill: INK, chips: ["Run", "Human Queue", "Dashboard (Plotly)", "About"] },
    { x: 3.7, head: "core/ (pure Python)", sub: "No Streamlit imports, fully tested", fill: INK2, chips: ["quality", "rules", "taxonomy", "mock_responses", "runner", "metrics"] },
    { x: 6.9, head: "Google Sheet", sub: "The only data store", fill: TEAL, chips: ["Invoices (18 + new columns)", "Audit_Log", "Run_Log", "Config"] },
  ];
  cols.forEach((c, ci) => {
    card(s, c.x, 1.35, 2.6, 3.4, "box " + c.head, WHITE);
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: c.x, y: 1.35, w: 2.6, h: 0.8, rectRadius: 0.08, fill: { color: c.fill }, line: { color: c.fill }, objectName: "header " + c.head });
    txt(s, c.head, { x: c.x + 0.15, y: 1.42, w: 2.3, h: 0.35, fontSize: 16, bold: true, color: WHITE });
    txt(s, c.sub, { x: c.x + 0.15, y: 1.77, w: 2.3, h: 0.3, fontSize: 12, color: "DDE3EE" });
    c.chips.forEach((chip, i) => {
      const y = 2.3 + i * 0.4;
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: c.x + 0.15, y, w: 2.3, h: 0.34, rectRadius: 0.06, fill: { color: PAPER }, line: { color: LINE, width: 0.75 }, objectName: "chip " + chip });
      txt(s, chip, { x: c.x + 0.25, y, w: 2.1, h: 0.34, valign: "middle", fontSize: 13, color: INK });
    });
  });
  arrow(s, 3.15, 3.05, 3.65, 3.05);
  arrow(s, 6.35, 3.05, 6.85, 3.05);
  txt(s, "Reads and batch-writes through gspread with a service account kept in Streamlit secrets. Every attempt is appended to Audit_Log.", { x: 0.5, y: 4.9, w: 8.4, h: 0.4, fontSize: 12, color: MUTED });
  s.addNotes("The app has four pages. Business logic lives in core/ so it can be unit tested without Streamlit. The service account needs Editor access to the sheet.");
}

// ---------- 4. Daily run flow ----------
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  s.addText("What happens in a daily run", { placeholder: "title" });
  const steps = [
    ["Load", "Read every invoice row. Skip rows already processed today."],
    ["Check data", "Run the data quality rules. Bad rows go to the human queue."],
    ["Decide", "Rules engine picks email, phone, skip or escalate."],
    ["Contact", "Mock email reply or phone transcript with a varied outcome."],
    ["Apply outcome", "Update counters, next follow-up date and status."],
    ["Write back", "Batch update the row and append to Audit_Log and Run_Log."],
  ];
  const w = 2.8, h = 1.65, gap = 0.3;
  steps.forEach(([title, desc], i) => {
    const col = i % 3, row = Math.floor(i / 3);
    const x = 0.5 + col * (w + gap), y = 1.3 + row * (h + 0.25);
    card(s, x, y, w, h, "step " + (i + 1));
    badge(s, i + 1, x + 0.2, y + 0.2, i === 3 ? AMBER : INK, i === 3 ? INK : WHITE);
    txt(s, title, { x: x + 0.75, y: y + 0.2, w: w - 0.95, h: 0.42, valign: "middle", fontSize: 18, bold: true });
    txt(s, desc, { x: x + 0.2, y: y + 0.78, w: w - 0.4, h: 0.8, fontSize: 14, color: MUTED });
  });
  s.addNotes("Running the app twice on the same day changes nothing: a row with last_processed_date equal to today is skipped. A failure on one row is logged and the run continues.");
}

// ---------- 5. Data quality ----------
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  s.addText("Data quality on the real file", { placeholder: "title" });
  card(s, 0.5, 1.3, 4.7, 3.7, "chart card");
  s.addChart(pres.charts.BAR, [{
    name: "Rows",
    labels: ["Blocked (BLOCK)", "Email unusable", "Not contacted (Ready = No)", "Phone fixed", "Ready flag blank", "Tiny amount", "Already paid"],
    values: [3, 3, 2, 1, 1, 1, 1],
  }], {
    x: 0.6, y: 1.4, w: 4.5, h: 3.5, barDir: "bar", chartColors: [TEAL],
    showTitle: true, title: "Rows affected, 35 rows checked", titleFontSize: 12, titleColor: MUTED, titleFontFace: "+mn-lt",
    showValue: true, dataLabelPosition: "outEnd", dataLabelColor: INK, dataLabelFontSize: 11, dataLabelFontFace: "+mn-lt",
    catAxisLabelColor: INK, catAxisLabelFontSize: 11, catAxisLabelFontFace: "+mn-lt", catAxisOrientation: "maxMin", catGridLine: { style: "none" },
    valAxisHidden: true, valGridLine: { style: "none" }, showLegend: false, barGapWidthPct: 40,
  });
  const sev = [
    ["BLOCK", RED, "Row cannot be processed. It goes to Human Queue A and is never contacted. Examples: duplicate invoice, credit note, no email and no phone."],
    ["WARN", AMBER, "Row is still processed and the issue is shown. Examples: invalid email, phone without country code, blank flag."],
    ["INFO", TEAL, "Noted only. Examples: on hold, already paid."],
  ];
  const hs = [1.3, 1.15, 1.0];
  let y = 1.3;
  sev.forEach(([label, col, text], i) => {
    card(s, 5.4, y, 4.1, hs[i], "severity " + label);
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 5.55, y: y + 0.15, w: 0.9, h: 0.3, rectRadius: 0.06, fill: { color: col }, line: { color: col }, objectName: "tag " + label });
    txt(s, label, { x: 5.55, y: y + 0.15, w: 0.9, h: 0.3, align: "center", valign: "middle", fontSize: 12, bold: true, color: label === "WARN" ? INK : WHITE });
    txt(s, text, { x: 5.55, y: y + 0.52, w: 3.8, h: hs[i] - 0.58, fontSize: 12, color: INK });
    y += hs[i] + 0.15;
  });
  s.addNotes("18 rules, DQ-01 to DQ-18. Counts are from profiling the 35-row CSV. The two Ready = No rows are INV-2026-3980 (insolvency) and INV-2026-4415 (on hold).");
}

// ---------- 6. Contact rules ----------
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  s.addText("Business rules: who we contact and how", { placeholder: "title" });
  const rules = [
    ["Email first", "Email is cheaper, so every row starts there when it has a valid address.", TEAL],
    ["Five attempts per channel", "After 5 email attempts with no real response, switch to phone. Phone is capped at 5 too.", INK],
    ["Then a human takes over", "Both channels at 5 with no response: mark NEEDING HUMAN FOLLOW UP.", RED],
    ["Once per contact per day", "One combined message covers all of a contact's invoices. A re-run skips rows done today.", AMBER],
  ];
  const w = 4.4, h = 1.75, gx = 0.2, gy = 0.2;
  rules.forEach(([title, text, col], i) => {
    const x = 0.5 + (i % 2) * (w + gx), y = 1.3 + Math.floor(i / 2) * (h + gy);
    card(s, x, y, w, h, "rule " + (i + 1));
    badge(s, i + 1, x + 0.2, y + 0.2, col, col === AMBER ? INK : WHITE);
    txt(s, title, { x: x + 0.75, y: y + 0.2, w: w - 0.95, h: 0.42, valign: "middle", fontSize: 18, bold: true });
    txt(s, text, { x: x + 0.2, y: y + 0.8, w: w - 0.4, h: 0.85, fontSize: 14, color: MUTED });
  });
  s.addNotes("Every attempt counts, including bounces and busy lines. A hard bounce disables email for that row. Several invoices for one contact (for example Veltner Gastro has three) are handled in one contact per day.");
}

// ---------- 7. Follow-up date rules ----------
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  s.addText("Business rules: follow-up dates", { placeholder: "title" });
  const box = (x, y, w, h, fill, color, text, name) => {
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, rectRadius: 0.08, fill: { color: fill }, line: { color: fill }, shadow: shadow(), objectName: name });
    txt(s, text, { x: x + 0.15, y, w: w - 0.3, h, valign: "middle", fontSize: 14, bold: true, color });
  };
  box(0.5, 1.7, 2.4, 1.2, INK, WHITE, "Contact states when they will pay", "start");
  box(3.5, 1.3, 2.8, 1.15, TEAL, WHITE, "5 days or fewer: follow up on the date they gave", "branch short");
  box(3.5, 2.75, 2.8, 1.15, AMBER, INK, "More than 5 days: a human must approve", "branch long");
  box(6.9, 2.75, 2.6, 1.15, RED, WHITE, "After 3 approvals the next request means NEEDING HUMAN FOLLOW UP", "limit");
  arrow(s, 2.95, 2.3, 3.45, 1.9);
  arrow(s, 2.95, 2.3, 3.45, 3.3);
  arrow(s, 6.35, 3.32, 6.85, 3.32);
  const small = [
    ["Paid or closed", "Counts as handled and is never contacted."],
    ["Ready for Contact = No", "Needs a human and is never contacted. The Notes text is the reason."],
  ];
  small.forEach(([t, d], i) => {
    const x = 0.5 + i * 4.6;
    card(s, x, 4.1, 4.4, 1.0, "exception " + (i + 1));
    txt(s, t, { x: x + 0.2, y: 4.2, w: 4.0, h: 0.3, fontSize: 14, bold: true });
    txt(s, d, { x: x + 0.2, y: 4.52, w: 4.0, h: 0.5, fontSize: 13, color: MUTED });
  });
  s.addNotes("Example: INV-2026-4233 promised payment on 31 Aug after contact on 18 Aug. That is 13 days, so it needs human approval. The 3-approval cap stops endless extensions for one contact.");
}

// ---------- 8. Outcome taxonomy ----------
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  s.addText("Outcome taxonomy", { placeholder: "title" });
  const cats = [
    ["Contact made", TEAL, WHITE, "PAID · PARTIAL_PAYMENT · PROMISE_TO_PAY · PAYMENT_IN_PROCESS · SETTLEMENT_REQUESTED · CALLBACK_REQUESTED · DISPUTE · REQUESTS_INVOICE_COPY · WRONG_CONTACT · REFUSED_TO_PAY"],
    ["No contact", INK, WHITE, "NO_ANSWER · OUT_OF_OFFICE · LEFT_VOICEMAIL"],
    ["Channel and system errors", AMBER, INK, "EMAIL_BOUNCE_HARD · EMAIL_BOUNCE_SOFT · EMAIL_INVALID_ADDRESS · EMAIL_SEND_FAILURE · PHONE_INVALID_NUMBER · PHONE_LINE_BUSY · PHONE_DISCONNECTED · PHONE_SYSTEM_ERROR · SYSTEM_ERROR"],
    ["Escalation", RED, WHITE, "NEEDS_HUMAN_FOLLOW_UP · MAX_ATTEMPTS_REACHED · DQ_FAILED"],
  ];
  const w = 4.4, h = 1.8, gx = 0.2, gy = 0.2;
  cats.forEach(([title, fill, fc, codes], i) => {
    const x = 0.5 + (i % 2) * (w + gx), y = 1.3 + Math.floor(i / 2) * (h + gy);
    card(s, x, y, w, h, "taxonomy " + title);
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h: 0.45, rectRadius: 0.08, fill: { color: fill }, line: { color: fill }, objectName: "taxonomy head " + title });
    txt(s, title, { x: x + 0.2, y, w: w - 0.4, h: 0.45, valign: "middle", fontSize: 15, bold: true, color: fc });
    txt(s, codes, { x: x + 0.2, y: y + 0.55, w: w - 0.4, h: h - 0.65, fontSize: 12, color: INK });
  });
  s.addNotes("Existing sheet values are kept: PAID, PARTIAL_PAYMENT, PROMISE_TO_PAY, SETTLEMENT_REQUESTED, CALLBACK_REQUESTED, DISPUTE, WRONG_CONTACT, OUT_OF_OFFICE, NO_ANSWER. Email and phone error codes are new, so system failures are tracked apart from vendors not responding. The mock responder can produce every code.");
}

// ---------- 9. Dashboard ----------
{
  const s = pres.addSlide({ masterName: "CONTENT" });
  s.addText("Dashboard and reconciliation", { placeholder: "title" });
  const parts = [["Data quality failed", RED, WHITE], ["Needing human follow-up", AMBER, INK], ["In progress", INK, WHITE], ["Handled", TEAL, WHITE]];
  const bw = 1.65, op = 0.35;
  let x = 0.5;
  parts.forEach(([label, fill, fc], i) => {
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 1.4, w: bw, h: 1.25, rectRadius: 0.08, fill: { color: fill }, line: { color: fill }, shadow: shadow(), objectName: "bucket " + label });
    txt(s, label, { x: x + 0.1, y: 1.4, w: bw - 0.2, h: 1.25, align: "center", valign: "middle", fontSize: 14, bold: true, color: fc });
    x += bw;
    txt(s, i < 3 ? "+" : "=", { x, y: 1.4, w: op, h: 1.25, align: "center", valign: "middle", fontSize: 22, bold: true, color: MUTED });
    x += op;
  });
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 1.4, w: 1.0, h: 1.25, rectRadius: 0.08, fill: { color: WHITE }, line: { color: INK, width: 1.5 }, objectName: "bucket total" });
  txt(s, "Total rows", { x, y: 1.4, w: 1.0, h: 1.25, align: "center", valign: "middle", fontSize: 14, bold: true });
  txt(s, "The four counts must add up to the total. The page warns if they do not.", { x: 0.5, y: 2.8, w: 9, h: 0.35, fontSize: 14, color: MUTED });
  const feats = [
    ["Rates", "Attempts, responses, success and failure rates, including contact not made and human flags."],
    ["Filters", "Filter by country. Search by customer, contact, email or invoice number."],
    ["Drill-down", "Every follow-up date, channel and outcome for a contact, plus themes in their replies."],
  ];
  const w = 2.9, gap = 0.15;
  feats.forEach(([t, d], i) => {
    const fx = 0.5 + i * (w + gap);
    card(s, fx, 3.3, w, 1.75, "feature " + t);
    txt(s, t, { x: fx + 0.2, y: 3.45, w: w - 0.4, h: 0.35, fontSize: 16, bold: true });
    txt(s, d, { x: fx + 0.2, y: 3.85, w: w - 0.4, h: 1.1, fontSize: 13, color: MUTED });
  });
  s.addNotes("Plotly charts: attempts per day, outcome mix by category, channel comparison, success by country. Outstanding amounts are grouped by currency and never summed across currencies.");
}

// ---------- 10. Build and deploy ----------
{
  const s = pres.addSlide({ masterName: "DARK" });
  s.addText("Build, deploy, test", { placeholder: "title" });
  const steps = [
    ["Build", "Twelve prompts, one chat each. Design spec first, then the code."],
    ["Share the sheet", "Create a Google service account and give it Editor access."],
    ["Deploy", "Push to GitHub, create the app on Streamlit Community Cloud, add secrets."],
    ["Test", "Run it twice to prove nothing repeats, and exercise every outcome."],
  ];
  const w = 2.1, gap = 0.2;
  steps.forEach(([t, d], i) => {
    const x = 0.5 + i * (w + gap);
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 1.5, w, h: 2.8, rectRadius: 0.1, fill: { color: INK2 }, line: { color: INK2 }, objectName: "deploy step " + (i + 1) });
    badge(s, i + 1, x + 0.2, 1.7, AMBER, INK);
    txt(s, t, { x: x + 0.2, y: 2.25, w: w - 0.4, h: 0.4, fontSize: 18, bold: true, color: WHITE });
    txt(s, d, { x: x + 0.2, y: 2.75, w: w - 0.4, h: 1.45, fontSize: 14, color: "C9D2E3" });
  });
  txt(s, "Next step: run prompt 1 to produce the design spec.", { x: 0.5, y: 4.6, w: 9, h: 0.4, fontSize: 14, color: AMBER });
  s.addNotes("Prompts are in prompt-pack.md. Deployment instructions are produced by prompt 11 once the app is built.");
}

(async () => {
  await pres.writeFile({ fileName: OUT });
  await applyTheme(OUT, THEME);
  console.log("written", OUT);
})();
