#!/usr/bin/env python3
"""Normalize bibliographic records and compute explicitly sample-only statistics.

Strong identifier links (DOI, arXiv, stable provider ID, verified_same_as) merge
records transitively. Identical titles alone NEVER merge. A verified_same_as
entry must be an explicit doi:..., arxiv:..., or source:<provider>:<id> link
already checked by the researcher. This utility does not verify that claim.

The default statistics date is first_public_date. OpenAlex publication_date is
not silently substituted; use --date-field publication_date to inspect that
different date basis, and retain the proxy warning in the report.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any
import urllib.parse


def normalize_doi(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value.strip(), flags=re.I)
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    return value.lower() if re.fullmatch(r"10\.\d{4,9}/\S+", value) else None


def normalize_arxiv(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(value)
        if parsed.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            return None
        value = re.sub(r"^/(?:abs|pdf)/", "", parsed.path)
        value = re.sub(r"\.pdf$", "", value, flags=re.I)
    value = re.sub(r"^arxiv:\s*", "", value, flags=re.I)
    value = re.sub(r"v\d+$", "", value)
    return value.lower() if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[A-Za-z][A-Za-z.\-]*/\d{7})", value) else None


def valid_date(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value[:10]):
        return None
    try:
        return dt.date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def unique(values: list[Any]) -> list[Any]:
    found: set[str] = set()
    result: list[Any] = []
    for value in values:
        marker = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if marker not in found:
            found.add(marker)
            result.append(value)
    return result


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def typed_link(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith("doi:"):
        identifier = normalize_doi(value)
        return "doi:" + identifier if identifier else None
    if value.startswith("arxiv:"):
        identifier = normalize_arxiv(value)
        return "arxiv:" + identifier if identifier else None
    if re.fullmatch(r"source:[^:\s]+:.+", value):
        return value
    return None


def identity_keys(record: dict[str, Any], fallback: str) -> tuple[list[str], list[str]]:
    keys: list[str] = []
    warnings: list[str] = []
    identifiers = record.get("identifiers") if isinstance(record.get("identifiers"), dict) else {}
    for value in [record.get("doi")] + as_list(identifiers.get("doi")):
        if value is not None:
            identifier = normalize_doi(value)
            if identifier:
                keys.append("doi:" + identifier)
            else:
                warnings.append("Ignored malformed DOI identifier; inspect the original observation.")
    for value in [record.get("arxiv_id")] + as_list(identifiers.get("arxiv")):
        if value is not None:
            identifier = normalize_arxiv(value)
            if identifier:
                keys.append("arxiv:" + identifier)
            else:
                warnings.append("Ignored malformed arXiv identifier; inspect the original observation.")
    source = str(record.get("source") or "unknown")
    record_id = record.get("id")
    if isinstance(record_id, str) and record_id.strip():
        link = typed_link(record_id)
        if link:
            keys.append(link)
        else:
            keys.append("source:" + source + ":" + record_id)
    for value in as_list(record.get("source_ids")):
        if isinstance(value, dict) and value.get("source") and value.get("id"):
            keys.append("source:" + str(value["source"]) + ":" + str(value["id"]))
    for value in as_list(record.get("verified_same_as")):
        link = typed_link(value)
        if link:
            keys.append(link)
        else:
            warnings.append("Ignored an invalid verified_same_as link; only explicit DOI/arXiv/provider links are accepted.")
    return sorted(set(keys)) or [fallback], warnings


def normalize(inputs: list[tuple[str, Any]]) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    contexts: list[dict[str, Any]] = []
    for file_index, (filename, data) in enumerate(inputs):
        if isinstance(data, list):
            records, context = data, {"input": filename, "status": "unknown"}
        elif isinstance(data, dict) and isinstance(data.get("records"), list):
            records = data["records"]
            context = {"input": filename, **{k: data.get(k) for k in (
                "provider", "query", "date_range", "endpoint", "params", "searched_at",
                "status", "reported_total", "returned", "limit", "coverage_notes", "errors",
            ) if k in data}}
            context.setdefault("status", "unknown")
        else:
            raise ValueError(filename + ": expected an array of records or an object with records[].")
        contexts.append(context)
        for record_index, record in enumerate(records):
            if not isinstance(record, dict):
                raise ValueError(filename + ": every record must be an object.")
            if not isinstance(record.get("title"), str) or not record["title"].strip():
                raise ValueError(filename + ": every record must have a nonempty title.")
            # An unidentifiable observation remains separate even from an identical
            # title/record in another input. No speculative title-based merge.
            digest = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()[:16]
            fallback = "observation:" + str(file_index) + ":" + str(record_index) + ":" + digest
            keys, warnings = identity_keys(record, fallback)
            observations.append({"record": record, "keys": keys, "warnings": warnings,
                                 "provenance": {**context, "record_index": record_index,
                                                "original_id": record.get("id")}})
    parent = list(range(len(observations)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    index: dict[str, int] = {}
    for i, observation in enumerate(observations):
        for key in observation["keys"]:
            if key in index:
                union(i, index[key])
            else:
                index[key] = i
    groups: dict[int, list[dict[str, Any]]] = {}
    for i, observation in enumerate(observations):
        groups.setdefault(find(i), []).append(observation)
    merged: list[dict[str, Any]] = []
    for group in groups.values():
        records = [o["record"] for o in group]
        aliases = sorted({key for o in group for key in o["keys"]})
        dois = [key[4:] for key in aliases if key.startswith("doi:")]
        arxiv_ids = [key[6:] for key in aliases if key.startswith("arxiv:")]
        titles = unique([r["title"] for r in records])
        first_dates = sorted({d for r in records if (d := valid_date(r.get("first_public_date")))})
        publication_dates = sorted({d for r in records if (d := valid_date(r.get("publication_date")))})
        metadata_warnings = [w for o in group for w in o["warnings"]]
        metadata_warnings.extend(w for r in records for w in as_list(r.get("metadata_warnings")))
        identity_warnings = [w for r in records for w in as_list(r.get("identity_warnings"))]
        journal_dois = [d for d in dois if not d.startswith("10.48550/arxiv.")]
        if len(journal_dois) > 1:
            identity_warnings.append("Multiple non-arXiv DOIs are linked; verify that this is one research report rather than distinct follow-up studies.")
        if len(arxiv_ids) > 1:
            identity_warnings.append("Multiple original arXiv IDs are linked; researcher verification is required before counting as one study.")
        for r in records:
            for field in ("first_public_date", "publication_date"):
                if r.get(field) is not None and valid_date(r[field]) is None:
                    metadata_warnings.append("Unusable " + field + " (missing day or invalid date); not imputed.")
        source_ids: list[dict[str, Any]] = []
        for r in records:
            source_ids.extend(x for x in as_list(r.get("source_ids")) if isinstance(x, dict))
            if r.get("id"):
                source_ids.append({"source": r.get("source") or "unknown", "id": r["id"]})
        # Retain original observations (including alternate titles/dates) so an
        # analyst can inspect both bibliographic disagreement and version links.
        raw_observations: list[Any] = []
        provenance: list[Any] = []
        for o in group:
            record = o["record"]
            raw_observations.extend(as_list(record.get("observations")) or [record])
            provenance.extend(as_list(record.get("provenance")))
            provenance.append(o["provenance"])
        merged.append({
            "id": ("doi:" + dois[0]) if dois else (("arxiv:" + arxiv_ids[0]) if arxiv_ids else aliases[0]),
            "title": titles[0],
            "titles": titles,
            "doi": dois[0] if len(dois) == 1 else None,
            "arxiv_id": arxiv_ids[0] if len(arxiv_ids) == 1 else None,
            "identifiers": {"doi": dois, "arxiv": arxiv_ids},
            "aliases": aliases,
            "source_ids": unique(source_ids),
            "source": "normalized",
            "sources": sorted({str(r.get("source") or "unknown") for r in records}),
            "first_public_date": first_dates[0] if first_dates else None,
            "first_public_dates": first_dates,
            "publication_date": publication_dates[0] if publication_dates else None,
            "publication_dates": publication_dates,
            "authors": unique([a for r in records for a in as_list(r.get("authors"))]),
            "topics": unique([t for r in records for t in as_list(r.get("topics"))]),
            "urls": unique([u for r in records for u in ([r.get("url")] + as_list(r.get("urls")))
                            if isinstance(u, str) and u.startswith(("https://", "http://"))]),
            "identity_warnings": unique(identity_warnings),
            "metadata_warnings": unique(metadata_warnings),
            "provenance": unique(provenance),
            "observations": unique(raw_observations),
        })
    merged.sort(key=lambda r: r["id"])
    title_index: dict[str, list[str]] = {}
    for record in merged:
        for title in record["titles"]:
            title_index.setdefault(" ".join(title.casefold().split()), []).append(record["id"])
    return {
        "schema_version": 1,
        "normalization_version": 1,
        "scope": "input_sample_only",
        "raw_record_count": len(observations),
        "unique_record_count": len(merged),
        "input_searches": contexts,
        "normalization_rules": [
            "DOI and original arXiv ID are normalized; arXiv versions are not distinct studies.",
            "Strong shared identifiers and researcher-verified links merge transitively.",
            "Identical titles do not merge records; title groups are for manual inspection.",
            "Missing dates stay null; no publication-date substitution for first-public dates.",
            "Deduplication does not prove that every resulting record represents a distinct experiment.",
        ],
        "same_title_review_groups": [{"normalized_title": title, "record_ids": sorted(set(ids))}
                                     for title, ids in title_index.items() if len(set(ids)) > 1],
        "records": merged,
    }


def topic_labels(record: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for topic in as_list(record.get("topics")):
        if isinstance(topic, str):
            result.add(topic.casefold())
        elif isinstance(topic, dict):
            for field in ("id", "display_name", "name"):
                if isinstance(topic.get(field), str):
                    result.add(topic[field].casefold())
    return result


def statistics(data: dict[str, Any], as_of: str, window_days: int = 90,
               baseline_days: int = 365, topic: str | None = None,
               date_field: str = "first_public_date") -> dict[str, Any]:
    if valid_date(as_of) != as_of:
        raise ValueError("as-of must use a valid YYYY-MM-DD date.")
    if window_days < 1 or baseline_days < 1:
        raise ValueError("Window and baseline durations must be positive.")
    if date_field not in {"first_public_date", "publication_date"}:
        raise ValueError("Unsupported date field.")
    records = data.get("records")
    if not isinstance(records, list):
        raise ValueError("Statistics input must contain records[].")
    end = dt.date.fromisoformat(as_of)
    current_start = end - dt.timedelta(days=window_days - 1)
    baseline_end = current_start - dt.timedelta(days=1)
    baseline_start = current_start - dt.timedelta(days=baseline_days)
    counts = {"current": 0, "baseline": 0, "unknown_date": 0, "future": 0, "older": 0,
              "identity_review_required": 0, "topic_classification_unknown": 0,
              "other_topics": 0}
    selected: list[str] = []
    current_ids: list[str] = []
    baseline_ids: list[str] = []
    for record in records:
        if topic:
            labels = topic_labels(record)
            if not labels:
                counts["topic_classification_unknown"] += 1
                continue
            if topic.casefold() not in labels:
                counts["other_topics"] += 1
                continue
        selected.append(str(record.get("id") or "unknown"))
        if as_list(record.get("identity_warnings")):
            counts["identity_review_required"] += 1
            continue
        date_value = valid_date(record.get(date_field))
        if date_value is None:
            counts["unknown_date"] += 1
            continue
        date = dt.date.fromisoformat(date_value)
        if date > end:
            counts["future"] += 1
        elif date >= current_start:
            counts["current"] += 1
            current_ids.append(str(record.get("id")))
        elif date >= baseline_start:
            counts["baseline"] += 1
            baseline_ids.append(str(record.get("id")))
        else:
            counts["older"] += 1
    n_current, n_baseline = counts["current"], counts["baseline"]
    growth = (math.log2(((n_current + 0.5) / window_days) / ((n_baseline + 0.5) / baseline_days))
              if n_current + n_baseline else None)
    warnings = [
        "INPUT SAMPLE ONLY: counts do not establish a field's total papers, global ranking, or absence of research.",
        "The rate comparison is uncalibrated and uses 0.5 smoothing; inspect raw counts, query consistency and coverage.",
        "Independent studies/teams, topic assignment and publication-version relationships still require scientific review.",
    ]
    input_searches = as_list(data.get("input_searches"))
    coverage = [{"input": s.get("input"), "provider": s.get("provider"),
                 "status": s.get("status", "unknown")}
                for s in input_searches if isinstance(s, dict)]
    incomplete_coverage = not coverage or any(s["status"] != "complete_for_query" for s in coverage)
    if incomplete_coverage:
        warnings.append("Input retrieval includes partial, failed or unknown coverage. Zero rows mean no records in this input, not a successful zero-result literature search; unobserved paper counts remain unknown.")
    if date_field == "publication_date":
        warnings.append("Publication-date proxy selected explicitly; this may be later than the first public preprint date.")
    if counts["unknown_date"] or counts["identity_review_required"] or counts["topic_classification_unknown"]:
        warnings.append("Some records were excluded because dates, identity links or topic classification are unknown; these exclusions are not zero research activity.")
    return {
        "schema_version": 1,
        "scope": "input_sample_only",
        "can_claim_field_hotness": False,
        "as_of": as_of,
        "date_field": date_field,
        "topic_filter": topic,
        "topic_filter_method": "exact case-insensitive match to supplied topic labels; no inferred classification",
        "windows": {
            "current": {"from": current_start.isoformat(), "to": end.isoformat(), "days": window_days},
            "baseline": {"from": baseline_start.isoformat(), "to": baseline_end.isoformat(), "days": baseline_days},
        },
        "input_record_count": len(records),
        "input_coverage": coverage,
        "input_coverage_incomplete": incomplete_coverage,
        "unretrieved_record_count": None,
        "selected_record_count": len(selected),
        "counts": counts,
        "current_record_ids": current_ids,
        "baseline_record_ids": baseline_ids,
        "sample_rate_growth_log2_smoothed": growth,
        "smoothing_pseudocount": 0.5,
        "low_sample_flag": n_current < 5,
        "warnings": warnings,
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    normalizer = commands.add_parser("normalize", help="Merge only records linked by strong/verified identifiers")
    normalizer.add_argument("inputs", nargs="+", type=Path)
    normalizer.add_argument("--output", required=True, type=Path)
    stats = commands.add_parser("stats", help="Compute input-sample counts, never global field heat")
    stats.add_argument("input", type=Path)
    stats.add_argument("--as-of", required=True, metavar="YYYY-MM-DD")
    stats.add_argument("--window-days", type=int, default=90)
    stats.add_argument("--baseline-days", type=int, default=365)
    stats.add_argument("--topic", help="Exact match to a supplied topic label (case-insensitive)")
    stats.add_argument("--date-field", choices=["first_public_date", "publication_date"], default="first_public_date")
    stats.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "normalize":
            inputs = [(path.name, json.loads(path.read_text(encoding="utf-8"))) for path in args.inputs]
            result = normalize(inputs)
        else:
            data = json.loads(args.input.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("normalization_version") != 1:
                raise ValueError("Run normalize first; stats expects its normalized JSON output.")
            result = statistics(data, args.as_of, args.window_days, args.baseline_days,
                                args.topic, args.date_field)
        write_json(args.output, result)
    except (ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"output": str(args.output), "operation": args.command,
                      "scope": "input_sample_only",
                      "records": result.get("unique_record_count", result.get("input_record_count"))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
