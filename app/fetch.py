"""Step 1: resolve an identifier, check open access, pull the relevant sections.

Only open-access papers are accepted. The full text is used in memory to build
the model prompt and is never stored.
"""
from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Tuple

import httpx
from lxml import etree

from . import config
from .schemas import PaperMeta

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
IDCONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
HEADERS = {"User-Agent": "OpenDAG/1.0 (open causal DAG dataset; https://github.com/)"}

# Section titles we send to the model, in this order.
WANTED = [
    ("methods", re.compile(r"method|design|participant|population|setting|data source|exposure|outcome|covariat|patients", re.I)),
    ("statistics", re.compile(r"statistic|analys", re.I)),
    ("results", re.compile(r"result|finding", re.I)),
]
SKIP = re.compile(r"discussion|reference|acknowledg|funding|conflict|competing|contribut|supplement|abbreviation|footnote|availability|ethic", re.I)


class FetchError(Exception):
    """Shown to the user as-is."""


def _get(url: str, params: Optional[dict] = None, as_json: bool = True, tries: int = 3):
    last = None
    for attempt in range(tries):
        try:
            r = httpx.get(url, params=params, headers=HEADERS, timeout=30, follow_redirects=True)
            if r.status_code == 200:
                if not as_json:
                    return r.text
                data = r.json()
                if "errCode" in data:  # Europe PMC returns 200/503 with errCode on hiccups
                    last = data.get("errMsg")
                else:
                    return data
            elif r.status_code == 404:
                return None
            else:
                last = f"HTTP {r.status_code}"
        except (httpx.HTTPError, ValueError) as e:
            last = str(e)
        time.sleep(1.5 * (attempt + 1))
    raise FetchError(f"Could not reach {url.split('/')[2]} ({last}). Please try again shortly.")


def classify_id(raw: str) -> Tuple[str, str]:
    """Return (kind, normalised id) where kind is doi|pmcid|pmid."""
    s = raw.strip()
    s = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", s, flags=re.I)
    s = re.sub(r"^doi:\s*", "", s, flags=re.I)
    m = re.search(r"PMC\d+", s, re.I)
    if m:
        return "pmcid", m.group(0).upper()
    if re.fullmatch(r"\d{1,9}", s):
        return "pmid", s
    if s.startswith("10.") and "/" in s:
        return "doi", s.lower()
    raise FetchError("Please enter a DOI (10.xxxx/...), a PMCID (PMC1234567) or a PubMed ID.")


def _idconv(doi: str) -> Optional[str]:
    data = _get(IDCONV, {"ids": doi, "format": "json", "tool": "opendag"})
    for rec in (data or {}).get("records", []):
        if rec.get("pmcid"):
            return rec["pmcid"]
    return None


def _search(query: str) -> Optional[dict]:
    data = _get(f"{EPMC}/search", {"query": query, "format": "json", "resultType": "core"})
    results = (data or {}).get("resultList", {}).get("result", [])
    return results[0] if results else None


def resolve(raw: str) -> dict:
    """Find the Europe PMC record for a DOI/PMCID/PMID."""
    kind, ident = classify_id(raw)
    rec = None
    if kind == "pmcid":
        rec = _search(f"PMCID:{ident}")
    elif kind == "pmid":
        rec = _search(f"EXT_ID:{ident} AND SRC:MED")
    else:
        try:
            rec = _search(f'DOI:"{ident}"')
        except FetchError:
            rec = None  # Europe PMC DOI search is sometimes down; fall back to NCBI
        if rec is None:
            pmcid = _idconv(ident)
            if pmcid:
                rec = _search(f"PMCID:{pmcid}")
    if not rec:
        raise FetchError(
            f"No record found for {ident}. OpenDAG only accepts papers indexed in "
            "Europe PMC with open-access full text."
        )
    return rec


def meta_from_record(rec: dict) -> PaperMeta:
    journal = (rec.get("journalInfo") or {}).get("journal", {}).get("title", "")
    return PaperMeta(
        title=re.sub(r"\.$", "", rec.get("title", "")),
        authors=rec.get("authorString", ""),
        year=rec.get("pubYear"),
        journal=journal,
        doi=rec.get("doi"),
        pmcid=rec.get("pmcid"),
        pmid=rec.get("pmid"),
        license=rec.get("license"),
    )


