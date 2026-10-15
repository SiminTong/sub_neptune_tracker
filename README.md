# sub_neptune_tracker
## Peer-review status of cited papers

Each cited arXiv paper gets a badge: **Published** (journal version exists), **Accepted**, or
**Preprint** (no journal version found). The weekly update decides this from, in order:

1. NASA ADS (optional): publisher full-text links (`PUB_PDF` / `PUB_HTML`) or the `REFEREED` flag.
   To turn this on, create a free API token at <https://ui.adsabs.harvard.edu/user/settings/token>
   and add it to this repo as an Actions secret named `ADS_API_TOKEN`
   (Settings → Secrets and variables → Actions → New repository secret).
2. The arXiv entry itself (journal reference, DOI, or an "accepted for publication in…" comment).
3. A Crossref title match.

Results are stored in `data/pubstatus.json`. A "published" result is kept; anything else is re-checked
every week. Without the ADS token the check still runs, but relies on steps 2 and 3, which can miss
papers whose authors never updated the arXiv entry.
