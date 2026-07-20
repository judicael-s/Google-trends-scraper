#!/usr/bin/env python3
"""Build a Google Trends radar config from audit/preflight/client evidence.

The tool is intentionally conservative: it turns website keywords, page titles,
competitor hints, GSC queries, and manual seeds into short Google Trends-ready
queries. It does not use Trends as search volume truth and does not create cron
jobs by itself.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

DEFAULT_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "de", "des", "du", "en", "et", "for", "from",
    "in", "is", "it", "la", "le", "les", "of", "on", "or", "our", "pour", "the", "to", "un", "une",
    "with", "your", "vous", "nous", "that", "this", "dans", "sur", "plus", "site", "home", "accueil",
    "contact", "conditions", "mentions", "privacy", "politique", "cookies", "panier", "checkout", "connexion",
    "blog", "article", "articles", "page", "pages", "produit", "produits", "collection", "collections",
    "acheter", "vente", "voir", "tout", "tous", "toutes", "mon", "ma", "mes", "nos", "vos", "leur", "leurs",
}

FRENCH_STOPWORDS = DEFAULT_STOPWORDS | {
    "ce", "ces", "cet", "cette", "comment", "est", "être", "etre", "faire", "ils", "par", "pas", "qui", "quoi",
    "sa", "ses", "son", "sont", "où", "ou", "aux", "au", "dun", "dune", "avec", "sans", "chez", "comme",
}

COMMERCIAL_INTENT_TERMS = {
    "price", "pricing", "cost", "buy", "best", "review", "reviews", "compare", "comparison", "versus", "vs",
    "alternative", "alternatives", "quote", "demo", "trial", "discount", "deal", "service", "software", "course",
    "training", "template", "consultant", "agency", "nearby", "local", "free",
    "prix", "tarif", "coût", "cout", "acheter", "comparatif", "meilleur", "avis", "comparer", "alternative",
    "devis", "démo", "demo", "essai", "réduction", "reduction", "promotion", "service", "logiciel", "formation",
    "stage", "cours", "modèle", "modele", "consultant", "agence", "local", "gratuit",
}


def load_json(path: str | Path | None, default: Any) -> Any:
    if not path:
        return default
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    return json.loads(p.read_text(encoding="utf-8"))


def text_chunks_from_preflight(preflight: dict[str, Any]) -> list[str]:
    chunks: list[str] = []
    for seed in preflight.get("keyword_seeds", []) or []:
        if isinstance(seed, dict) and seed.get("term"):
            chunks.append(str(seed["term"]))
    for page in preflight.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        chunks.extend(str(page.get(key, "")) for key in ["title", "meta_description"])
        chunks.extend(str(x) for x in page.get("h1", []) or [])
        chunks.extend(str(x) for x in page.get("h2", []) or [])
        url = str(page.get("url", ""))
        path = re.sub(r"https?://[^/]+", "", url)
        chunks.append(path.replace("/", " ").replace("-", " ").replace("_", " "))
    for link in preflight.get("navigation", []) or []:
        if isinstance(link, dict):
            chunks.append(str(link.get("text", "")))
    handoff = preflight.get("automatic_workflow_handoff") or {}
    for seed in handoff.get("top_keyword_seeds_for_validation", []) or []:
        if isinstance(seed, dict) and seed.get("term"):
            chunks.append(str(seed["term"]))
    return [c for c in chunks if c and c.strip()]


def text_chunks_from_competitors(competitors: Iterable[str]) -> list[str]:
    chunks: list[str] = []
    for raw in competitors:
        text = str(raw or "").strip()
        if not text:
            continue
        # If a URL is provided, use the path/query as topical evidence and drop
        # the competitor brand/domain. The user wants what to search, not a
        # competitor-branded Trends alert.
        parsed = re.match(r"https?://([^/]+)(/.*)?$", text)
        if parsed:
            text = parsed.group(2) or ""
        text = re.sub(r"https?://", "", text)
        text = re.sub(r"www\.", "", text)
        text = re.sub(r"\.[a-z]{2,}(?:/|$)", " ", text, flags=re.I)
        text = text.replace("/", " ").replace("-", " ").replace("_", " ")
        chunks.append(text)
    return chunks


def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[’']", " ", text)
    text = re.sub(r"[^\wÀ-ÿ\s-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def words(text: str, stopwords: set[str]) -> list[str]:
    out = []
    for word in re.findall(r"[\wÀ-ÿ][\wÀ-ÿ-]{2,}", normalize_text(text)):
        word = word.strip("-_")
        if not word or word in stopwords or word.isdigit():
            continue
        out.append(word)
    return out


def title_to_candidates(title: str, stopwords: set[str]) -> list[tuple[str, str, str]]:
    """Convert a title/sentence into Trends-ready candidates.

    Returns (query, source, intent).
    """
    toks = words(title, stopwords)
    if not toks:
        return []
    candidates: list[tuple[str, str, str]] = []
    # Keep short noun-like anchors. Trends works better with 2-5 natural words
    # than long page titles.
    for n in (2, 3, 4):
        for i in range(0, max(0, len(toks) - n + 1)):
            phrase = " ".join(toks[i : i + n])
            if len(phrase) >= 6:
                candidates.append((phrase, "title_or_page_text", "short topic phrase from page/title evidence"))
    if len(toks) == 1:
        candidates.append((toks[0], "keyword_seed", "single high-frequency seed from audit/preflight"))
    # Add action/problem variants only when they remain short and natural.
    anchor = " ".join(toks[:3])
    if anchor:
        candidates.append((anchor, "title_or_page_text", "broad anchor from page/title evidence"))
    return candidates


def gsc_query_candidates(gsc: Any) -> list[tuple[str, str, str]]:
    candidates = []
    rows = gsc if isinstance(gsc, list) else gsc.get("queries", []) if isinstance(gsc, dict) else []
    for row in rows or []:
        if isinstance(row, str):
            q = row
            clicks = impressions = 0
        elif isinstance(row, dict):
            q = row.get("query") or row.get("keys", [""])[0]
            clicks = float(row.get("clicks") or 0)
            impressions = float(row.get("impressions") or 0)
        else:
            continue
        q = normalize_text(str(q))
        if 2 <= len(q.split()) <= 6:
            intent = "GSC query with existing visibility"
            if impressions and clicks == 0:
                intent = "GSC query with impressions but low/no clicks"
            candidates.append((q, "gsc", intent))
    return candidates


def topic_query_candidates(topic_sets: Any) -> list[dict[str, str]]:
    """Read explicit human-style queries grouped by reusable topic buckets."""
    if not topic_sets:
        return []
    topics = topic_sets.get("topics", topic_sets) if isinstance(topic_sets, dict) else topic_sets
    if isinstance(topics, dict):
        topics = [{"name": name, "queries": queries} for name, queries in topics.items()]
    output: list[dict[str, str]] = []
    for topic in topics or []:
        if not isinstance(topic, dict):
            continue
        name = str(topic.get("name") or topic.get("topic") or "custom_topic")
        purpose = str(topic.get("purpose") or "explicit topical bucket")
        for item in topic.get("queries", []) or []:
            if isinstance(item, str):
                query, intent = item, purpose
            elif isinstance(item, dict):
                query = item.get("query", "")
                intent = item.get("intent") or purpose
            else:
                continue
            if str(query).strip():
                output.append({"query": str(query), "intent": str(intent), "source": "topic_strategy", "topic": name})
    return output


def searched_queries_from_state(state: Any) -> set[str]:
    found: set[str] = set()
    if not isinstance(state, dict):
        return found
    for key, value in (state.get("queries") or {}).items():
        if isinstance(value, dict) and value.get("query"):
            found.add(normalize_text(str(value["query"])))
        else:
            found.add(normalize_text(str(key).split("::", 1)[0]))
    return found


def searched_queries_from_jsonl(path: str | Path | None) -> set[str]:
    found: set[str] = set()
    if not path or not Path(path).exists():
        return found
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("query"):
            found.add(normalize_text(str(row["query"])))
    return found


def select_topic_mix(ranked: dict[str, dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Round-robin top candidates so one narrow cluster cannot dominate."""
    groups: dict[str, list[dict[str, Any]]] = collections.OrderedDict()
    for item in sorted(ranked.values(), key=lambda row: (-row["score"], row["query"])):
        groups.setdefault(str(item.get("topic") or "site_topics"), []).append(item)
    selected: list[dict[str, Any]] = []
    while groups and len(selected) < max(1, limit):
        for topic in list(groups):
            if groups[topic] and len(selected) < max(1, limit):
                selected.append(groups[topic].pop(0))
            if not groups[topic]:
                del groups[topic]
    return selected


