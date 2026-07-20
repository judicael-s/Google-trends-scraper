#!/usr/bin/env python3
"""Rotating Google Trends cron wrapper for the Windows-side Playwright runner.

This script is designed to be called from Hermes cron in WSL. It chooses one
query per tick, calls ``run-trends.ps1`` on Windows, stores raw JSON output, and
prints a concise summary. It never treats Google Trends as volume truth.
"""
from __future__ import annotations

import argparse
import json
import os
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


def format_path_template(value: str, config: dict[str, Any], now: datetime) -> str:
    return value.format(
        client_id=str(config.get("client_id", "client")),
        date=now.strftime("%Y-%m-%d"),
        yyyymmdd=now.strftime("%Y%m%d"),
    )


def resolve_log_destination_path(destination: dict[str, Any], config: dict[str, Any], config_dir: Path, now: datetime) -> Path | None:
    raw_path = destination.get("path") or destination.get("relative_path")
    if not raw_path:
        return None
    path_text = format_path_template(str(raw_path), config, now)
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path
    dest_type = str(destination.get("type", "markdown")).lower()
    if dest_type.startswith("obsidian"):
        vault = destination.get("vault_path") or config.get("search_log", {}).get("obsidian_vault_path") or os.environ.get("OBSIDIAN_VAULT_PATH")
        if not vault:
            return None
        return Path(str(vault)).expanduser() / path
    base = destination.get("base_dir") or config.get("search_log", {}).get("base_dir")
    return (Path(str(base)).expanduser() if base else config_dir) / path



def _parse_trends_date(point: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(point.get("time")), tz=timezone.utc)
    except Exception:
        return None


def _month_label(dt: datetime | None, fallback: str = "") -> str:
    if not dt:
        return fallback
    return dt.strftime("%b %Y")


