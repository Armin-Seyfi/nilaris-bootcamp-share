"""Authenticated Reviewer for the three pinned Case_001 sources."""
import base64
from dataclasses import dataclass
from hashlib import sha256
from html import escape
from http.server import HTTPServer
from io import BytesIO
import json
from pathlib import Path
import re
import secrets
import subprocess
import signal
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from nilaris.rfq_stage_decision import STAGES, StageDecision, StaleDecision
from nilaris.review_http import ReviewHTTPHandler, reviewer_credential

ASSETS = Path(__file__).with_name("review_web")
EXTRACTOR = Path(__file__).resolve().parents[2] / "tools/rfq/joint-evidence.swift"
PREFIX = "/rfq/case-001"
SCENARIO = ("Case_001", "historical procurement")
HISTORICAL_SOURCES_UNAVAILABLE = "Historical Case_001 source documents are not distributed in the bootcamp snapshot."
PINS = {}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass(frozen=True)
class Source:
    name: str
    digest: str
    data: bytes

    @property
    def url(self):
        return f"{PREFIX}/{self.digest}"


def load_source(directory, role):
    if role not in PINS:
        raise ValueError(HISTORICAL_SOURCES_UNAVAILABLE)
    name, digest = PINS[role]
    data = (directory / name).read_bytes()
    if sha256(data).hexdigest() != digest:
        raise ValueError(f"The selected {role} revision differs from the approved Case_001 source.")
    return Source(name, digest, data)


def worksheet(data):
    """Read the worksheet selected by its workbook name and relationship."""
    main = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    with ZipFile(BytesIO(data)) as book:
        root = ET.fromstring(book.read("xl/workbook.xml"))
        sheet = root.find(f"{main}sheets/{main}sheet[@name='Sheet1']")
        relationships = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
        target = next(r.attrib["Target"] for r in relationships if r.attrib["Id"] == sheet.attrib[rel])
        path = target.lstrip("/") if target.startswith("/") else "xl/" + target
        strings = ET.fromstring(book.read("xl/sharedStrings.xml"))
        shared = ["".join(t.text or "" for t in entry.iter(main + "t")) for entry in strings]
        cells = {}
        for cell in ET.fromstring(book.read(path)).iter(main + "c"):
            value = cell.findtext(main + "v", "")
            cells[cell.attrib["r"]] = shared[int(value)] if cell.get("t") == "s" else value
    return cells


def pdf_evidence(source, role):
    extracted = subprocess.run(["/usr/bin/swift", str(EXTRACTOR), role], input=source.data,
                               stdout=subprocess.PIPE, check=True, timeout=90)
    result = json.loads(extracted.stdout)
    result["image"] = base64.b64decode(result["image"], validate=True)
    return result


def page(title, content, css=""):
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{escape(title)} · NILARIS Reviewer</title>"
            f"<link rel='stylesheet' href='/rfq/style.css'>{css}</head>"
            f"<body><header><strong>NILARIS</strong><span>Reviewer</span></header>"
            f"<main><h1>{escape(title)}</h1>{content}</main></body></html>").encode()


def link(url, label):
    return f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'


def source_heading(source, location):
    return (f"<p>{escape(source.name)} · {escape(location)}</p>"
            f"<p class='revision'>SHA-256 {source.digest}</p>")


def workbook_view(source, cells):
    rows = []
    for number in (1, 2):
        values = []
        for col in "ABCDE":
            addr = f"{col}{number}"
            value = escape(cells[addr])
            if addr == "E2":
                value = f"<mark id='E2'>{value}</mark>"
            values.append(f"<td>{value}</td>")
        rows.append(f"<tr><th scope='row'>{number}</th>{''.join(values)}</tr>")
    headings = "".join(f"<th scope='col'>{col}</th>" for col in "ABCDE")
    return page("Sheet1!E2", source_heading(source, "Historical RFQ · Sheet1") +
                "<div class='table-scroll'><table><thead><tr><td></td>" + headings +
                "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div><p>" +
                link(source.url + "/original.xlsx", "Download original workbook") + "</p>")


def pdf_fragment(evidence, passage):
    x, y, _, _ = passage["bounds"]
    width, height = evidence["size"]
    return f"page=1&zoom=180,{x * width * 96 / 72:.2f},{y * height * 96 / 72:.2f}"


