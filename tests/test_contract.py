import json
import importlib.util
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        "build_trends_radar_config.py",
        "examples/client-trends-radar.config.json",
        "examples/topic-buckets.json",
        "README.md",
    ]:
        assert (ROOT / rel).exists(), rel


def test_seasonality_and_adaptable_logging(tmp_path):
    cron = load_module("rotating_trends_cron", ROOT / "rotating_trends_cron.py")
    points = [
        {"time": "1760000000", "formatted_time": "Oct 2025", "value": 79},
        {"time": "1778000000", "formatted_time": "May 2026", "value": 100},
    ] + [{"time": str(1778000000 + i * 604800), "formatted_time": f"week {i}", "value": 0} for i in range(51)]
    seasonality = cron.classify_seasonality(points)
    assert seasonality["seasonality"] == "sparse multi-peak"
    assert seasonality["active_weeks"] == "2/53"

    config = {
        "client_id": "example",
        "site_url": "https://example.com",
        "search_log": {"destinations": [
            {"type": "markdown", "path": "searched.md"},
            {"type": "jsonl", "path": "searched.jsonl"},
        ]},
    }
    payload = {"errors": [], "rows": [{"interest_over_time": points, "summary": {
        "points": 53, "latest_value": 0, "mean_value": 3.38, "max_value": 100,
        "trend_delta": 0, "region_count": 2, "related_query_count": 0,
    }}]}
    cron.append_search_log(
        config,
        tmp_path,
        {"query": "prepare main offer", "topic": "core_offers", "intent": "test", "source": "fixture"},
        payload,
        tmp_path / "raw.json",
        datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    assert "prepare main offer" in (tmp_path / "searched.md").read_text(encoding="utf-8")
    row = json.loads((tmp_path / "searched.jsonl").read_text(encoding="utf-8"))
    assert row["seasonality"] == "sparse multi-peak"
    assert row["raw_path"].endswith("raw.json")


def test_topic_rotation_and_opportunity_only_alerts():
    cron = load_module("rotating_trends_cron_rotation", ROOT / "rotating_trends_cron.py")
    config = {
        "geo": "US",
        "timeframe": "today 12-m",
        "query_selection": {"topic_mix": ["core", "pain", "seasonal"]},
        "queries": [
            {"query": "main product", "topic": "core"},
            {"query": "customer pain", "topic": "pain"},
            {"query": "holiday product", "topic": "seasonal"},
        ],
        "alert_policy": {"mode": "opportunities_only", "min_latest_value": 60},
    }
    chosen = cron.choose_query(config, {"queries": {}, "last_topic": "core"}, datetime.now(timezone.utc))
    assert chosen["topic"] == "pain"
    quiet = {"errors": [], "rows": [{"interest_over_time": [], "summary": {"latest_value": 10, "trend_delta": 0, "related_query_count": 0}}]}
    hot = {"errors": [], "rows": [{"interest_over_time": [], "summary": {"latest_value": 80, "trend_delta": 0, "related_query_count": 0}}]}
    assert cron.should_emit_summary(config, quiet) is False
    assert cron.should_emit_summary(config, hot) is True


def test_config_builder_uses_short_topic_buckets_and_excludes_history(tmp_path):
    topic_path = tmp_path / "topics.json"
    state_path = tmp_path / "state.json"
    out_path = tmp_path / "config.json"
    topic_path.write_text(json.dumps({"topics": [
        {"name": "core", "queries": ["main offer", "buy main offer"]},
        {"name": "pain", "queries": ["customer problem", "fix customer problem"]},
    ]}), encoding="utf-8")
    state_path.write_text(json.dumps({"queries": {"main offer::US::today 12-m": {"query": "main offer"}}}), encoding="utf-8")
    proc = subprocess.run([
        "python", str(ROOT / "build_trends_radar_config.py"),
        "--client-id", "example", "--site-url", "https://example.com",
        "--geo", "US", "--hl", "en-US", "--topic-json", str(topic_path),
        "--state-json", str(state_path), "--output", str(out_path),
    ], cwd=ROOT, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    config = json.loads(out_path.read_text(encoding="utf-8"))
    queries = {item["query"] for item in config["queries"]}
    assert "main offer" not in queries
    assert {item["topic"] for item in config["queries"]} == {"core", "pain"}
    assert config["alert_policy"]["mode"] == "opportunities_only"