def classify_seasonality(points: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify yearly Google Trends timing from weekly interest points.

    Google Trends values are normalized indexes, not volumes. This summary is a
    planning aid: it highlights peak windows and evergreen/sparse patterns.
    """
    clean: list[dict[str, Any]] = []
    for point in points or []:
        try:
            value = int(point.get("value") or 0)
        except (TypeError, ValueError):
            value = 0
        dt = _parse_trends_date(point)
        clean.append({"dt": dt, "label": point.get("formatted_time", ""), "value": value})
    total = len(clean)
    if total == 0:
        return {
            "seasonality": "no timeline data",
            "active_weeks": "0/0",
            "active_share": 0,
            "peak_weeks": "none",
            "peak_months": "none",
            "seasonality_detail": "No weekly interest_over_time points were captured; this can be true low Trends signal, UI/rate-limit noise, or a very narrow exact query.",
        }

    nonzero = [p for p in clean if p["value"] > 0]
    strong = [p for p in clean if p["value"] >= 50]
    moderate = [p for p in clean if p["value"] >= 20]
    active_share = len(nonzero) / total if total else 0
    top = sorted(clean, key=lambda p: p["value"], reverse=True)[:3]
    top_nonzero = [p for p in top if p["value"] > 0]
    peak_weeks = "; ".join(f"{p['value']} — {p['label']}" for p in top_nonzero) or "none"
    # Month max + active weeks, sorted by max then active count.
    by_month: dict[str, list[int]] = {}
    for p in clean:
        if p["value"] <= 0:
            continue
        label = _month_label(p["dt"], str(p["label"])[:12])
        by_month.setdefault(label, []).append(p["value"])
    month_rank = sorted(by_month.items(), key=lambda kv: (max(kv[1]), len(kv[1])), reverse=True)[:3]
    peak_months = "; ".join(f"{m} max {max(vals)} ({len(vals)} active wk)" for m, vals in month_rank) or "none"

    if len(nonzero) == 0:
        seasonality = "no visible Trends demand"
        detail = "All captured weeks were zero in Google Trends; keep only if GSC/SERP/product fit supports it."
    elif active_share >= 0.60 and len(strong) >= 8:
        seasonality = "evergreen / all-year"
        detail = f"Active in {len(nonzero)}/{total} weeks with repeated strong weeks; use as an evergreen SEO anchor."
    elif active_share >= 0.35:
        seasonality = "recurring / broad-season"
        detail = f"Active in {len(nonzero)}/{total} weeks; useful across several months, with strongest windows: {peak_months}."
    elif len(strong) >= 2 and len({(_month_label(p['dt'], p['label'])) for p in strong}) >= 2:
        seasonality = "sparse multi-peak"
        detail = f"Not all-year-round in Trends: only {len(nonzero)}/{total} active weeks, but separated strong peaks appear around {peak_months}."
    elif len(strong) >= 1 or len(moderate) >= 2:
        seasonality = "seasonal peak"
        detail = f"Demand concentrates in a short window: {peak_weeks}. Plan content/internal links before those weeks."
    else:
        seasonality = "sparse / low Trends signal"
        detail = f"Only {len(nonzero)}/{total} active weeks and no strong peak; treat as long-tail/support unless GSC/SERP validates it."

    return {
        "seasonality": seasonality,
        "active_weeks": f"{len(nonzero)}/{total}",
        "active_share": round(active_share, 3),
        "strong_weeks": len(strong),
        "moderate_weeks": len(moderate),
        "peak_weeks": peak_weeks,
        "peak_months": peak_months,
        "seasonality_detail": detail,
    }

def append_markdown_log(path: Path, record: dict[str, Any], config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        client = config.get("client_id", "client")
        path.write_text(
            f"# Google Trends searched keywords — {client}\n\n"
            "Discovery log for Google Trends radar checks. Values are Google Trends ideation signals, not absolute search volume. Validate with GSC, SERP, GA4, DataForSEO or Ads before prioritizing content.\n\n"
            "| Time UTC | Topic | Query | Intent | Source | Status | Timeline | Latest / Mean / Max / Delta | Seasonality | Active weeks | Peak weeks | Peak months | Detail | Raw JSON |\n"
            "|---|---|---|---|---|---|---:|---|---|---:|---|---|---|---|\n",
            encoding="utf-8",
        )
    query = str(record.get("query", "")).replace("|", "\\|")
    topic = str(record.get("topic", "")).replace("|", "\\|")
    intent = str(record.get("intent", "")).replace("|", "\\|")
    source = str(record.get("source", "")).replace("|", "\\|")
    raw = str(record.get("raw_path", "")).replace("|", "\\|")
    seasonality = str(record.get("seasonality", "")).replace("|", "\\|")
    active_weeks = str(record.get("active_weeks", "")).replace("|", "\\|")
    peak_weeks = str(record.get("peak_weeks", "")).replace("|", "\\|")
    peak_months = str(record.get("peak_months", "")).replace("|", "\\|")
    detail = str(record.get("seasonality_detail", "")).replace("|", "\\|")
    values = f"{record.get('latest_value')} / {record.get('mean_value')} / {record.get('max_value')} / {record.get('trend_delta')}"
    topic_column = "| Topic |" in path.read_text(encoding="utf-8", errors="replace")[:4000]
    with path.open("a", encoding="utf-8") as handle:
        if topic_column:
            handle.write(
                f"| {record.get('fetched_at', '')} | {topic} | {query} | {intent} | {source} | {record.get('status', '')} | {record.get('timeline_points', '')} | {values} | {seasonality} | {active_weeks} | {peak_weeks} | {peak_months} | {detail} | `{raw}` |\n"
            )
        else:
            handle.write(
                f"| {record.get('fetched_at', '')} | {query} | {intent} | {source} | {record.get('status', '')} | {record.get('timeline_points', '')} | {values} | {seasonality} | {active_weeks} | {peak_weeks} | {peak_months} | {detail} | `{raw}` |\n"
            )


def append_jsonl_log(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def append_search_log(config: dict[str, Any], config_dir: Path, query_item: dict[str, Any], payload: dict[str, Any], raw_path: Path, now: datetime) -> None:
    search_log = config.get("search_log") or {}
    if search_log.get("enabled") is False:
        return
    destinations = search_log.get("destinations") or []
    if not isinstance(destinations, list) or not destinations:
        return
    errors = payload.get("errors") or []
    rows = payload.get("rows") or []
    summary = (rows[0].get("summary", {}) if rows else {}) if isinstance(rows, list) else {}
    timeline_points = []
    if isinstance(rows, list) and rows:
        timeline_points = rows[0].get("interest_over_time") or []
    seasonality = classify_seasonality(timeline_points)
    record = {
        "fetched_at": now.isoformat(),
        "client_id": config.get("client_id", "unknown-client"),
        "site_url": config.get("site_url", ""),
        "query": query_item.get("query", ""),
        "topic": item_topic(query_item),
        "intent": query_item.get("intent", ""),
        "source": query_item.get("source", ""),
        "selection_score": query_item.get("selection_score"),
        "geo": query_item.get("geo", config.get("geo", "FR")),
        "hl": query_item.get("hl", config.get("hl", "fr-FR")),
        "timeframe": query_item.get("timeframe", config.get("timeframe", "today 12-m")),
        "status": "error" if errors else "ok",
        "error_codes": [err.get("code") for err in errors],
        "timeline_points": summary.get("points"),
        "latest_value": summary.get("latest_value"),
        "mean_value": summary.get("mean_value"),
        "max_value": summary.get("max_value"),
        "trend_delta": summary.get("trend_delta"),
        "region_count": summary.get("region_count"),
        "related_query_count": summary.get("related_query_count"),
        "raw_path": str(raw_path),
        "validation_status": "trends_ideation_only",
        **seasonality,
    }
    for destination in destinations:
        if not isinstance(destination, dict) or destination.get("enabled") is False:
            continue
        path = resolve_log_destination_path(destination, config, config_dir, now)
        if path is None:
            continue
        dest_type = str(destination.get("type", "markdown")).lower()
        if dest_type == "jsonl":
            append_jsonl_log(path, record)
        elif dest_type in {"markdown", "md", "obsidian", "obsidian_markdown"}:
            append_markdown_log(path, record, config)


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


def query_identity(item: dict[str, Any], config: dict[str, Any]) -> str:
    query = " ".join(str(item.get("query", "")).lower().split())
    geo = str(item.get("geo", config.get("geo", ""))).upper()
    timeframe = str(item.get("timeframe", config.get("timeframe", "today 12-m"))).lower()
    return f"{query}::{geo}::{timeframe}"


def item_topic(item: dict[str, Any]) -> str:
    return str(item.get("topic") or item.get("bucket") or "unbucketed")


def choose_query(config: dict[str, Any], state: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    min_hours = float(config.get("min_hours_between_same_query", 24))
    queries = config.get("queries", [])
    if not isinstance(queries, list) or not queries:
        raise ValueError("Config must contain a non-empty queries list")
    query_state = state.setdefault("queries", {})

    due: list[dict[str, Any]] = []
    for item in queries:
        if isinstance(item, str):
            item = {"query": item}
        query = item.get("query")
        if not query:
            continue
        identity = query_identity(item, config)
        # Read both the current geo/timeframe-aware key and the old exact-query
        # key so upgrades do not immediately repeat previously checked terms.
        last_checked = (query_state.get(identity) or query_state.get(query) or {}).get("last_checked_at")
        age = hours_since(last_checked, now)
        if age is None or age >= min_hours:
            due.append(item)
    if not due:
        return None

    # Rotate across topical buckets instead of exhausting one narrow cluster.
    configured_topics = config.get("query_selection", {}).get("topic_mix") or config.get("topic_mix") or []
    topic_order = [str(topic) for topic in configured_topics]
    for item in due:
        topic = item_topic(item)
        if topic not in topic_order:
            topic_order.append(topic)
    if len(topic_order) <= 1:
        return due[0]
    last_topic = str(state.get("last_topic") or "")
    start = (topic_order.index(last_topic) + 1) if last_topic in topic_order else 0
    for offset in range(len(topic_order)):
        wanted = topic_order[(start + offset) % len(topic_order)]
        match = next((item for item in due if item_topic(item) == wanted), None)
        if match:
            return match
    return due[0]


def should_emit_summary(config: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Control no-agent cron stdout without suppressing durable logs."""
    policy = config.get("alert_policy") or {}
    mode = str(policy.get("mode", "always")).lower()
    errors = payload.get("errors") or []
    if errors:
        return bool(policy.get("alert_on_errors", True)) and mode != "silent"
    if mode == "always":
        return True
    if mode in {"silent", "errors_only"}:
        return False
    if mode != "opportunities_only":
        return True
    rows = payload.get("rows") or []
    if not rows:
        return False
    row = rows[0]
    summary = row.get("summary") or {}
    latest = float(summary.get("latest_value") or 0)
    delta = float(summary.get("trend_delta") or 0)
    related = int(summary.get("related_query_count") or 0)
    seasonality = classify_seasonality(row.get("interest_over_time") or []).get("seasonality")
    return bool(
        latest >= float(policy.get("min_latest_value", 60))
        or delta >= float(policy.get("min_trend_delta", 25))
        or related >= int(policy.get("min_related_queries", 3))
        or seasonality in set(policy.get("alert_on_seasonality") or [])
    )


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
    seasonality = classify_seasonality(row.get("interest_over_time") or [])
    lines.extend([
        f"Seasonality: {seasonality.get('seasonality')}",
        f"Active weeks: {seasonality.get('active_weeks')}",
        f"Peak weeks: {seasonality.get('peak_weeks')}",
        f"Peak months: {seasonality.get('peak_months')}",
        f"Detail: {seasonality.get('seasonality_detail')}",
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
    append_search_log(config, config_path.parent, query_item, payload, raw_path, now)

    state_key = query_identity(query_item, config)
    query_state = state.setdefault("queries", {}).setdefault(state_key, {})
    query_state["query"] = query_item["query"]
    query_state["topic"] = item_topic(query_item)
    query_state["last_checked_at"] = now.isoformat()
    query_state["last_raw_path"] = str(raw_path)
    query_state["last_error_codes"] = [err.get("code") for err in payload.get("errors", [])]
    state["last_run_at"] = now.isoformat()
    state["last_topic"] = item_topic(query_item)
    save_json(state_path, state)

    if should_emit_summary(config, payload):
        print(summarize(config, payload, query_item, raw_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
