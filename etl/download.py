"""Download monthly NHS England A&E (CSV) and RTT incomplete-provider (XLSX) files.

The england.nhs.uk listing pages return HTTP 202 with an empty body to plain
HTTP clients, so the pages are rendered with Playwright (Chromium) and the file
links are read from the DOM. Files are saved to data/raw/ using the names the
ETL expects:

    ae_attendances_YYYYMM.csv
    rtt_incomplete_provider_YYYYMM.xlsx

Usage:
    python -m etl.download --list              # print matched links, download nothing
    python -m etl.download                      # download missing files
    python -m etl.download --headed --force     # headed browser, re-download everything
"""
import argparse
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

RAW_DIR = _ROOT / "data" / "raw"
START = (2025, 4)

AE_PAGES = [
    "https://www.england.nhs.uk/statistics/statistical-work-areas/ae-waiting-times-and-activity/ae-attendances-and-emergency-admissions-2025-26/",
    "https://www.england.nhs.uk/statistics/statistical-work-areas/ae-waiting-times-and-activity/ae-attendances-and-emergency-admissions-2026-27/",
]
RTT_PAGES = [
    "https://www.england.nhs.uk/statistics/statistical-work-areas/rtt-waiting-times/rtt-data-2025-26/",
    "https://www.england.nhs.uk/statistics/statistical-work-areas/rtt-waiting-times/rtt-data-2026-27/",
]

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
MON_ABBR = {m[:3]: i + 1 for i, m in enumerate(MONTHS)}

# Revision markers vary in the link text ("(revised 14.05.26)", "revised 14.05.26)", a
# trailing "revised", "CSV (revised ...)"), so allow anything between the period and the
# "(CSV" / "(XLSX" file-type tag.
AE_RE = re.compile(r"^Monthly A&E (" + "|".join(MONTHS) + r") (\d{4}).*?\(CSV", re.I)
RTT_RE = re.compile(r"^Incomplete Provider (" + "|".join(MON_ABBR) + r")(\d{2})\s*\(XLSX", re.I)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _match_ae(text: str):
    m = AE_RE.match(text)
    if not m:
        return None
    return int(m.group(2)), MONTHS.index(m.group(1).title()) + 1


def _match_rtt(text: str):
    m = RTT_RE.match(text)
    if not m:
        return None
    return 2000 + int(m.group(2)), MON_ABBR[m.group(1).title()]


def collect_links(page, url: str, matcher):
    """Return {(year, month): (text, href, revised)} for links on one listing page."""
    resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
    status = resp.status if resp else None
    # The 202 response is an interstitial; the real page replaces it a moment later.
    # Wait for an actual file link to be attached rather than a fixed delay.
    for attempt in range(3):
        try:
            page.wait_for_selector("a[href$='.xlsx'], a[href$='.csv']", state="attached", timeout=20000)
            break
        except Exception:
            if attempt == 2:
                raise
            resp = page.reload(wait_until="domcontentloaded")
            status = resp.status if resp else status
    page.wait_for_timeout(1000)
    found = {}
    for a in page.query_selector_all("a[href]"):
        text = _norm(a.text_content() or "")
        key = matcher(text)
        if key is None or key < START:
            continue
        revised = "revised" in text.lower()
        prev = found.get(key)
        # Prefer a revised version; otherwise keep the first link seen.
        if prev is None or (revised and not prev[2]):
            found[key] = (text, a.get_attribute("href"), revised)
    return status, found


def target(kind: str, key) -> Path:
    y, m = key
    if kind == "ae":
        return RAW_DIR / f"ae_attendances_{y}{m:02d}.csv"
    return RAW_DIR / f"rtt_incomplete_provider_{y}{m:02d}.xlsx"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="run a visible browser")
    ap.add_argument("--list", action="store_true", help="list matched links only")
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        links = {"ae": {}, "rtt": {}}
        for kind, pages, matcher in (("ae", AE_PAGES, _match_ae), ("rtt", RTT_PAGES, _match_rtt)):
            for url in pages:
                status, found = collect_links(page, url, matcher)
                print(f"[{kind}] HTTP {status} {url} -> {len(found)} links")
                links[kind].update(found)

        for kind in ("ae", "rtt"):
            for key in sorted(links[kind]):
                text, href, revised = links[kind][key]
                print(f"  {kind} {key[0]}-{key[1]:02d}: {text!r} -> {href}")

        if args.list:
            browser.close()
            return

        # A&E and RTT must end on the same month: the latest both have.
        end = min(max(links["ae"]), max(links["rtt"]))
        print(f"Common end month: {end[0]}-{end[1]:02d}")
        for kind in ("ae", "rtt"):
            for key in sorted(k for k in links[kind] if k <= end):
                dest = target(kind, key)
                if dest.exists() and not args.force:
                    print(f"  skip (exists) {dest.name}")
                    continue
                href = links[kind][key][1]
                resp = ctx.request.get(href, timeout=120000)
                if not resp.ok or not resp.body():
                    raise SystemExit(f"Download failed ({resp.status}) for {href}")
                dest.write_bytes(resp.body())
                print(f"  saved {dest.name} ({dest.stat().st_size} bytes)")
        browser.close()


if __name__ == "__main__":
    main()
