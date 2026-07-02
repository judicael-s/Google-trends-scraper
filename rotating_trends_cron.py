#!/usr/bin/env python3
"""Rotating Google Trends cron wrapper for the Windows-side Playwright runner.

This script is designed to be called from Hermes cron in WSL. It chooses one
query per tick, calls ``run-trends.ps1`` on Windows, stores raw JSON output, and
prints a concise summary. It never treats Google Trends as volume truth.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def hours_since(value: str | None, now: datetime) -> float | None:
    parsed = parse_time(value)
    if not parsed:
        return None
    return (now - parsed).total_seconds() / 3600


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def wslpath_windows(path: Path) -> str:
    proc = subprocess.run(["wslpath", "-w", str(path)], text=True, capture_output=True, check=True)
    return proc.stdout.strip()


def extract_json(stdout: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for idx, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(stdout[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("Runner stdout did not contain a JSON object")


def choose_query(config: dict[str, Any], state: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    min_hours = float(config.get("min_hours_between_same_query", 24))
    queries = config.get("queries", [])
    if not isinstance(queries, list) or not queries:
        raise ValueError("Config must contain a non-empty queries list")
    query_state = state.setdefault("queries", {})

    for item in queries:
        if isinstance(item, str):
            item = {"query": item}
        query = item.get("query")
        if not query:
            continue
        last_checked = query_state.get(query, {}).get("last_checked_at")
        age = hours_since(last_checked, now)
        if age is None or age >= min_hours:
            return item
    return None


def run_runner(args: argparse.Namespace, config: dict[str, Any], query_item: dict[str, Any]) -> dict[str, Any]:
    runner_ps1 = Path(args.runner_ps1).resolve()
    if not runner_ps1.exists():
        raise FileNotFoundError(f"Runner PowerShell script not found: {runner_ps1}")

    query = query_item["query"]
    geo = query_item.get("geo", config.get("geo", "FR"))
    hl = query_item.get("hl", config.get("hl", "fr-FR"))
    timeframe = query_item.get("timeframe", config.get("timeframe", "today 12-m"))
    region_resolution = query_item.get("region_resolution", config.get("region_resolution", "REGION"))

    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        wslpath_windows(runner_ps1),
        "-Query",
        query,
        "-Geo",
        geo,
        "-Hl",
        hl,
        "-Timeframe",
        timeframe,
        "-RegionResolution",
        region_resolution,
        "-KeepOpenMs",
        str(int(config.get("keep_open_ms", 3500))),
    ]
    if args.runner_fixture:
        cmd.extend(["-Fixture", wslpath_windows(Path(args.runner_fixture).resolve())])
    if config.get("browser_channel"):
        cmd.extend(["-BrowserChannel", str(config["browser_channel"])])
    if config.get("user_data_dir"):
        cmd.extend(["-UserDataDir", str(config["user_data_dir"])])

    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=int(config.get("timeout_seconds", 180)))
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    try:
        payload = extract_json(stdout)
    except Exception as exc:
        payload = {
            "connector": "google_trends_playwright_windows",
            "mode": "runner-error",
            "fetched_at": utc_now().isoformat(),
            "params": {"queries": [query], "geo": geo, "hl": hl, "timeframe": timeframe, "region_resolution": region_resolution},
            "rows": [],
            "warnings": [],
            "errors": [{"code": "RUNNER_STDOUT_PARSE_FAILED", "message": str(exc), "context": {"returncode": proc.returncode, "stdout_tail": stdout[-500:], "stderr_tail": stderr[-500:]}}],
        }
    if proc.returncode != 0 and not payload.get("errors"):
        payload.setdefault("errors", []).append({"code": "RUNNER_EXIT_NONZERO", "message": f"Runner exited with {proc.returncode}", "context": {"stderr_tail": stderr[-500:]}})
    return payload


def summarize(config: dict[str, Any], payload: dict[str, Any], query_item: dict[str, Any], raw_path: Path) -> str:
    client = config.get("client_id", "unknown-client")
    query = query_item["query"]
    errors = payload.get("errors") or []
    warnings = payload.get("warnings") or []
    rows = payload.get("rows") or []
    lines = [f"Google Trends radar: {client}", f"Query: {query}", f"Raw: {raw_path}"]
    if errors:
        lines.append("Status: error")
        for err in errors[:3]:
            lines.append(f"- {err.get('code')}: {err.get('message')}")
        return "\n".join(lines)
    lines.append("Status: ok")
    if warnings:
        lines.append("Warnings: " + "; ".join(f"{w.get('code')}: {w.get('message')}" for w in warnings[:3]))
    if not rows:
        lines.append("Rows: 0")
        return "\n".join(lines)
    row = rows[0]
    summary = row.get("summary", {})
    lines.extend([
        f"Timeline points: {summary.get('points')}",
        f"Latest/mean/max/delta: {summary.get('latest_value')} / {summary.get('mean_value')} / {summary.get('max_value')} / {summary.get('trend_delta')}",
        f"Regions: {summary.get('region_count')}",
        f"Related queries: {summary.get('related_query_count')}",
        "Validation: trends_ideation_only",
    ])
    related = row.get("related_queries") or []
    if related:
        lines.append("Top related: " + ", ".join(item.get("query", "") for item in related[:5] if item.get("query")))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to client Trends radar config JSON")
    parser.add_argument("--state", required=True, help="Path to persistent rotation state JSON")
    parser.add_argument("--output-dir", required=True, help="Directory for raw runner JSON outputs")
    parser.add_argument("--runner-ps1", default=str(Path(__file__).with_name("run-trends.ps1")))
    parser.add_argument("--runner-fixture", default="", help="Offline fixture path for tests")
    args = parser.parse_args()

    now = utc_now()
    config_path = Path(args.config).resolve()
    state_path = Path(args.state).resolve()
    output_dir = Path(args.output_dir).resolve()
    config = load_json(config_path, {})
    state = load_json(state_path, {"queries": {}})

    query_item = choose_query(config, state, now)
    if query_item is None:
        # Silent skip is intentional for Hermes no_agent cron jobs.
        return 0

    payload = run_runner(args, config, query_item)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    safe_client = str(config.get("client_id", "client")).replace("/", "-")
    safe_query = "".join(ch if ch.isalnum() else "-" for ch in query_item["query"].lower()).strip("-")[:80]
    raw_path = output_dir / f"{stamp}-{safe_client}-{safe_query}.json"
    save_json(raw_path, payload)

    query_state = state.setdefault("queries", {}).setdefault(query_item["query"], {})
    query_state["last_checked_at"] = now.isoformat()
    query_state["last_raw_path"] = str(raw_path)
    query_state["last_error_codes"] = [err.get("code") for err in payload.get("errors", [])]
    state["last_run_at"] = now.isoformat()
    save_json(state_path, state)

    print(summarize(config, payload, query_item, raw_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