def add_pdf_view(routes, source, evidence, field, location, passage):
    x, y, width, height = passage["bounds"]
    marker = (f"#evidence-row{{left:{x:.8%};top:{y:.8%};width:{width:.8%};height:{height:.8%}}}")
    prefix = source.url + "/" + field
    original = source.url + "/original.pdf#" + pdf_fragment(evidence, passage)
    content = (source_heading(source, location) + f"<pre>{escape(passage['text'])}</pre><p>" +
               link(original, "Open original PDF at passage") +
               f"</p><figure><img src='{source.url}/page.png' alt='Original page 1'>"
               "<span id='evidence-row' aria-label='Selected passage'></span></figure>")
    routes[prefix + "/"] = ("text/html; charset=utf-8",
                             page(location, content, f"<link rel='stylesheet' href='{prefix}/mark.css'>"))
    routes[prefix + "/mark.css"] = ("text/css", marker.encode())
    return link(prefix + "/#evidence-row", location)


def normalized_words(text):
    return re.sub(r"\bdia\b\.?", "diameter", " ".join(text.lower().split()))


@dataclass(frozen=True)
class OfferMeasurement:
    name: str
    value: str
    passage: dict


def offer_measurements(passages, other_sources):
    """Compare printed item fields against the RFQ and referenced datasheet."""
    other = normalized_words(other_sources)
    flow = None
    diameters = []
    for passage in passages:
        label, separator, value = passage["text"].partition(":")
        if not separator or not value.strip():
            continue
        name = normalized_words(label)
        if name == "discharge flow":
            if flow is not None:
                raise ValueError("The offer item has multiple discharge-flow entries.")
            flow = passage
        # This slice asks for offer-only diameter measurements. The part name,
        # measured value, original label and locator all come from the item.
        if (re.search(r"\bdiameter\b", name)
                and re.fullmatch(r"\s*\d+(?:[.,]\d+)?\s*(?:mm|cm|m|in)\s*", value, re.I)
                and not re.search(r"\b" + re.escape(name) + r"\b", other)):
            diameters.append(OfferMeasurement(name.capitalize(), value.strip(), passage))
    if flow is None:
        raise ValueError("The selected offer item has no discharge-flow entry.")
    return flow, diameters


def stage_form(endpoint):
    boxes = "".join(f'<label><input type="checkbox" value="{code}"> {label}</label>'
                    for code, label in STAGES.items())
    return (f'<form data-decision="{endpoint}"><fieldset disabled><legend>Required procurement stages</legend>'
            f'{boxes}<p>Save with all stages unchecked to record Optional throughout.</p>'
            '<button>Save draft</button></fieldset><p role="status">Loading stage decision…</p></form>')


@dataclass
class CaseEvidence:
    book: Source
    sheet: Source
    offer: Source
    cells: dict
    product: str
    data: dict
    bid: dict
    flow: dict
    proposals: list


def inspect_case(directory):
    book = load_source(directory, "workbook")
    sheet = load_source(directory, "datasheet")
    offer = load_source(directory, "offer")
    cells = worksheet(book.data)
    description = cells["E2"]
    product, separator, reference = description.partition(";")
    if not separator or "datasheet" not in reference.casefold():
        raise ValueError("The RFQ description has no datasheet reference.")
    data = pdf_evidence(sheet, "datasheet")
    bid = pdf_evidence(offer, "offer")
    prior = "\n".join([*cells.values(), data["pageText"]])
    flow, proposals = offer_measurements(bid["passages"], prior)
    return CaseEvidence(book, sheet, offer, cells, product.strip(), data, bid, flow, proposals)


def build_review(directory, decision_directory):
    case = inspect_case(directory)
    book, sheet, offer = case.book, case.sheet, case.offer
    cells, product, description = case.cells, case.product, case.cells["E2"]
    data, bid, flow, proposals = case.data, case.bid, case.flow, case.proposals
    sheet_flow, = data["passages"]

    workbook_path = book.url + "/Sheet1/E2"
    routes = {workbook_path: ("text/html; charset=utf-8", workbook_view(book, cells)),
              book.url + "/original.xlsx": (XLSX, book.data)}
    for source, evidence in ((sheet, data), (offer, bid)):
        routes[source.url + "/original.pdf"] = ("application/pdf", source.data)
        routes[source.url + "/page.png"] = ("image/png", evidence["image"])

    sheet_link = add_pdf_view(routes, sheet, data, "flow", "Page 1 · first equipment row, flow and gland pressure",
                             sheet_flow)
    flow_link = add_pdf_view(routes, offer, bid, "flow", "Page 1 · Scope of Supply, item 1, discharge flow", flow)
    attributes = []
    for number, measurement in enumerate(proposals, start=1):
        location = "Page 1 · Scope of Supply, item 1, " + measurement.name.lower()
        view = add_pdf_view(routes, offer, bid, f"measurement-{number}", location, measurement.passage)
        identity = json.dumps([*SCENARIO, product.strip(), measurement.name,
                               book.digest, sheet.digest, offer.digest])
        digest = sha256(identity.encode()).hexdigest()
        endpoint = f"{PREFIX}/decisions/{digest}"
        routes[endpoint] = StageDecision(decision_directory, digest)
        stage_view = stage_form(endpoint)
        attributes.append(f"<h3>{escape(measurement.name)}</h3>"
                          f"<p>Observed value: <strong>{escape(measurement.value)}</strong></p>"
                          "<p>Observed only in the supplier offer within these three documents.</p>"
                          f"{view}{stage_view}")
    # PDFKit returns baseline "3" in its plain text. Keep the full extracted
    # passage and provide the normalized measurement separately.
    raw_flow = sheet_flow["text"]
    measurement = re.match(r"([\d.]+)\s+(m[3³]/h)\b", raw_flow)
    if measurement is None:
        raise ValueError("The datasheet passage has no readable volumetric flow.")
    displayed_flow = measurement[0].replace("m3/h", "m³/h")
    content = (f"<p>{escape(product.strip())}</p><p>Procurement scenario: {' '.join(SCENARIO)}</p><h2>Historical RFQ workbook</h2><p>{escape(book.name)}</p>"
               f"<p>{escape(description)}</p>{link(workbook_path + '#E2', 'Open Sheet1!E2')}"
               f"<h2>Referenced datasheet</h2><p>{escape(sheet.name)}</p>"
               f"<p>Flow: {escape(displayed_flow)}</p><pre>{escape(raw_flow)}</pre>{sheet_link}"
               f"<h2>Supplier offer</h2><p>{escape(offer.name)}</p>"
               f"<pre>{escape(flow['text'])}</pre>{flow_link}"
               "<h2>Proposed Attribute</h2>" + "".join(attributes)
               + '<script src="/rfq/joint-stages.js" defer></script>')
    routes[PREFIX + "/"] = ("text/html; charset=utf-8", page("Product Type Schema draft", content))
    return routes


