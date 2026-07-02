import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_fixture_output_contract():
    proc = subprocess.run([
        "node", str(ROOT / "trends_runner.js"),
        "--fixture", str(ROOT / "fixtures" / "google_trends_sample.json"),
    ], cwd=ROOT, text=True, capture_output=True, check=True)
    data = json.loads(proc.stdout)
    assert data["connector"] == "google_trends_playwright_windows"
    assert data["mode"] == "windows-fixture"
    assert data["rows"]
    row = data["rows"][0]
    assert row["interest_over_time"]
    assert row["interest_by_region"]
    assert row["related_queries"]
    assert row["validation_status"] == "trends_ideation_only"


def test_required_files_exist():
    for rel in [
        "trends_runner.js",
        "run-trends.ps1",
        "open-trends-profile.ps1",
        "rotating_trends_cron.py",
        "examples/client-trends-radar.config.json",
        "README.md",
    ]:
        assert (ROOT / rel).exists(), rel
