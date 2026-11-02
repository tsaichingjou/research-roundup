"""Read the open full text of a paper (PDF or web page).

Used in two places:
  1. Screening fallback — a paper whose feed carries no abstract (Fed speeches
     and blog posts, some central-bank working papers) can still be scored from
     the opening of its text: the abstract section of a PDF if there is one,
     otherwise the first paragraphs.
  2. Write-ups — the 5-8 papers selected for the newsletter are written up from
     the full text when it is openly available, so the key takeaways can quote
     actual estimates instead of paraphrasing the abstract.

Only openly published documents are read: the paper's own link, the PDF its
landing page declares (citation_pdf_url), or an open-access copy listed by
OpenAlex. A page that answers with an error, a login or a bot check is simply
skipped; no paywall or protection is bypassed. Journals whose publishers do not
offer open copies (e.g. JPE) therefore keep abstract-only treatment.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from src.fetchers.rss import clean_text
from src.models import normalise_title

DATASET_HOSTS = ("zenodo.org", "dataverse", "data.mendeley.com", "doi.org/10.17632",
                 "doi.org/10.5281", "doi.org/10.7910", "openicpsr")

log = logging.getLogger(__name__)

MAX_BYTES = 15_000_000
_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s?#]+)", re.IGNORECASE)
_BLOCKED = re.compile(r"captcha|are you a robot|access denied|enable javascript|cloudflare|"
                      r"verify you are human|sign in to continue|purchase this article", re.I)
_REFERENCES = re.compile(r"\n\s*(references|bibliography|works cited)\s*\n", re.I)
_ABSTRACT = re.compile(r"\babstract\b[\s:.\-—]*", re.I)


@dataclass
class FullText:
    text: str
    url: str
    kind: str          # "PDF" | "web page"


class _PageParser(HTMLParser):
    """Collects readable paragraphs and the declared PDF link of an HTML page."""

    SKIP = {"script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg", "button"}
    BLOCK = {"p", "h1", "h2", "h3", "h4", "li", "blockquote", "dd"}

    def __init__(self):
        super().__init__()
        self.pdf_url: str | None = None
        self.pdf_links: list[str] = []      # <a href="...pdf"> links, in page order
        self.paragraphs: list[str] = []
        self._skip = 0
        self._buf: list[str] = []
        self._in_block = 0

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta" and (a.get("name") or "").lower() == "citation_pdf_url" and a.get("content"):
            self.pdf_url = self.pdf_url or a["content"]
        if tag == "a" and ".pdf" in a.get("href", "").lower() and not self._skip:
            self.pdf_links.append(a["href"])
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self._flush()
            self._in_block += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK and self._in_block:
            self._in_block -= 1
            self._flush()

    def handle_data(self, data):
        if not self._skip and self._in_block:
            self._buf.append(data)

    def _flush(self):
        text = clean_text(" ".join(self._buf))
        self._buf = []
        if len(text) >= 40:          # drop menu items, buttons, captions
            self.paragraphs.append(text)


def html_text(html: str) -> tuple[str, str | None]:
    """Readable text of an HTML page and the PDF link it declares (if any)."""
    text, pdf, _ = parse_page(html)
    return text, pdf


def parse_page(html: str) -> tuple[str, str | None, list[str]]:
    """Readable text, declared PDF (citation_pdf_url) and linked PDFs of a page."""
    parser = _PageParser()
    try:
        parser.feed(html)
        parser._flush()
    except Exception:  # malformed markup: keep what was parsed
        pass
    return "\n\n".join(parser.paragraphs), parser.pdf_url, parser.pdf_links


def pdf_text(data: bytes, max_pages: int = 40) -> str:
    from pypdf import PdfReader  # imported lazily: only needed when a PDF is found

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages[:max_pages]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # one bad page should not lose the document
            continue
    text = "\n".join(pages)
    text = re.sub(r"-\n(?=[a-z])", "", text)          # re-join hyphenated words
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def trim(text: str, max_chars: int) -> str:
    """Drop the reference list and appendices, then cap the length."""
    m = _REFERENCES.search(text, int(len(text) * 0.4))
    if m:
        text = text[:m.start()]
    if len(text) > max_chars:
        cut = text.rfind("\n", 0, max_chars)
        text = text[:cut if cut > max_chars * 0.8 else max_chars] + "\n[... text truncated ...]"
    return text.strip()


def opening(text: str, n_chars: int = 1500) -> str:
    """The part of a document that best stands in for an abstract: the text after
    an 'Abstract' heading near the top, otherwise the opening paragraphs."""
    m = _ABSTRACT.search(text[:6000])
    start = m.end() if m else 0
    chunk = clean_text(text[start:start + n_chars * 2])
    if len(chunk) > n_chars:
        cut = chunk.rfind(". ", 0, n_chars)
        chunk = chunk[:cut + 1 if cut > n_chars * 0.5 else n_chars]
    return chunk


def _mentions_title(text: str, title: str) -> bool:
    """Guard against cookie walls and generic pages: the document should mention
    most of the distinctive words of the paper's title near the top."""
    words = [w for w in normalise_title(title).split() if len(w) > 3]
    if not words:
        return True
    head = normalise_title(text[:8000])
    return sum(w in head for w in words) / len(words) >= 0.6


