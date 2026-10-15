#!/usr/bin/env python3
"""Weekly refresh for the sub-Neptune x JWST tracker.

1. Pulls every planet with R_MIN <= R <= R_MAX Earth radii (see below) from the NASA Exoplanet Archive (TAP).
2. Searches arXiv (astro-ph.EP) for recent JWST papers and matches them to catalog planets by name.
3. Checks whether each cited arXiv paper has a journal version (NASA ADS if a token is set, then
   arXiv metadata, then Crossref) and records it in data/pubstatus.json.
4. Merges everything with the hand-reviewed results in data/jwst_results.json and writes data/data.json.

Papers found automatically are marked status "new" (unreviewed). To promote one, add a row to
data/jwst_results.json with its composition and status (mol / flat / amb).

Usage: python scripts/update.py [--offline]   (offline skips all network calls)
"""
import datetime
import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
R_MIN, R_MAX = 1.0, 6.0  # Earth radii; widen or narrow the catalog here
UA = {"User-Agent": "subneptune-tracker/1.0 (GitHub Actions)"}

TAP = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"
TAP_QUERY = (
    "select pl_name,hostname,pl_rade,pl_bmasse,pl_eqt,pl_orbper,disc_year,st_mass "
    f"from pscomppars where pl_rade>={R_MIN} and pl_rade<={R_MAX}"
)
ARXIV = "https://export.arxiv.org/api/query"
ARXIV_QUERY = (
    "cat:astro-ph.EP AND (abs:JWST OR abs:\"James Webb\") AND "
    "(abs:\"sub-Neptune\" OR abs:\"mini-Neptune\" OR abs:\"exo-Neptune\" OR abs:\"super-Earth\" "
    "OR abs:\"sub-Neptunes\" OR abs:\"transmission spectrum\")"
)
ATOM = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}


def norm(s):
    return re.sub(r"[\s\-_]", "", s or "").lower()


def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (400, 404, 410):
                raise  # definitive answer, do not retry
            last = e
            time.sleep(10 * (i + 1))  # 429 / 5xx: back off
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"fetch failed for {url}: {last}")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001
        return default


def get_catalog():
    url = TAP + "?" + urllib.parse.urlencode({"query": TAP_QUERY, "format": "json"})
    rows = json.loads(fetch(url))
    out = {}
    for x in rows:
        if not x.get("pl_name"):
            continue

        def f(k, nd=2):
            v = x.get(k)
            return None if v is None else round(float(v), nd)

        out[norm(x["pl_name"])] = {
            "n": x["pl_name"], "h": x.get("hostname") or "",
            "r": f("pl_rade"), "m": f("pl_bmasse"), "t": f("pl_eqt", 0),
            "p": f("pl_orbper", 3), "sm": f("st_mass", 2), "y": x.get("disc_year"),
        }
    if len(out) < 100:
        raise RuntimeError(f"catalog suspiciously small ({len(out)} planets); refusing to publish")
    return out


def name_regex(name):
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", name) if p]
    return re.compile(r"(?<![A-Za-z0-9])" + r"[\s\-]?".join(parts) + r"(?![A-Za-z0-9])", re.I)


OBS_RE = re.compile(r"NIRSpec|NIRCam|NIRISS|MIRI|NIRSpec|eclipse|phase curve|transmission spectr|emission spectr|"
                    r"we (?:present|report|observe[d]?|analy[sz]e|detect)|observations? (?:of|with|from)|JWST/", re.I)
THEORY_RE = re.compile(r"we (?:model|simulate|predict|investigate theoretically)|synthetic|forward model|"
                       r"simulated JWST|prospects|detectability|will be observed|upcoming|proposed", re.I)


