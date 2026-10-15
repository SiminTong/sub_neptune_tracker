#!/usr/bin/env python3
"""Weekly refresh for the sub-Neptune x JWST tracker.

1. Pulls every planet with R_MIN <= R <= R_MAX Earth radii (see below) from the NASA Exoplanet Archive (TAP).
2. Searches arXiv (astro-ph.EP) for recent JWST papers and matches them to catalog planets by name.
3. Looks up JWST observations in MAST (instrument, filters, wavelength range, proposal ID, data
   release date), so planets observed but not yet published still show up.
4. Merges everything with the hand-reviewed results in data/jwst_results.json and writes data/data.json.

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


MAST = "https://mast.stsci.edu/api/v0/invoke"
MAST_COLS = "target_name,instrument_name,filters,em_min,em_max,proposal_id,t_min,t_obs_release,intentType"
INSTR = {"NIRSPEC": "NIRSpec", "NIRCAM": "NIRCam", "NIRISS": "NIRISS", "MIRI": "MIRI"}
# Nominal wavelength coverage (um), used only when MAST gives no em_min/em_max for an observation.
NOMINAL = {
    "G140H": (0.97, 1.89), "G235H": (1.66, 3.17), "G395H": (2.87, 5.14), "PRISM": (0.6, 5.3),
    "GR700XD": (0.6, 2.8), "F322W2": (2.4, 4.0), "F444W": (3.9, 5.0), "P750L": (5.0, 12.0),
}
MJD0 = datetime.date(1858, 11, 17)


def mjd_date(v):
    try:
        return (MJD0 + datetime.timedelta(days=int(float(v)))).isoformat()
    except (TypeError, ValueError):
        return None


def mast_post(req):
    body = urllib.parse.urlencode({"request": json.dumps(req)}).encode()
    last = None
    for i in range(3):
        try:
            r = urllib.request.Request(
                MAST, data=body, headers={**UA, "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(r, timeout=180) as f:
                return json.loads(f.read())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"MAST request failed: {last}")


def get_mast(rows):
    """JWST observations found in MAST (published or not) for these planets/hosts.

    Matches MAST target_name to a planet name (planet-level) or to its host star name
    (host-level, flagged because the proposer did not name the planet).
    Returns {planet_name: [obs, ...]}.
    """
    by_planet = {norm(r["n"]): r["n"] for r in rows}
    by_host = {}
    for r in rows:
        if r.get("h"):
            by_host.setdefault(norm(r["h"]), []).append(r["n"])
    names = set()
    for r in rows:
        for v in (r["n"], r["n"].replace(" ", ""), r.get("h") or "", (r.get("h") or "").replace(" ", "")):
            if v:
                names.add(v)
    names = sorted(names)
    found, total = {}, 0
    for i in range(0, len(names), 150):
        chunk = names[i:i + 150]
        page = 1
        while page <= 20:
            resp = mast_post({
                "service": "Mast.Caom.Filtered", "format": "json", "pagesize": 5000, "page": page,
                "params": {"columns": MAST_COLS, "filters": [
                    {"paramName": "obs_collection", "values": ["JWST"]},
                    {"paramName": "target_name", "values": chunk}]},
            })
            if resp.get("status") not in (None, "COMPLETE"):
                raise RuntimeError(f"MAST returned status {resp.get('status')}: {resp.get('msg')}")
            data = resp.get("data") or []
            total += len(data)
            for row in data:
                if (row.get("intentType") or "science").lower() != "science":
                    continue
                inst = (row.get("instrument_name") or "")
                base, _, sub = inst.partition("/")
                if base.upper() not in INSTR:
                    continue
                tn = norm(row.get("target_name"))
                if tn in by_planet:
                    targets = [(by_planet[tn], False)]
                elif tn in by_host:
                    targets = [(p, True) for p in by_host[tn]]
                else:
                    continue
                filt = row.get("filters") or ""
                mode = f"{INSTR[base.upper()]}{'/' + sub if sub else ''} {filt}".strip()
                lo, hi, nom = row.get("em_min"), row.get("em_max"), False
                lo = lo / 1000 if isinstance(lo, (int, float)) else None  # MAST em_* are in nm
                hi = hi / 1000 if isinstance(hi, (int, float)) else None
                if lo is None or hi is None:
                    for tok in re.split(r"[;,\s]+", filt.upper()):
                        if tok in NOMINAL:
                            (lo, hi), nom = NOMINAL[tok], True
                            break
                d0, rel = mjd_date(row.get("t_min")), mjd_date(row.get("t_obs_release"))
                pid = str(row.get("proposal_id") or "")
                for pname, host in targets:
                    e = found.setdefault(pname, {}).setdefault((mode, pid, host), {
                        "mode": mode, "pid": pid, "host": host, "w": None, "nom": nom,
                        "d": d0, "rel": rel, "n": 0})
                    e["n"] += 1
                    if lo is not None and hi is not None:
                        e["w"] = [lo, hi] if e["w"] is None else [min(e["w"][0], lo), max(e["w"][1], hi)]
                        e["nom"] = e["nom"] and nom
                    if d0 and (not e["d"] or d0 < e["d"]):
                        e["d"] = d0
                    if rel and (not e["rel"] or rel > e["rel"]):
                        e["rel"] = rel
            if len(data) < 5000:
                break
            page += 1
        print(f"MAST: {min(i + 150, len(names))}/{len(names)} names queried, {total} rows so far", flush=True)
    if total == 0:
        raise RuntimeError("MAST returned no rows at all; treating as a failed query")
    out = {}
    for pname, d in found.items():
        lst = sorted(d.values(), key=lambda e: (e["host"], e["d"] or "", e["pid"]))
        for e in lst:
            if e["w"]:
                e["w"] = [round(e["w"][0], 2), round(e["w"][1], 2)]
            else:
                e.pop("w")
            if not e.pop("nom") or "w" not in e:
                e.pop("nom", None)
            else:
                e["nom"] = True
        out[pname] = lst
    return out


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

    # JWST observations in MAST (includes data that is still proprietary / unpublished)
    prev = load_json(DATA / "data.json", {})
    prev_obs = {norm(r["n"]): r.get("obs") for r in prev.get("rows", []) if r.get("obs")}
    mast_checked = prev.get("mast_checked")
    obs = None
    if not offline:
        try:
            obs = get_mast(list(rows.values()))
            mast_checked = today
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: MAST step failed, keeping previous observation data: {e}", file=sys.stderr)
    for k, row in rows.items():
        lst = obs.get(row["n"], []) if obs is not None else prev_obs.get(k, [])
        if lst:
            row["obs"] = lst
        row["on"] = len(lst)

    out_rows = sorted(rows.values(), key=lambda r: (r.get("st") is None, not r.get("on"), r["n"]))
    out = {
        "asof": today,
        "mast_checked": mast_checked,
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
