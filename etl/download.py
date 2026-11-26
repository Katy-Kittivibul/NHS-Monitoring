"""Download monthly NHS England A&E (CSV) and RTT incomplete-provider (XLSX) files.

The england.nhs.uk listing pages return HTTP 202 with an empty body to plain
HTTP clients, so the pages are rendered with Playwright (Chromium) and the file
links are read from the DOM. Files are saved to data/raw/ using the names the
ETL expects:

    ae_attendances_YYYYMM.csv
    rtt_incomplete_provider_YYYYMM.xlsx

NHSBSA dental activity (dental_uda_YYYYMM.csv) comes from the NHSBSA Open Data Portal's
CKAN API over plain HTTP, so it needs neither Playwright nor a browser.

GP prescribing is deliberately NOT downloaded here. OpenPrescribing's API sits behind a
Cloudflare bot challenge (HTTP 403 "Just a moment...") for scripted clients, and this
project does not try to evade it; the NHSBSA English Prescribing Dataset it is built on
is ~7 GB per month and currently ends at 2025-06. data/raw/openprescribing_spending_*.json
therefore stays a manual download (see README).

Usage:
    python -m etl.download --list              # print matched links, download nothing
    python -m etl.download                      # download missing A&E + RTT files
    python -m etl.download --headed --force     # headed browser, re-download everything
    python -m etl.download --datasets dental    # NHSBSA dental only (no browser needed)
    python -m etl.download --datasets ae,rtt,dental --end 2026-08
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
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


DENTAL_PACKAGE_URL = (
    "https://opendata.nhsbsa.net/api/3/action/package_show"
    "?id=english-contractor-monthly-general-dental-activity"
)
USER_AGENT = "nhs-monitoring-copilot/0.1 (public-data research project)"
DENTAL_RESOURCE_RE = re.compile(r"^UDA_CONTRACTOR_(\d{4})(\d{2})$", re.I)


def http_get(url: str, retries: int = 3, timeout: int = 120, backoff: float = 2.0) -> bytes:
    """GET with a timeout and exponential backoff on transient failures (5xx, 429, network).
    4xx other than 429 fail immediately: retrying a 403/404 cannot help."""
    last_error = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                raise
            last_error = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
        if attempt < retries - 1:
            time.sleep(backoff ** attempt)
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_error}")


def dental_resource_urls(package: dict) -> dict:
    """{(year, month): csv_url} from an NHSBSA CKAN package_show response."""
    found = {}
    for resource in package["result"]["resources"]:
        match = DENTAL_RESOURCE_RE.match(resource.get("name", ""))
        if match and resource.get("url"):
            found[(int(match.group(1)), int(match.group(2)))] = resource["url"]
    return found


def looks_like_dental_csv(body: bytes) -> bool:
    """Guard against saving an HTML error/challenge page as a CSV."""
    head = body[:2000].lstrip(b"\xef\xbb\xbf").lstrip().upper()
    return head.startswith(b"YEAR_MONTH") or head.startswith(b'"YEAR_MONTH"')


def download_dental(force: bool = False, list_only: bool = False, end=None, raw_dir: Path = None) -> list:
    """Download dental_uda_YYYYMM.csv for every published month from START up to `end`
    (default: the latest published). Returns the list of files written."""
    raw_dir = Path(raw_dir or RAW_DIR)
    package = json.loads(http_get(DENTAL_PACKAGE_URL, timeout=60))
    available = dental_resource_urls(package)
    urls = {k: v for k, v in available.items() if k >= START and (end is None or k <= end)}
    span = f" to {max(urls)[0]}-{max(urls)[1]:02d}" if urls else ""
    print(f"[dental] NHSBSA -> {len(urls)} months from {START[0]}-{START[1]:02d}{span}")
    written = []
    if not list_only:
        raw_dir.mkdir(parents=True, exist_ok=True)
    for key in sorted(urls):
        dest = raw_dir / f"dental_uda_{key[0]}{key[1]:02d}.csv"
        if list_only:
            print(f"  dental {key[0]}-{key[1]:02d}: {urls[key]}")
            continue
        if dest.exists() and not force:
            print(f"  skip (exists) {dest.name}")
            continue
        body = http_get(urls[key])
        if not looks_like_dental_csv(body):
            raise SystemExit(f"{urls[key]} did not return a dental CSV (first bytes: {body[:80]!r})")
        tmp = dest.with_suffix(".csv.part")
        tmp.write_bytes(body)
        tmp.replace(dest)  # atomic: a killed download never leaves a truncated .csv
        print(f"  saved {dest.name} ({len(body)} bytes)")
        written.append(dest)
    return written


def parse_end(text):
    if not text:
        return None
    match = re.fullmatch(r"(\d{4})-(\d{2})", text.strip())
    if not match:
        raise SystemExit(f"--end must look like 2026-08, got {text!r}")
    return int(match.group(1)), int(match.group(2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="run a visible browser")
    ap.add_argument("--list", action="store_true", help="list matched links only")
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    ap.add_argument("--datasets", default="ae,rtt",
                    help="comma-separated subset of: ae, rtt, dental (default: ae,rtt)")
    ap.add_argument("--end", help="last month to fetch, YYYY-MM (default: latest published)")
    args = ap.parse_args()

    wanted = {d.strip().lower() for d in args.datasets.split(",") if d.strip()}
    unknown = wanted - {"ae", "rtt", "dental"}
    if unknown:
        raise SystemExit(f"unknown dataset(s): {', '.join(sorted(unknown))}")
    end = parse_end(args.end)

    if "dental" in wanted:
        download_dental(force=args.force, list_only=args.list, end=end)
    if not wanted & {"ae", "rtt"}:
        return

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "Playwright is required for A&E/RTT downloads: pip install -e '.[ingest]' "
            "&& playwright install chromium"
        )

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        links = {"ae": {}, "rtt": {}}
        for kind, pages, matcher in (("ae", AE_PAGES, _match_ae), ("rtt", RTT_PAGES, _match_rtt)):
            if kind not in wanted:
                continue
            for url in pages:
                status, found = collect_links(page, url, matcher)
                print(f"[{kind}] HTTP {status} {url} -> {len(found)} links")
                links[kind].update(found)

        for kind in sorted(wanted & {"ae", "rtt"}):
            for key in sorted(links[kind]):
                text, href, revised = links[kind][key]
                print(f"  {kind} {key[0]}-{key[1]:02d}: {text!r} -> {href}")

        if args.list:
            browser.close()
            return

        # A&E and RTT must end on the same month: the latest both have (or --end).
        latest = [max(links[k]) for k in ("ae", "rtt") if k in wanted and links[k]]
        if not latest:
            browser.close()
            raise SystemExit("no matching A&E/RTT links found")
        stop = min(latest + ([end] if end else []))
        print(f"Common end month: {stop[0]}-{stop[1]:02d}")
        for kind in sorted(wanted & {"ae", "rtt"}):
            for key in sorted(k for k in links[kind] if k <= stop):
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