class FullTextFinder:
    def __init__(self, settings: dict, max_chars: int = 60000, min_chars: int = 1500):
        self.timeout = min(settings.get("request_timeout_seconds", 20), 30)
        self.headers = {"User-Agent": settings.get("user_agent", "ResearchRoundupBot/0.1")}
        self.mailto = settings.get("contact_email")
        self.max_chars = max_chars
        self.min_chars = min_chars

    # -- network -----------------------------------------------------------------
    def _get(self, url: str) -> tuple[bytes, str, str] | None:
        """(content, final url, content type) or None on any failure."""
        try:
            with requests.get(url, headers=self.headers, timeout=self.timeout, stream=True,
                              allow_redirects=True) as resp:
                if resp.status_code != 200:
                    log.debug("Full text: %s returned HTTP %s", url, resp.status_code)
                    return None
                data = b""
                for chunk in resp.iter_content(65536):
                    data += chunk
                    if len(data) > MAX_BYTES:
                        return None
                return data, resp.url, resp.headers.get("Content-Type", "").lower()
        except requests.RequestException as exc:
            log.debug("Full text: %s failed: %s", url, exc)
            return None

    def _open_access_urls(self, url: str) -> list[str]:
        """Open-access copies OpenAlex knows for a DOI (DOI lookups are free)."""
        m = _DOI_RE.search(url or "")
        if not m:
            return []
        params = {"select": "best_oa_location,open_access"}
        if self.mailto:
            params["mailto"] = self.mailto
        try:
            resp = requests.get(f"https://api.openalex.org/works/doi:{m.group(1)}", params=params,
                                headers=self.headers, timeout=self.timeout)
            if resp.status_code != 200:
                return []
            data = resp.json()
        except (requests.RequestException, ValueError):
            return []
        loc = data.get("best_oa_location") or {}
        return [u for u in (loc.get("pdf_url"), loc.get("landing_page_url"),
                            (data.get("open_access") or {}).get("oa_url")) if u]

    # -- extraction ----------------------------------------------------------------
    def _read(self, url: str, title: str, depth: int = 0) -> FullText | None:
        got = self._get(url)
        if not got:
            return None
        data, final_url, ctype = got
        if "pdf" in ctype or data[:5] == b"%PDF-":
            try:
                text = pdf_text(data)
            except Exception as exc:
                log.debug("Full text: unreadable PDF %s: %s", final_url, exc)
                return None
            kind = "PDF"
        elif "html" in ctype or data.lstrip()[:1] == b"<":
            html = data.decode("utf-8", errors="replace")
            if _BLOCKED.search(html[:20000]) and len(html) < 60000:
                return None
            text, pdf, links = parse_page(html)
            # A landing page that declares a PDF: the PDF is the paper; the page
            # usually only holds the abstract.
            if pdf and depth == 0:
                found = self._read(urljoin(final_url, pdf), title, depth + 1)
                if found:
                    return found
            # Otherwise a short landing page (BIS, ECB) usually links the paper's
            # PDF: try the first PDFs on the same site, keeping one only if it is
            # about this paper (title check) and longer than the page itself.
            if depth == 0 and len(text) < 15000:
                host = urlparse(final_url).netloc
                same_site = [u for u in dict.fromkeys(urljoin(final_url, h) for h in links)
                             if urlparse(u).netloc == host][:2]
                for link in same_site:
                    found = self._read(link, title, depth + 1)
                    if found and found.kind == "PDF" and len(found.text) > len(text):
                        return found
            # Fed in Print records link to the publishing bank's own page.
            if "fedinprint.org/item/" in final_url and depth == 0 and not final_url.endswith("/original"):
                found = self._read(final_url.rstrip("/") + "/original", title, depth + 1)
                if found and len(found.text) > len(text):
                    return found
            kind = "web page"
        else:
            return None
        if len(text) < self.min_chars or not _mentions_title(text, title):
            return None
        return FullText(text=trim(text, self.max_chars), url=final_url, kind=kind)

    def find(self, paper: dict) -> FullText | None:
        url = paper.get("url") or ""
        if any(h in url for h in DATASET_HOSTS):
            return None
        candidates = [url]
        if "doi.org/" in url:
            candidates += self._open_access_urls(url)
        seen = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            found = self._read(candidate, paper.get("title", ""))
            if found:
                return found
        return None