def parse_entries(root, regexes):
    papers = []
    for e in root.findall("a:entry", ATOM):
        title = " ".join((e.findtext("a:title", "", ATOM)).split())
        abstract = " ".join((e.findtext("a:summary", "", ATOM)).split())
        link = e.findtext("a:id", "", ATOM).strip()
        m = re.search(r"arxiv\.org/abs/([^\s]+?)(v\d+)?$", link)
        aid = m.group(1) if m else link
        text = title + " " + abstract
        if not re.search(r"JWST|James Webb", text, re.I):
            continue
        hits = sorted({n for n, rx in regexes if rx.search(text)})
        if not hits:
            continue
        # a planet counts as the subject only if named in the title, or the abstract is observational
        in_title = sorted({n for n, rx in regexes if rx.search(title)})
        obs = bool(OBS_RE.search(abstract)) and not (THEORY_RE.search(abstract) and not in_title)
        if len(hits) > 6 and not in_title:      # survey/population papers: don't flag individual planets
            hits = []
        papers.append({
            "id": aid, "title": title, "url": f"https://arxiv.org/abs/{aid}",
            "published": (e.findtext("a:published", "", ATOM) or "")[:10], "planets": hits,
            "subject": in_title or (hits if obs else []), "obs": obs,
        })
    return papers


def arxiv_query(q, regexes, n=100, start=0):
    url = ARXIV + "?" + urllib.parse.urlencode({
        "search_query": q, "sortBy": "submittedDate", "sortOrder": "descending",
        "max_results": n, "start": start})
    return parse_entries(ET.fromstring(fetch(url)), regexes)


def get_papers(catalog_names, tracked_hosts=()):
    """Broad scan (3 pages of the keyword query) + one targeted query per host star of the
    planets already in the table, so older/other-keyword JWST papers on tracked planets are found."""
    regexes = [(n, name_regex(n)) for n in catalog_names]
    found = {}
    for start in (0, 100, 200):
        try:
            for p in arxiv_query(ARXIV_QUERY, regexes, 100, start):
                found[p["id"]] = p
        except Exception as e:  # noqa: BLE001
            print("broad scan failed:", e)
            break
        time.sleep(3)
    for host in tracked_hosts:
        q = f'(abs:JWST OR abs:"James Webb") AND (abs:"{host}" OR ti:"{host}")'
        try:
            for p in arxiv_query(q, regexes, 30):
                found.setdefault(p["id"], p)
        except Exception as e:  # noqa: BLE001
            print("host query failed:", host, e)
        time.sleep(3)
    return list(found.values())


# ---------------------------------------------------------------------------
# Peer-review status of cited arXiv papers
#   "pub" = a journal version exists, "acc" = accepted/in press, "pre" = preprint only (none found)
# Order of evidence (stops at the first journal version found): NASA ADS (needs ADS_API_TOKEN; publisher
# full-text links or the REFEREED flag), arXiv's own journal_ref / DOI / "accepted" comment, OpenAlex
# (merges preprint and journal records), Crossref (preprint relation, then title match), Semantic Scholar.
# Results live in data/pubstatus.json; a "pub" result is final, everything else is re-checked weekly.
# ---------------------------------------------------------------------------
ADS_TOKEN = os.environ.get("ADS_API_TOKEN", "").strip()
JOURNALS = {
    "The Astrophysical Journal Letters": "ApJL", "The Astrophysical Journal": "ApJ",
    "The Astronomical Journal": "AJ", "Monthly Notices of the Royal Astronomical Society": "MNRAS",
    "Astronomy & Astrophysics": "A&A", "Astronomy and Astrophysics": "A&A",
    "The Planetary Science Journal": "PSJ", "Nature Astronomy": "Nature Astronomy",
    "Publications of the Astronomical Society of the Pacific": "PASP",
}
DOI_JOURNAL = [
    ("10.3847/2041-8213", "ApJL"), ("10.3847/1538-4357", "ApJ"), ("10.3847/1538-3881", "AJ"),
    ("10.3847/psj", "PSJ"), ("10.1093/mnras", "MNRAS"), ("10.1051/0004-6361", "A&A"),
    ("10.1038/s41586", "Nature"), ("10.1038/s41550", "Nature Astronomy"),
    ("10.1126/science", "Science"), ("10.1088/1538-3873", "PASP"),
]
ACC_RE = re.compile(
    r"(accepted|in press|to appear|published)\s+(?:for publication\s+)?(?:in|by|at)\s+(?:the\s+)?([A-Za-z&. ]{2,60})", re.I)
ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})", re.I)