def score_query(query: str, source: str, keyword_counts: collections.Counter[str], manual_terms: set[str]) -> float:
    toks = query.split()
    score = 0.0
    # Manual seeds, audit-selected titles, and GSC rows should dominate raw
    # n-grams from pages. The page text is useful for discovery, not for
    # overriding explicit strategy evidence.
    score += 35.0 if source == "topic_strategy" else 0.0
    score += 30.0 if source == "manual" else 0.0
    score += 20.0 if source == "gsc" else 0.0
    score += 8.0 if source == "competitor" else 0.0
    score += 1.5 if source in {"title_or_page_text", "keyword_seed"} else 0.0
    score += sum(min(keyword_counts.get(tok, 0), 8) * 0.35 for tok in toks)
    score += sum(1.0 for tok in toks if tok in COMMERCIAL_INTENT_TERMS)
    score += 1.5 if any(tok in manual_terms for tok in toks) else 0.0
    # Prefer Trends-friendly length. Penalize one-word and long literal titles.
    if len(toks) == 1:
        score -= 0.8
    elif 2 <= len(toks) <= 4:
        score += 2.0
    elif len(toks) > 6:
        score -= 4.0
    # Avoid overly generic navigational leftovers.
    if len(set(toks)) != len(toks):
        score -= 1.0
    return score