def _text(el) -> str:
    """Plain text of an XML element, minus tables, figures and citations."""
    el = etree.fromstring(etree.tostring(el))  # copy so we can strip children
    etree.strip_elements(
        el, "table-wrap", "fig", "xref", "table", "disp-formula", "supplementary-material",
        with_tail=False,
    )
    txt = " ".join(el.itertext())
    return re.sub(r"\s+", " ", txt).strip()


def extract_sections(xml: str) -> Dict[str, str]:
    """Pull abstract, methods, statistics and results text from JATS XML."""
    root = etree.fromstring(xml.encode("utf-8"), parser=etree.XMLParser(recover=True, huge_tree=True))
    out: Dict[str, List[str]] = {"abstract": [], "methods": [], "statistics": [], "results": []}

    for ab in root.xpath("//front//abstract[not(@abstract-type) or @abstract-type!='graphical']"):
        out["abstract"].append(_text(ab))

    def walk(sec, inherited: Optional[str]):
        title_el = sec.find("title")
        title = " ".join(title_el.itertext()) if title_el is not None else ""
        sec_type = sec.get("sec-type", "")
        label = inherited
        if SKIP.search(title) and not inherited:
            return
        for name, pat in WANTED:
            if pat.search(title) or pat.search(sec_type):
                # statistics nested under methods is still useful, keep it labelled
                if inherited is None or name == "statistics":
                    label = name
                break
        if label:
            paras = [_text(p) for p in sec.findall("p")]
            body = " ".join(p for p in paras if p)
            if body:
                out[label].append((f"[{title}] " if title else "") + body)
        for child in sec.findall("sec"):
            walk(child, label)

    for sec in root.xpath("//body/sec"):
        walk(sec, None)

    return {k: "\n".join(v) for k, v in out.items() if v}


def build_prompt_text(sections: Dict[str, str], budget: int = None) -> str:
    """Join sections under a character budget; methods get the biggest share."""
    budget = budget or config.MAX_INPUT_CHARS
    shares = {"abstract": 0.15, "methods": 0.45, "statistics": 0.2, "results": 0.2}
    present = {k: v for k, v in sections.items() if v}
    total_share = sum(shares[k] for k in present) or 1
    parts = []
    spare = 0
    # Give unused budget from short sections to later ones.
    for k in ["abstract", "statistics", "methods", "results"]:
        if k not in present:
            continue
        limit = int(budget * shares[k] / total_share) + spare
        text = present[k]
        spare = max(0, limit - len(text))
        parts.append((k, text[:limit]))
    order = ["abstract", "methods", "statistics", "results"]
    parts.sort(key=lambda p: order.index(p[0]))
    return "\n\n".join(f"## {k.upper()}\n{t}" for k, t in parts)


def check_open_access(rec: dict) -> PaperMeta:
    """Raise FetchError unless the record is open access under a CC/public-domain licence."""
    meta = meta_from_record(rec)
    licence = (meta.license or "").lower()
    has_open_licence = licence.startswith("cc") or "public domain" in licence
    if rec.get("isOpenAccess") != "Y" or not meta.pmcid or not has_open_licence:
        raise FetchError(
            f"“{meta.title or 'This paper'}” is not available under an open-access licence "
            f"(licence: {meta.license or 'none'}), so OpenDAG can't process it. "
            "We only use Creative Commons-licensed papers to respect copyright."
        )
    return meta


def fetch_paper(raw: str) -> Tuple[PaperMeta, Dict[str, str]]:
    """Resolve, enforce open access, and return (metadata, sections)."""
    meta = check_open_access(resolve(raw))
    xml = _get(f"{EPMC}/{meta.pmcid}/fullTextXML", as_json=False)
    if not xml:
        raise FetchError(f"Europe PMC has no full-text XML for {meta.pmcid}.")
    sections = extract_sections(xml)
    if not sections.get("methods") and not sections.get("abstract"):
        raise FetchError("Couldn't find a methods section or abstract in this paper's full text.")
    return meta, sections