def arxiv_id(url):
    m = ARXIV_ID_RE.search(url or "")
    return m.group(1) if m else None


def short_journal(name=None, doi=None):
    if name:
        return JOURNALS.get(name.strip(), name.strip())
    for pre, j in DOI_JOURNAL:
        if (doi or "").lower().startswith(pre):
            return j
    return None


def norm_title(t):
    t = re.sub(r"<[^>]+>", "", t or "")  # Crossref titles can contain <sub>, <i>, MathML ...
    return re.sub(r"[^a-z0-9]", "", t.lower())


def arxiv_meta(ids):
    out = {}
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        url = ARXIV + "?" + urllib.parse.urlencode({"id_list": ",".join(chunk), "max_results": len(chunk)})
        root = ET.fromstring(fetch(url))
        for e in root.findall("a:entry", ATOM):
            aid = re.sub(r"v\d+$", "", e.findtext("a:id", "", ATOM).rsplit("/abs/", 1)[-1])
            first = e.findtext("a:author/a:name", "", ATOM).split()
            out[aid] = {
                "title": " ".join(e.findtext("a:title", "", ATOM).split()),
                "author": first[-1] if first else "",
                "journal_ref": (e.findtext("x:journal_ref", "", ATOM) or "").strip(),
                "doi": (e.findtext("x:doi", "", ATOM) or "").strip(),
                "comment": " ".join((e.findtext("x:comment", "", ATOM) or "").split()),
            }
        time.sleep(3)
    return out


def meta_status(m):
    if m.get("doi") or m.get("journal_ref"):
        j = short_journal(doi=m.get("doi")) or (m.get("journal_ref") or "").split(",")[0].strip() or None
        return {"ps": "pub", "j": j, "doi": m.get("doi") or None, "src": "arXiv metadata"}
    c = ACC_RE.search(m.get("comment") or "")
    if c:
        return {"ps": "acc", "j": c.group(2).strip().rstrip(".,; "), "doi": None, "src": "arXiv comment"}
    return None