def dedupe_key(query: str) -> str:
    return normalize_text(query)


def build_config(
    *,
    client_id: str,
    site_url: str,
    preflight: dict[str, Any] | None = None,
    gsc: Any | None = None,
    competitors: list[str] | None = None,
    manual_seed: list[str] | None = None,
    topic_sets: Any | None = None,
    exclude_queries: set[str] | None = None,
    geo: str = "FR",
    hl: str = "fr-FR",
    timeframe: str = "today 12-m",
    region_resolution: str = "REGION",
    min_hours_between_same_query: int = 24,
    limit: int = 12,
    log_markdown_path: str = "google-trends-searched-keywords.md",
    log_jsonl_path: str = "google-trends-search-log.jsonl",
    obsidian_note: str = "",
    obsidian_vault_path: str = "",
) -> dict[str, Any]:
    stopwords = set(FRENCH_STOPWORDS if hl.lower().startswith("fr") else DEFAULT_STOPWORDS)
    # Avoid selecting the client/domain name as a Trends seed. Branded demand can
    # be tracked separately, but this radar is for opportunity discovery.
    for raw in [client_id, site_url]:
        for tok in words(raw.replace("https://", " ").replace("http://", " ").replace(".", " "), DEFAULT_STOPWORDS):
            stopwords.add(tok)
    source_items: list[tuple[str, str, str, str]] = []
    text_chunks: list[str] = []
    if preflight:
        text_chunks.extend(text_chunks_from_preflight(preflight))
    if competitors:
        competitor_chunks = text_chunks_from_competitors(competitors)
        text_chunks.extend(competitor_chunks)
        for chunk in competitor_chunks:
            for q, _source, intent in title_to_candidates(chunk, stopwords):
                source_items.append((q, "competitor", f"competitor-derived {intent}", "competitor_topics"))
    for chunk in text_chunks:
        source_items.extend((q, source, intent, "site_topics") for q, source, intent in title_to_candidates(chunk, stopwords))
    if gsc:
        source_items.extend((q, source, intent, "existing_visibility") for q, source, intent in gsc_query_candidates(gsc))
    for seed in manual_seed or []:
        q = normalize_text(seed)
        if q:
            source_items.append((q, "manual", "manual seed from onboarding/audit strategy", "manual_strategy"))
    for item in topic_query_candidates(topic_sets):
        source_items.append((item["query"], item["source"], item["intent"], item["topic"]))

    keyword_counts: collections.Counter[str] = collections.Counter()
    for chunk in text_chunks:
        keyword_counts.update(words(chunk, stopwords))
    manual_terms = {tok for seed in manual_seed or [] for tok in normalize_text(seed).split()}

    ranked: dict[str, dict[str, Any]] = {}
    excluded = {normalize_text(q) for q in (exclude_queries or set())}
    for q, source, intent, topic in source_items:
        q = normalize_text(q)
        if not q or len(q) > 80:
            continue
        tok_count = len(q.split())
        if tok_count < 1 or tok_count > 6:
            continue
        key = dedupe_key(q)
        if key in excluded:
            continue
        scored = score_query(q, source, keyword_counts, manual_terms)
        item = ranked.get(key)
        if not item or scored > item["score"]:
            ranked[key] = {"query": q, "intent": intent, "source": source, "topic": topic, "score": round(scored, 2)}

    selected = select_topic_mix(ranked, limit)
    destinations: list[dict[str, Any]] = [
        {"type": "markdown", "path": log_markdown_path},
        {"type": "jsonl", "path": log_jsonl_path},
    ]
    if obsidian_note:
        obsidian_destination: dict[str, Any] = {"type": "obsidian_markdown", "path": obsidian_note}
        if obsidian_vault_path:
            obsidian_destination["vault_path"] = obsidian_vault_path
        destinations.append(obsidian_destination)
    return {
        "client_id": client_id,
        "site_url": site_url,
        "geo": geo,
        "hl": hl,
        "timeframe": timeframe,
        "region_resolution": region_resolution,
        "min_hours_between_same_query": min_hours_between_same_query,
        "keep_open_ms": 3500,
        "timeout_seconds": 180,
        "query_selection": {
            "method": "audit_preflight_gsc_competitor_manual_and_topic_buckets_to_short_human_queries",
            "warning": "Google Trends is ideation only. Validate with GSC, SERP, GA4, DataForSEO or Ads before prioritizing content.",
            "style_rule": "Use short human-typed queries, not article titles or over-specified SEO phrases. Prefer 2–4 words; allow a broader 1-word anchor or a justified 5–6-word query when the market requires it.",
            "topic_mix": list(dict.fromkeys(str(item.get("topic") or "site_topics") for item in selected)),
            "inputs_used": {
                "preflight": bool(preflight),
                "gsc": bool(gsc),
                "competitors": bool(competitors),
                "manual_seed_count": len(manual_seed or []),
                "topic_bucket_count": len({item.get("topic") for item in topic_query_candidates(topic_sets)}),
                "excluded_previously_searched_count": len(excluded),
            },
        },
        "alert_policy": {
            "mode": "opportunities_only",
            "alert_on_errors": True,
            "min_latest_value": 60,
            "min_trend_delta": 25,
            "min_related_queries": 3
        },
        "search_log": {
            "enabled": True,
            "description": "Adaptable searched-keyword log destinations. Relative markdown/jsonl paths are saved next to this config. Obsidian destinations use vault_path or OBSIDIAN_VAULT_PATH when available.",
            "destinations": destinations,
        },
        "queries": [{"query": item["query"], "topic": item["topic"], "intent": item["intent"], "source": item["source"], "selection_score": item["score"]} for item in selected],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Create a client Google Trends radar config from audit/preflight evidence.")
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--site-url", required=True)
    ap.add_argument("--preflight-json", help="Output from public_site_preflight.py or first audit JSON")
    ap.add_argument("--gsc-json", help="Optional GSC query rows, either {'queries': [...]} or a list")
    ap.add_argument("--competitor", action="append", default=[], help="Competitor URL/domain/title/keyword. Repeatable.")
    ap.add_argument("--seed", action="append", default=[], help="Manual seed from onboarding/audit. Repeatable.")
    ap.add_argument("--topic-json", help="Optional JSON topic buckets with human-style queries and purposes.")
    ap.add_argument("--state-json", help="Optional radar state JSON; already searched exact queries are excluded.")
    ap.add_argument("--search-log-jsonl", help="Optional prior search log JSONL; already searched exact queries are excluded.")
    ap.add_argument("--geo", default="FR")
    ap.add_argument("--hl", default="fr-FR")
    ap.add_argument("--timeframe", default="today 12-m")
    ap.add_argument("--region-resolution", default="REGION")
    ap.add_argument("--min-hours-between-same-query", type=int, default=24)
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--log-markdown-path", default="google-trends-searched-keywords.md", help="Relative/absolute markdown searched-keyword log path. Relative paths resolve next to the config.")
    ap.add_argument("--log-jsonl-path", default="google-trends-search-log.jsonl", help="Relative/absolute JSONL searched-keyword log path. Relative paths resolve next to the config.")
    ap.add_argument("--obsidian-note", default="", help="Optional Obsidian note path relative to the vault, for example 'Projects/Client/Google Trends Keywords.md'.")
    ap.add_argument("--obsidian-vault-path", default="", help="Optional Obsidian vault path. If omitted, runtime can use OBSIDIAN_VAULT_PATH.")
    ap.add_argument("--output", help="Write config JSON to this path. Defaults to stdout.")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    excluded = searched_queries_from_state(load_json(args.state_json, {}))
    excluded.update(searched_queries_from_jsonl(args.search_log_jsonl))
    config = build_config(
        client_id=args.client_id,
        site_url=args.site_url,
        preflight=load_json(args.preflight_json, None),
        gsc=load_json(args.gsc_json, None),
        competitors=args.competitor,
        manual_seed=args.seed,
        topic_sets=load_json(args.topic_json, None),
        exclude_queries=excluded,
        geo=args.geo,
        hl=args.hl,
        timeframe=args.timeframe,
        region_resolution=args.region_resolution,
        min_hours_between_same_query=args.min_hours_between_same_query,
        limit=args.limit,
        log_markdown_path=args.log_markdown_path,
        log_jsonl_path=args.log_jsonl_path,
        obsidian_note=args.obsidian_note,
        obsidian_vault_path=args.obsidian_vault_path,
    )
    text = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
