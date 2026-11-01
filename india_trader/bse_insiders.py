"""Read-only BSE PIT XBRL search and inline transaction facts."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .core import IST

PAGE = "https://beta.bseindia.com/corporates/xbrldetails.aspx"
MAX_HTML = 3_000_000
MAX_DAYS = 366


class BseError(ValueError):
    pass


def bse_code(raw):
    code = str(raw).strip().upper().removesuffix(".BO")
    if not re.fullmatch(r"\d{6}", code):
        raise BseError("BSE PIT search needs a six-digit BSE scrip code")
    return code


def filing_url(raw):
    p = urllib.parse.urlsplit(str(raw or ""))
    if p.scheme != "https" or p.hostname not in ("www.bseindia.com", "beta.bseindia.com") or \
            not re.fullmatch(r"/XBRLFILES/PITXBRLDataXML/PIT_\d{6}_[A-Za-z0-9_]+\.html", p.path, re.I) or \
            p.query or p.fragment:
        raise BseError("unsafe BSE PIT filing URL")
    return p.geturl()


class HiddenFields(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("type", "").lower() == "hidden" and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value", "")


class ResultRows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_row = False
        self.in_cell = False
        self.cell = ""
        self.cells = []
        self.link = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr" and "TTRow" in (attrs.get("class") or "").split():
            self.in_row, self.cells, self.link = True, [], None
        if self.in_row and tag == "td":
            self.in_cell, self.cell = True, ""
        if self.in_row and tag == "a" and attrs.get("href"):
            try:
                self.link = filing_url(attrs["href"])
            except BseError:
                pass

    def handle_data(self, data):
        if self.in_cell:
            self.cell += data

    def handle_endtag(self, tag):
        if self.in_row and tag == "td":
            self.cells.append(" ".join(self.cell.split()))
            self.in_cell = False
        if self.in_row and tag == "tr":
            if len(self.cells) >= 7 and self.link:
                self.rows.append((self.cells, self.link))
            self.in_row = False


def parse_index_html(data, expected_code):
    if len(data) > MAX_HTML:
        raise BseError("BSE index response exceeds size limit")
    html = data.decode("utf-8", "replace")
    parser = ResultRows()
    parser.feed(html)
    page = re.search(r"You are viewing page\s*(\d+)\s*of\s*(\d+)", html, re.I)
    pages = int(page.group(2)) if page else 0
    if not page:
        raise BseError("BSE PIT result page has no pagination status")
    rows = []
    for cells, link in parser.rows:
        code = bse_code(cells[0])
        if code != expected_code or not link.rsplit("/", 1)[-1].startswith("PIT_" + code + "_"):
            continue
        try:
            filed = datetime.strptime(cells[6], "%d/%m/%Y %H:%M:%S").replace(tzinfo=IST)
        except ValueError as exc:
            raise BseError("invalid BSE PIT submission timestamp") from exc
        rows.append({"id": "bse:" + link.rsplit("/", 1)[-1].removesuffix(".html"),
                     "source": "BSE PIT XBRL filing", "symbol": code + ".BO",
                     "company": cells[1], "filed_at": filed.isoformat(),
                     "regulation": "7(2)", "submission": None, "previous_id": None,
                     "revision_remark": None, "xbrl_url": None, "view_url": link,
                     "financial_year": cells[2]})
    return rows, pages


def _request(opener, request):
    try:
        with opener.open(request, timeout=25) as response:
            data = response.read(MAX_HTML + 1)
    except (OSError, TimeoutError) as exc:
        raise BseError("public BSE PIT source unavailable") from exc
    if len(data) > MAX_HTML:
        raise BseError("public BSE PIT response exceeds size limit")
    return data


def _one_index(opener, code, start, end, offline=None):
    filename = f"bse-{code}-{start.isoformat()}-{end.isoformat()}.html"
    if offline:
        path = Path(offline) / filename
        if not path.is_file() or path.stat().st_size > MAX_HTML:
            raise BseError(f"offline BSE PIT page missing or too large: {path}")
        return parse_index_html(path.read_bytes(), code)
    headers = {"User-Agent": "Mozilla/5.0 (IndiaTraderPaperResearch/0.2.1)",
               "Accept": "text/html", "Referer": PAGE}
    landing = _request(opener, urllib.request.Request(PAGE, headers=headers))
    hidden = HiddenFields()
    hidden.feed(landing.decode("utf-8", "replace"))
    if "__VIEWSTATE" not in hidden.fields or "__EVENTVALIDATION" not in hidden.fields:
        raise BseError("BSE search form state missing")
    body = dict(hidden.fields)
    body.update({"ctl00$ContentPlaceHolder1$Ddlcategory": "1",
                 "ctl00$ContentPlaceHolder1$txtDate": start.strftime("%d/%m/%Y"),
                 "ctl00$ContentPlaceHolder1$txtTodate": end.strftime("%d/%m/%Y"),
                 "ctl00$ContentPlaceHolder1$SmartSearch$hdnCode": code,
                 "ctl00$ContentPlaceHolder1$SmartSearch$smartSearch": code,
                 "ctl00$ContentPlaceHolder1$hf_scripcode": code,
                 "ctl00$ContentPlaceHolder1$btnSubmit": "Submit"})
    req = urllib.request.Request(PAGE, data=urllib.parse.urlencode(body).encode(), headers=headers)
    return parse_index_html(_request(opener, req), code)


def index(opener, code, start, end, offline=None):
    code = bse_code(code)
    if end < start or (end - start).days >= MAX_DAYS:
        raise BseError("BSE PIT search needs a range of at most 366 days")
    out = []
    cursor = start
    while cursor <= end:
        last = min(end, cursor + timedelta(days=29))
        out.extend(_range(opener, code, cursor, last, offline))
        cursor = last + timedelta(days=1)
        if not offline and cursor <= end:
            time.sleep(0.2)
    unique = {r["id"]: r for r in out}
    return sorted(unique.values(), key=lambda r: (r["filed_at"], r["id"]), reverse=True)


def _range(opener, code, start, end, offline):
    rows, pages = _one_index(opener, code, start, end, offline)
    if pages <= 1:
        return rows
    if start == end:
        raise BseError("BSE PIT has more than one result page for a day; coverage incomplete")
    mid = start + timedelta(days=(end - start).days // 2)
    return _range(opener, code, start, mid, offline) + _range(opener, code, mid + timedelta(days=1), end, offline)


class InlineFacts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.current = None
        self.facts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("ix:nonnumeric", "ix:nonfraction") and attrs.get("name") and attrs.get("contextref"):
            self.current = [attrs, ""]

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data

    def handle_endtag(self, tag):
        if tag in ("ix:nonnumeric", "ix:nonfraction") and self.current is not None:
            self.facts.append(tuple(self.current))
            self.current = None


def parse_ixbrl(data, filing):
    """Translate public BSE inline facts into the shared safe XBRL row parser."""
    if len(data) > MAX_HTML:
        raise BseError("BSE inline filing exceeds size limit")
    parser = InlineFacts()
    parser.feed(data.decode("utf-8", "replace"))
    main = {attrs["name"].split(":")[-1]: value.strip() for attrs, value in parser.facts
            if attrs["contextref"] == "MainI"}
    code = bse_code(main.get("ScripCode"))
    if filing and code + ".BO" != filing["symbol"]:
        raise BseError("BSE inline filing scrip code does not match index")
    root = ET.Element("xbrl")
    ET.SubElement(root, "Symbol", {"contextRef": "MainI"}).text = code + ".BO"
    ET.SubElement(root, "BSETradingSymbol", {"contextRef": "MainI"}).text = main.get("Symbol")
    ET.SubElement(root, "DisclosureUnderRegulation", {"contextRef": "MainI"}).text = "7(2)"
    for attrs, value in parser.facts:
        name = attrs["name"].split(":")[-1]
        if name in ("Symbol", "ScripCode", "DisclosureUnderRegulation") and attrs["contextref"] == "MainI":
            continue
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
            continue
        value = value.strip()
        if name == "DateOfFiling" or name.startswith("DateOfAllotmentAdvice") or name == "DateOfIntimationToCompany":
            try:
                value = datetime.strptime(value, "%d-%m-%Y").date().isoformat()
            except ValueError:
                pass
        if attrs.get("unitref"):
            value = value.replace(",", "")
            try:
                scale = int(attrs.get("scale", "0"))
                if scale:
                    value = format(Decimal(value) * (Decimal(10) ** scale), "f")
            except (ValueError, InvalidOperation):
                raise BseError("invalid BSE inline numeric scale")
        output = {"contextRef": attrs["contextref"]}
        if attrs.get("unitref"):
            output["unitRef"] = attrs["unitref"]
        ET.SubElement(root, name, output).text = value
    from .insider_tracker import parse_xml
    try:
        return parse_xml(ET.tostring(root), filing)
    except ValueError as exc:
        raise BseError(str(exc)) from exc


def get_filing(opener, url, offline=None):
    url = filing_url(url)
    filename = url.rsplit("/", 1)[-1]
    if offline:
        path = Path(offline) / "bse-ixbrl" / filename
        if not path.is_file() or path.stat().st_size > MAX_HTML:
            raise BseError(f"offline BSE inline filing missing or too large: {path}")
        return path.read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (IndiaTraderPaperResearch/0.2.1)",
                                              "Accept": "text/html", "Referer": PAGE})
    return _request(opener, req)