LOGIN = page("Local Reviewer", """
<form id="login"><label for="key">Local Reviewer key</label>
<input id="key" type="password" autocomplete="off" required>
<button>Open draft</button><p id="status" role="status"></p></form>
<script src="/rfq/joint-login.js" defer></script>""")
LOGIN_JS = b"""document.querySelector('form').addEventListener('submit', async event => {
  event.preventDefault();
  const key = document.querySelector('#key');
  try {
    const response = await fetch('/rfq/session', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token: key.value})});
    key.value = '';
    if (response.ok) { location.assign('/rfq/case-001/'); return; }
    document.querySelector('#status').textContent = 'The key was not accepted.';
  } catch { document.querySelector('#status').textContent = 'The Reviewer is unavailable.'; }
});"""


def reviewer(routes, credential, port):
    public = {"/": ("text/html; charset=utf-8", LOGIN),
              "/rfq/joint-stages.js": ("text/javascript", (ASSETS / "joint_stages.js").read_bytes()),
              "/rfq/joint-login.js": ("text/javascript", LOGIN_JS),
              "/rfq/style.css": ("text/css", (ASSETS / "rfq_demo.css").read_bytes())}

    class Handler(ReviewHTTPHandler):
        cookie_name = "nilaris_joint_reviewer"
        session_token = secrets.token_urlsafe(32)

        @property
        def expected_host(self):
            return f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.host_valid():
                return self.reply(403, {"category": "invalid_host"})
            asset = public.get(self.path)
            if asset is None:
                if not self.authenticated():
                    return self.reply(401, {"category": "unauthorized"})
                asset = routes.get(self.path)
            if asset is None:
                return self.reply(404, {"category": "not_found"})
            if isinstance(asset, StageDecision):
                return self.reply(200, asset.read())
            mime, raw = asset
            self.reply(200, raw, mime)

        def do_POST(self):
            body = self.same_origin_json()
            if body is None:
                return
            if self.path == "/rfq/session":
                return self.login(body, credential)
            if not self.authenticated():
                return self.reply(401, {"category": "unauthorized"})
            decision = routes.get(self.path)
            if not isinstance(decision, StageDecision):
                return self.reply(404, {"category": "not_found"})
            try:
                result = decision.save(body)
            except StaleDecision:
                return self.reply(409, {"category": "stale_decision"})
            except ValueError:
                return self.reply(400, {"category": "invalid_decision"})
            return self.reply(200, result)

    return HTTPServer(("127.0.0.1", port), Handler)


def serve_for(server, seconds):
    # A synchronous handler can be inside a socket read at the session deadline.
    # BaseException crosses HTTPServer's per-request exception handler; the
    # server closes the accepted socket before unwinding the credential context.
    class SessionExpired(BaseException):
        pass

    def expire(_signum, _frame):
        raise SessionExpired

    old_handler = signal.signal(signal.SIGALRM, expire)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        server.serve_forever()
    except (SessionExpired, KeyboardInterrupt):
        pass
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def main():
    raise SystemExit(HISTORICAL_SOURCES_UNAVAILABLE)


if __name__ == "__main__":
    main()