def ads_status(aid):
    q = urllib.parse.urlencode({"q": f"identifier:arXiv:{aid}", "rows": 1,
                                "fl": "bibcode,doctype,pub,doi,esources,property"})
    req = urllib.request.Request("https://api.adsabs.harvard.edu/v1/search/query?" + q,
                                 headers={**UA, "Authorization": f"Bearer {ADS_TOKEN}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        docs = json.loads(r.read()).get("response", {}).get("docs", [])
    if not docs:
        return None
    d = docs[0]
    # Publisher full text (PUB_PDF / PUB_HTML) or the REFEREED flag means a journal version exists;
    # a preprint-only record has just EPRINT_* sources.
    pub = bool(set(d.get("esources") or []) & {"PUB_PDF", "PUB_HTML"}) or "REFEREED" in (d.get("property") or [])
    doi = (d.get("doi") or [None])[0]
    if pub:
        return {"ps": "pub", "j": short_journal(d.get("pub"), doi), "doi": doi, "src": "NASA ADS"}
    return {"ps": "pre", "j": None, "doi": None, "src": "NASA ADS"}


def openalex_status(aid):
    """OpenAlex merges the arXiv record with the journal record; a journal-type location means published."""
    url = f"https://api.openalex.org/works/https://doi.org/10.48550/arXiv.{aid}?select=doi,type,locations"
    try:
        w = json.loads(fetch(url))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    for loc in w.get("locations") or []:
        src = loc.get("source") or {}
        if src.get("type") != "journal":
            continue
        doi = None
        for cand in (loc.get("landing_page_url"), w.get("doi")):
            m = re.search(r"10\.\d{4,9}/\S+", cand or "")
            if m and not m.group(0).lower().startswith("10.48550"):
                doi = m.group(0)
                break
        return {"ps": "pub", "j": short_journal(src.get("display_name"), doi), "doi": doi, "src": "OpenAlex"}
    return {"ps": "pre", "j": None, "doi": None, "src": "OpenAlex"}


def crossref_status(aid, m):
    base = "https://api.crossref.org/works?"
    # 1) publishers often deposit a "has-preprint" link to the arXiv DOI
    q = {"filter": f"relation.type:has-preprint,relation.object:10.48550/arXiv.{aid}", "rows": 1,
         "select": "DOI,container-title,type"}
    items = json.loads(fetch(base + urllib.parse.urlencode(q))).get("message", {}).get("items", [])
    for it in items:
        cont = (it.get("container-title") or [""])[0]
        if it.get("type") == "journal-article" and cont and "arxiv" not in cont.lower():
            return {"ps": "pub", "j": short_journal(cont, it.get("DOI")), "doi": it.get("DOI"), "src": "Crossref"}
    # 2) fall back to a title + first-author match
    if not m.get("title"):
        return None
    q = {"query.bibliographic": m["title"], "rows": 5, "select": "title,DOI,container-title,type,author"}
    items = json.loads(fetch(base + urllib.parse.urlencode(q))).get("message", {}).get("items", [])
    want, fa = norm_title(m["title"]), (m.get("author") or "").lower()
    for it in items:
        if it.get("type") != "journal-article":
            continue
        if difflib.SequenceMatcher(None, want, norm_title((it.get("title") or [""])[0])).ratio() < 0.88:
            continue
        cont = (it.get("container-title") or [""])[0]
        if not cont or "arxiv" in cont.lower():
            continue
        fam = [(a.get("family") or "").lower() for a in (it.get("author") or [])[:3]]
        if fa and fam and fa not in fam:
            continue
        return {"ps": "pub", "j": short_journal(cont, it.get("DOI")), "doi": it.get("DOI"), "src": "Crossref"}
    return None


def s2_status(aid):
    url = f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{aid}?fields=externalIds,venue,journal"
    try:
        d = json.loads(fetch(url, tries=2))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    doi = (d.get("externalIds") or {}).get("DOI")
    if doi and not doi.lower().startswith("10.48550"):
        name = (d.get("journal") or {}).get("name") or d.get("venue")
        return {"ps": "pub", "j": short_journal(name, doi), "doi": doi, "src": "Semantic Scholar"}
    return None


def update_pubstatus(ids, offline, today):
    path = DATA / "pubstatus.json"
    cache = load_json(path, {})
    todo = [i for i in ids if (cache.get(i) or {}).get("ps") != "pub"]
    if offline or not todo:
        return cache
    try:
        meta = arxiv_meta(todo)
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: arXiv metadata lookup failed: {e}", file=sys.stderr)
        meta = {}
    for i in todo:
        m = meta.get(i, {})
        steps = []
        if ADS_TOKEN:
            steps.append(("ADS", lambda i=i: ads_status(i)))
        steps += [
            ("arXiv metadata", lambda m=m: meta_status(m) if m else None),
            ("OpenAlex", lambda i=i: openalex_status(i)),
            ("Crossref", lambda i=i, m=m: crossref_status(i, m)),
            ("Semantic Scholar", lambda i=i: s2_status(i)),
        ]
        found, errors = [], 0
        for name, step in steps:
            try:
                r = step()
            except Exception as e:  # noqa: BLE001
                errors += 1
                print(f"WARNING: {name} check failed for {i}: {e}", file=sys.stderr)
                continue
            if r:
                found.append(r)
                if r["ps"] == "pub":
                    break  # a journal version is final; no need to ask further
            time.sleep(0.5)
        best = (next((r for r in found if r["ps"] == "pub"), None)
                or next((r for r in found if r["ps"] == "acc"), None)
                or (found[0] if found else None))
        if best is None:
            if errors:
                continue  # checks failed: leave unchecked / keep the earlier result rather than guess
            best = {"ps": "pre", "j": None, "doi": None, "src": "no journal version found"}
        cache[i] = {**best, "checked": today}
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True))
    return cache


