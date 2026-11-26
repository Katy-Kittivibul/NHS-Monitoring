"""Hermetic tests for etl/download.py's HTTP (dental) path and the dental loader changes
that came with newer NHSBSA files. No network: urlopen / http_get are monkeypatched."""
import json
import urllib.error

import pytest

from etl import download
from etl.dental_uda import load_dental_uda_file, parse_dental_month

HEADER_OLD = (
    "YEAR_MONTH,COMMISSIONER_CODE,COMMISSIONER_NAME,CONTRACT_NUMBER,PROVIDER_NAME,UDA_PERF_TARGET,"
    "UDA_DELIVERED,BAND_1_DELIVERED,BAND_2A_DELIVERED,BAND_2B_DELIVERED,BAND_2C_DELIVERED,"
    "BAND_3_DELIVERED,BAND_URGENT_DELIVERED\n"
)
HEADER_NEW = HEADER_OLD.replace("BAND_URGENT_DELIVERED", "BAND_UNSCHEDULED_DELIVERED")
ROW = "{ym},QYG,NHS Cheshire ICB,1000080000,{name},1000,900,10,20,5,1,3,{urgent}\n"


class TestParseDentalMonth:
    @pytest.mark.parametrize("raw", ["202608", 202608, "2026-08", " 2026-08 "])
    def test_both_formats(self, raw):
        assert parse_dental_month(raw).isoformat() == "2026-08-01"

    @pytest.mark.parametrize("raw", ["2026-13", "abc", "", "20261"])
    def test_invalid_raises(self, raw):
        with pytest.raises(ValueError):
            parse_dental_month(raw)


class TestDentalLoader:
    def test_unscheduled_column_maps_to_urgent(self, tmp_path):
        path = tmp_path / "d.csv"
        path.write_text(HEADER_NEW + ROW.format(ym="2026-08", name="Dental Ltd", urgent=42))
        df = load_dental_uda_file(str(path))
        assert df["band_urgent_delivered"].tolist() == [42.0]
        assert df["month_date"].iloc[0].isoformat() == "2026-08-01"
        assert df["band_2_delivered"].tolist() == [26.0]

    def test_cp1252_file_is_read(self, tmp_path):
        path = tmp_path / "d.csv"
        text = HEADER_OLD + ROW.format(ym="202504", name="Café Dental £", urgent=1)
        path.write_bytes(text.encode("cp1252"))
        df = load_dental_uda_file(str(path))
        assert df["provider_name"].iloc[0] == "Café Dental £"

    def test_utf8_bom_file_is_read(self, tmp_path):
        path = tmp_path / "d.csv"
        text = HEADER_OLD + ROW.format(ym="202504", name="X", urgent=1)
        path.write_bytes(b"\xef\xbb\xbf" + text.encode())
        assert load_dental_uda_file(str(path)).columns[0] == "month_date"


PACKAGE = {"result": {"resources": [
    {"name": "UDA_CONTRACTOR_202503", "url": "https://x/uda_202503.csv"},
    {"name": "UDA_CONTRACTOR_202504", "url": "https://x/uda_202504.csv"},
    {"name": "UDA_CONTRACTOR_202505", "url": "https://x/uda_202505.csv"},
    {"name": "UDA_ORTHODONTIC_202505", "url": "https://x/other.csv"},
    {"name": "UDA_CONTRACTOR_202506", "url": ""},
]}}


class TestDentalResources:
    def test_parses_only_contractor_resources_with_urls(self):
        assert download.dental_resource_urls(PACKAGE) == {
            (2025, 3): "https://x/uda_202503.csv",
            (2025, 4): "https://x/uda_202504.csv",
            (2025, 5): "https://x/uda_202505.csv",
        }

    @pytest.mark.parametrize("body,ok", [
        (b"YEAR_MONTH,COMMISSIONER_CODE\n1", True),
        (b'\xef\xbb\xbf"YEAR_MONTH","X"\n', True),
        (b"<!DOCTYPE html><title>Just a moment...</title>", False),
        (b"", False),
    ])
    def test_csv_sniffing(self, body, ok):
        assert download.looks_like_dental_csv(body) is ok

    def test_parse_end(self):
        assert download.parse_end("2026-08") == (2026, 8)
        assert download.parse_end(None) is None
        with pytest.raises(SystemExit):
            download.parse_end("08/2026")


class TestDownloadDental:
    @pytest.fixture
    def fake_http(self, monkeypatch):
        calls = []

        def fake(url, **kwargs):
            calls.append(url)
            if url == download.DENTAL_PACKAGE_URL:
                return json.dumps(PACKAGE).encode()
            return (HEADER_OLD + "x\n").encode()

        monkeypatch.setattr(download, "http_get", fake)
        return calls

    def test_downloads_from_start_and_skips_existing(self, tmp_path, fake_http):
        # START is 2025-04, so 202503 is never fetched.
        first = download.download_dental(raw_dir=tmp_path)
        assert sorted(p.name for p in first) == ["dental_uda_202504.csv", "dental_uda_202505.csv"]
        assert not list(tmp_path.glob("*.part"))
        again = download.download_dental(raw_dir=tmp_path)
        assert again == []
        forced = download.download_dental(raw_dir=tmp_path, force=True)
        assert len(forced) == 2

    def test_end_caps_range(self, tmp_path, fake_http):
        written = download.download_dental(raw_dir=tmp_path, end=(2025, 4))
        assert [p.name for p in written] == ["dental_uda_202504.csv"]

    def test_list_only_writes_nothing(self, tmp_path, fake_http):
        target = tmp_path / "raw"
        assert download.download_dental(raw_dir=target, list_only=True) == []
        assert not target.exists()

    def test_html_challenge_page_is_not_saved(self, tmp_path, monkeypatch):
        def fake(url, **kwargs):
            if url == download.DENTAL_PACKAGE_URL:
                return json.dumps(PACKAGE).encode()
            return b"<html>Just a moment...</html>"

        monkeypatch.setattr(download, "http_get", fake)
        with pytest.raises(SystemExit, match="did not return a dental CSV"):
            download.download_dental(raw_dir=tmp_path)
        assert not list(tmp_path.glob("dental_uda_*"))


class _Response:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class TestHttpGet:
    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch):
        monkeypatch.setattr(download.time, "sleep", lambda s: None)

    def test_retries_transient_then_succeeds(self, monkeypatch):
        attempts = []

        def urlopen(request, timeout):
            attempts.append(request.get_header("User-agent"))
            if len(attempts) < 3:
                raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)
            return _Response(b"ok")

        monkeypatch.setattr(download.urllib.request, "urlopen", urlopen)
        assert download.http_get("https://x") == b"ok"
        assert len(attempts) == 3
        assert "nhs-monitoring-copilot" in attempts[0]

    def test_403_fails_immediately_without_retry(self, monkeypatch):
        attempts = []

        def urlopen(request, timeout):
            attempts.append(1)
            raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, None)

        monkeypatch.setattr(download.urllib.request, "urlopen", urlopen)
        with pytest.raises(urllib.error.HTTPError):
            download.http_get("https://x")
        assert len(attempts) == 1

    def test_gives_up_after_retries(self, monkeypatch):
        def urlopen(request, timeout):
            raise urllib.error.URLError("down")

        monkeypatch.setattr(download.urllib.request, "urlopen", urlopen)
        with pytest.raises(RuntimeError, match="failed after 3 attempts"):
            download.http_get("https://x")
