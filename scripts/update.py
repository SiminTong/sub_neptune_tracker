#!/usr/bin/env python3
"""Weekly refresh for the sub-Neptune x JWST tracker.

1. Pulls every planet with R_MIN <= R <= R_MAX Earth radii (see below) from the NASA Exoplanet Archive (TAP).
2. Searches arXiv (astro-ph.EP) for recent JWST papers and matches them to catalog planets by name.
3. Merges everything with the hand-reviewed results in data/jwst_results.json and writes data/data.json.

Papers found automatically are marked status "new" (unreviewed). To promote one, add a row to
data/jwst_results.json with its composition and status (mol / flat / amb).

Usage: python scripts/update.py [--offline]   (offline skips all network calls)
"""
import datetime
import json
import re
import sys
import time
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
    "select pl_name,hostname,pl_rade,pl_bmasse,pl_eqt,pl_orbper,disc_year "
    f"from pscomppars where pl_rade>={R_MIN} and pl_rade<={R_MAX}"
)
ARXIV = "https://export.arxiv.org/api/query"
ARXIV_QUERY = (
    "cat:astro-ph.EP AND (abs:JWST OR abs:\"James Webb\") AND "
    "(abs:\"sub-Neptune\" OR abs:\"mini-Neptune\" OR abs:\"exo-Neptune\" OR abs:\"super-Earth\" "
    "OR abs:\"sub-Neptunes\" OR abs:\"transmission spectrum\")"
)
ATOM = {"a": "http://www.w3.org/2005/Atom"}


def norm(s):
    return re.sub(r"[\s\-_]", "", s or "").lower()


def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
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
            "p": f("pl_orbper", 3), "y": x.get("disc_year"),
        }
    if len(out) < 100:
        raise RuntimeError(f"catalog suspiciously small ({len(out)} planets); refusing to publish")
    return out


def name_regex(name):
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", name) if p]
    return re.compile(r"(?<![A-Za-z0-9])" + r"[\s\-]?".join(parts) + r"(?![A-Za-z0-9])", re.I)


def get_papers(catalog_names):
    url = ARXIV + "?" + urllib.parse.urlencode({
        "search_query": ARXIV_QUERY, "sortBy": "submittedDate",
        "sortOrder": "descending", "max_results": 100,
    })
    root = ET.fromstring(fetch(url))
    regexes = [(n, name_regex(n)) for n in catalog_names]
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
        papers.append({
            "id": aid, "title": title, "url": f"https://arxiv.org/abs/{aid}",
            "published": (e.findtext("a:published", "", ATOM) or "")[:10], "planets": hits,
        })
    return papers


def main():
    offline = "--offline" in sys.argv
    today = datetime.date.today().isoformat()
    DATA.mkdir(exist_ok=True)
    curated = load_json(DATA / "jwst_results.json", [])
    seen = {p["id"]: p for p in load_json(DATA / "papers.json", [])}

    catalog = {}
    if not offline:
        catalog = get_catalog()
        for p in get_papers([v["n"] for v in catalog.values()]):
            if p["id"] not in seen:
                p["first_seen"] = today
                seen[p["id"]] = p
    papers = sorted(seen.values(), key=lambda p: p["published"], reverse=True)

    rows = {k: dict(v) for k, v in catalog.items()}
    reviewed = set()
    for c in curated:
        k = norm(c["n"])
        reviewed.add(k)
        base = rows.get(k, {})
        row = dict(c)
        for f in ("h", "r", "m", "t", "p", "y"):  # catalog values win when present
            if base.get(f) is not None and base.get(f) != "":
                row[f] = base[f]
        rows[k] = row

    # unreviewed arXiv papers -> flag planets that have no hand-reviewed result yet
    for p in papers:
        for name in p["planets"]:
            k = norm(name)
            row = rows.get(k)
            if row is None or k in reviewed or row.get("st") == "new":
                continue
            row["st"] = "new"
            row["ins"] = "JWST (see paper)"
            row["res"] = "New JWST paper, not yet reviewed: " + p["title"]
            row["ref"] = f"arXiv:{p['id']} ({p['published']})"
            row["url"] = p["url"]

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