def annotate(rows, papers, status):
    """Copy ps/j/doi from the status cache onto refs, rows and papers."""
    def pick(i):
        st = status.get(i) or {}
        return {k: v for k, v in (("ps", st.get("ps")), ("j", st.get("j")), ("doi", st.get("doi"))) if v}
    def clear(o):
        for k in ("ps", "j", "doi"):
            o.pop(k, None)
    for r in rows:
        for ref in r.get("refs") or []:
            clear(ref)
            ref.update(pick(arxiv_id(ref.get("u"))))
        if not r.get("refs") and arxiv_id(r.get("url")):
            clear(r)
            r.update(pick(arxiv_id(r["url"])))
    for p in papers:
        clear(p)
        p.update(pick(p["id"]))


def main():
    offline = "--offline" in sys.argv
    today = datetime.date.today().isoformat()
    DATA.mkdir(exist_ok=True)
    curated = load_json(DATA / "jwst_results.json", [])
    seen = {p["id"]: p for p in load_json(DATA / "papers.json", [])}

    catalog = {}
    if not offline:
        catalog = get_catalog()
        hosts = sorted({c["h"] for c in curated if c.get("h")})
        for p in get_papers([v["n"] for v in catalog.values()], hosts):
            old = seen.get(p["id"])
            p["first_seen"] = (old or {}).get("first_seen", today)
            seen[p["id"]] = p
    papers = sorted(seen.values(), key=lambda p: p["published"], reverse=True)

    rows = {k: dict(v) for k, v in catalog.items()}
    reviewed = set()
    for c in curated:
        k = norm(c["n"])
        reviewed.add(k)
        base = rows.get(k, {})
        row = dict(c)
        for f in ("h", "r", "m", "t", "p", "y", "sm"):  # catalog values win when present
            if base.get(f) is not None and base.get(f) != "":
                row[f] = base[f]
        rows[k] = row

    # Unreviewed observational papers. Reviewed planets: add the paper to their references as an
    # "unreviewed" entry; other table planets: flag the row as "new".
    for p in papers:
        if not p.get("obs"):
            continue
        for name in p.get("subject", []):
            k = norm(name)
            row = rows.get(k)
            if row is None:
                continue
            if k in reviewed:
                have = {arxiv_id(x.get("u")) for x in row.get("refs") or []}
                if p["id"] not in have:
                    short = p["title"][:70] + ("…" if len(p["title"]) > 70 else "")
                    row.setdefault("refs", []).append(
                        {"t": f"New, unreviewed ({p['published']}): {short}", "u": p["url"]})
                continue
            if row.get("st") == "new":
                continue
            row["st"] = "new"
            row["ins"] = "JWST (see paper)"
            row["res"] = "New JWST paper, not yet reviewed: " + p["title"]
            row["ref"] = f"arXiv:{p['id']} ({p['published']})"
            row["url"] = p["url"]

    ids = sorted({i for r in rows.values()
                  for i in [arxiv_id(x.get("u")) for x in (r.get("refs") or [])] + [arxiv_id(r.get("url"))] if i}
                 | {p["id"] for p in papers})
    status = update_pubstatus(ids, offline, today)
    annotate(rows.values(), papers, status)

    out_rows = sorted(rows.values(), key=lambda r: (r.get("st") is None, r["n"]))
    out = {
        "asof": today,
        "catalog_loaded": bool(catalog) or None,
        "catalog_count": len(catalog),
        "r_range": [R_MIN, R_MAX],
        "rows": out_rows,
        "papers": papers[:60],
    }
    (DATA / "data.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    (DATA / "papers.json").write_text(json.dumps(papers, ensure_ascii=False, indent=1))
    print(f"{len(out_rows)} rows ({len(catalog)} catalog), {len(papers)} matched papers, as of {today}")


if __name__ == "__main__":
    main()
