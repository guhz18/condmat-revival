#!/usr/bin/env python3
"""Bounded scholarly metadata collection; never interpret retrieval failure as zero.

Only arXiv and OpenAlex are implemented here. This is a read-only metadata
client, not a Google Scholar scraper. Use the skill's browser workflow for
Scholar and its other source-specific workflows for additional databases.

Python 3.10+, standard library. Endpoints checked against official docs on
2026-11-07. API policy may change: inspect current documentation on failures.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


UTC = dt.timezone.utc
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
ENDPOINTS = {
    "arxiv": "https://export.arxiv.org/api/query",
    "openalex": "https://api.openalex.org/works",
}
MAX_BODY_BYTES = 20 * 1024 * 1024
ARXIV_FIELDS = {
    "ti", "au", "abs", "co", "jr", "cat", "rn", "id", "all",
    "submittedDate", "lastUpdatedDate",
}


class RetrievalError(Exception):
    """A safe, bounded error description that does not contain credentials."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def utc_now() -> str:
    return dt.datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def parse_date(value: str) -> dt.date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Dates must use YYYY-MM-DD.")
    return dt.date.fromisoformat(value)


def day_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_date(value[:10]).isoformat()
    except (ValueError, TypeError):
        return None


def normalize_doi(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    candidate = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", candidate, flags=re.I)
    candidate = re.sub(r"^doi:\s*", "", candidate, flags=re.I)
    return candidate.lower() if re.fullmatch(r"10\.\d{4,9}/\S+", candidate) else None


def normalize_arxiv(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate.startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(candidate)
        if parsed.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            return None
        candidate = re.sub(r"^/(?:abs|pdf)/", "", parsed.path)
        candidate = re.sub(r"\.pdf$", "", candidate, flags=re.I)
    candidate = re.sub(r"^arxiv:\s*", "", candidate, flags=re.I)
    candidate = re.sub(r"v\d+$", "", candidate)
    if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[A-Za-z][A-Za-z.\-]*/\d{7})", candidate):
        return candidate.lower()
    return None


def validate_arxiv_query(query: str) -> None:
    # Ignore quoted text when detecting field prefixes (e.g. a literal URL).
    unquoted = re.sub(r'"(?:\\.|[^"\\])*"', '""', query)
    fields = re.findall(r"(?<![\w/])([A-Za-z][A-Za-z0-9_]*)\s*:", unquoted)
    unknown = sorted(set(fields) - ARXIV_FIELDS)
    if unknown:
        raise ValueError("Unsupported arXiv field prefix: " + ", ".join(unknown))
    if query.count('"') % 2:
        raise ValueError("Unbalanced quotation marks in arXiv query.")


def parse_arxiv(payload: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise RetrievalError("unsafe_xml", "Unexpected DTD/entity in Atom response.")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RetrievalError("invalid_xml", "The arXiv response is not valid Atom XML.") from exc
    if root.tag != ATOM + "feed":
        raise RetrievalError("wrong_shape", "Expected an Atom feed; no result count is established.")
    total_text = root.findtext(OPENSEARCH + "totalResults")
    try:
        total = int(total_text) if total_text is not None else -1
        start = int(root.findtext(OPENSEARCH + "startIndex", "0"))
    except (ValueError, TypeError) as exc:
        raise RetrievalError("wrong_shape", "arXiv pagination metadata is invalid.") from exc
    if total < 0 or start < 0:
        raise RetrievalError("missing_total", "arXiv did not provide a valid totalResults count.")
    records: list[dict[str, Any]] = []
    for entry in root.findall(ATOM + "entry"):
        title = clean_text(entry.findtext(ATOM + "title"))
        raw_id = clean_text(entry.findtext(ATOM + "id"))
        if title.casefold() == "error" or "/api/errors" in raw_id:
            raise RetrievalError("arxiv_error_feed", "arXiv returned an error entry despite HTTP 200.")
        arxiv_id = normalize_arxiv(raw_id)
        if not arxiv_id or not title:
            raise RetrievalError("invalid_record", "An arXiv entry lacks a valid identifier or title.")
        raw_doi = clean_text(entry.findtext(ARXIV + "doi"))
        doi = normalize_doi(raw_doi)
        published = entry.findtext(ATOM + "published")
        updated = entry.findtext(ATOM + "updated")
        first_date = day_or_none(published)
        records.append({
            "id": "arxiv:" + arxiv_id,
            "title": title,
            "doi": doi,
            "doi_raw": raw_doi or None,
            "arxiv_id": arxiv_id,
            "versioned_id": raw_id,
            "first_public_date": first_date,
            "publication_date": None,
            "updated_date": day_or_none(updated),
            "date_basis": "arXiv published (first submission), not updated",
            "authors": [clean_text(a.findtext(ATOM + "name"))
                        for a in entry.findall(ATOM + "author")
                        if clean_text(a.findtext(ATOM + "name"))],
            "topics": sorted({c.attrib["term"] for c in entry.findall(ATOM + "category")
                              if c.attrib.get("term")}),
            "source": "arxiv",
            "url": "https://arxiv.org/abs/" + arxiv_id,
            "abstract": clean_text(entry.findtext(ATOM + "summary")),
            "metadata_warnings": (["DOI field is present but is not one parseable DOI."]
                                  if raw_doi and doi is None else [])
                                 + (["First submission date is absent or malformed."]
                                    if first_date is None else []),
        })
    return records, {"total": total, "start": start, "next_cursor": None}


def parse_openalex(payload: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RetrievalError("invalid_json", "The OpenAlex response is not valid JSON.") from exc
    if not isinstance(data, dict) or "error" in data or "errors" in data:
        raise RetrievalError("openalex_error_body", "OpenAlex returned an error or unexpected object.")
    meta, results = data.get("meta"), data.get("results")
    if not isinstance(meta, dict) or not isinstance(results, list):
        raise RetrievalError("wrong_shape", "OpenAlex results/meta fields are missing.")
    total = meta.get("count")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise RetrievalError("missing_total", "OpenAlex meta.count is not a nonnegative integer.")
    records: list[dict[str, Any]] = []
    for work in results:
        if not isinstance(work, dict):
            raise RetrievalError("invalid_record", "An OpenAlex result is not an object.")
        work_id = work.get("id")
        if not isinstance(work_id, str) or not re.fullmatch(r"https://openalex\.org/W\d+", work_id):
            raise RetrievalError("invalid_record", "An OpenAlex result lacks a valid work ID.")
        title = clean_text(work.get("title") or work.get("display_name"))
        if not title:
            raise RetrievalError("invalid_record", "An OpenAlex result lacks a title.")
        for list_field in ("locations", "authorships", "topics"):
            if work.get(list_field) is not None and not isinstance(work[list_field], list):
                raise RetrievalError("invalid_record", "An OpenAlex list field has an unexpected shape.")
        arxiv_ids: set[str] = set()
        locations = list(work.get("locations") or [])
        if isinstance(work.get("primary_location"), dict):
            locations.append(work["primary_location"])
        for loc in locations:
            if isinstance(loc, dict):
                for field in ("landing_page_url", "pdf_url"):
                    candidate = normalize_arxiv(loc.get(field))
                    if candidate:
                        arxiv_ids.add(candidate)
        warnings: list[str] = []
        if len(arxiv_ids) > 1:
            warnings.append("Multiple arXiv IDs in locations; verify the version relationship before merging.")
        authors: list[str] = []
        author_ids: list[str] = []
        for authorship in work.get("authorships") or []:
            author = authorship.get("author") if isinstance(authorship, dict) else None
            if isinstance(author, dict):
                if author.get("display_name"):
                    authors.append(clean_text(author["display_name"]))
                if author.get("id"):
                    author_ids.append(str(author["id"]))
        topics: set[str] = set()
        topic_ids: set[str] = set()
        for topic in work.get("topics") or []:
            if isinstance(topic, dict):
                if topic.get("display_name"):
                    topics.add(clean_text(topic["display_name"]))
                if topic.get("id"):
                    topic_ids.add(str(topic["id"]))
        records.append({
            "id": work_id,
            "title": title,
            "doi": normalize_doi(work.get("doi")),
            "arxiv_id": next(iter(arxiv_ids)) if len(arxiv_ids) == 1 else None,
            "arxiv_ids_to_review": sorted(arxiv_ids) if len(arxiv_ids) > 1 else [],
            # A journal publication date does not establish the first public date
            # of its preprint. Leave it null until a linked source verifies it.
            "first_public_date": None,
            "publication_date": day_or_none(work.get("publication_date")),
            "date_basis": "OpenAlex publication_date; first public date not verified",
            "authors": authors,
            "author_ids": author_ids,
            "topics": sorted(topics),
            "topic_ids": sorted(topic_ids),
            "source": "openalex",
            "url": work_id,
            "document_type": work.get("type"),
            "is_retracted": work.get("is_retracted"),
            "metadata_warnings": warnings,
        })
    cursor = meta.get("next_cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise RetrievalError("wrong_shape", "OpenAlex next_cursor is not a string or null.")
    return records, {"total": total, "start": None, "next_cursor": cursor}


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward the optional API credential to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            raise RetrievalError("redirect_blocked", "Cross-origin API redirect requires review.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def retry_delay(header: str | None, attempt: int) -> float:
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(header)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                return max(0.0, (when - dt.datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return float(2 ** (attempt + 1))


class HTTPClient:
    def __init__(self, source: str):
        self.source = source
        self.last_request = 0.0
        self.opener = urllib.request.build_opener(SameOriginRedirect())

    def fetch(self, params: dict[str, Any], api_key: str | None = None) -> bytes:
        url = ENDPOINTS[self.source] + "?" + urllib.parse.urlencode(params)
        headers = {"User-Agent": "condmat-revival/1.0 (scholarly metadata client)",
                   "Accept": "application/atom+xml" if self.source == "arxiv" else "application/json"}
        if api_key and self.source == "openalex":
            headers["Authorization"] = "Bearer " + api_key
        for attempt in range(3):
            min_interval = 3.0 if self.source == "arxiv" else 1.0
            delay = min_interval - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            self.last_request = time.monotonic()
            try:
                req = urllib.request.Request(url, headers=headers)
                with self.opener.open(req, timeout=30) as response:
                    payload = response.read(MAX_BODY_BYTES + 1)
                if len(payload) > MAX_BODY_BYTES:
                    raise RetrievalError("response_too_large", "API response exceeded the 20 MiB limit.")
                return payload
            except urllib.error.HTTPError as exc:
                # Never store an arbitrary response body, headers, or credential.
                if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                    wait = retry_delay(exc.headers.get("Retry-After"), attempt)
                    if wait > 30:
                        raise RetrievalError("retry_later", "API asks to retry after more than 30 seconds; stop and resume later.") from exc
                    time.sleep(wait)
                    continue
                raise RetrievalError("http_" + str(exc.code), "API HTTP " + str(exc.code) + "; retrieval is not complete.") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise RetrievalError("network_error", "API network or timeout failure; no empty-result inference is valid.") from exc
        raise RetrievalError("retry_exhausted", "API retry budget exhausted.")


def collect(source: str, query: str, from_date: str, to_date: str, limit: int,
            client: Any = None, api_key: str | None = None) -> dict[str, Any]:
    start_day, end_day = parse_date(from_date), parse_date(to_date)
    if start_day > end_day:
        raise ValueError("from-date must not be after to-date.")
    if not query.strip() or len(query) > 6000:
        raise ValueError("Provide a nonempty query of at most 6000 characters.")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000.")
    if source not in ENDPOINTS:
        raise ValueError("source must be arxiv or openalex.")
    if source == "arxiv":
        validate_arxiv_query(query)
    if client is None:
        client = HTTPClient(source)
    # Keep the key out of every object written to disk.
    if source == "openalex" and api_key is None:
        api_key = os.environ.get("OPENALEX_API_KEY") or None
    result: dict[str, Any] = {
        "schema_version": 1,
        "provider": source,
        "query": query,
        "date_range": {"from": from_date, "to": to_date, "inclusive": True},
        "endpoint": ENDPOINTS[source],
        "params": {},
        "searched_at": utc_now(),
        "status": "failed",
        "reported_total": None,
        "returned": 0,
        "limit": limit,
        "pages": [],
        "errors": [],
        "coverage_notes": ["Complete means complete for this recorded query only, not complete coverage of a research field."],
        "records": [],
    }
    if source == "arxiv":
        search_query = "(" + query + ") AND submittedDate:[" + start_day.strftime("%Y%m%d") + "0000 TO " + end_day.strftime("%Y%m%d") + "2359]"
        base_params = {"search_query": search_query, "sortBy": "submittedDate", "sortOrder": "descending"}
        result["coverage_notes"].append("Run arXiv collectors serially across agents/machines; this client uses one connection and >=3-second spacing within a run.")
    else:
        base_params = {"search": query, "filter": "from_publication_date:" + from_date + ",to_publication_date:" + to_date, "sort": "publication_date:desc"}
        result["coverage_notes"].append("OpenAlex window uses publication_date; first-public dates require linked preprint/primary-source verification.")
    result["params"] = base_params
    seen: set[str] = set()
    cursor = "*"
    count_changed = False
    try:
        while len(result["records"]) < limit:
            page_size = min(100, limit - len(result["records"]))
            params = dict(base_params)
            if source == "arxiv":
                params.update({"start": len(result["records"]), "max_results": page_size})
            else:
                params.update({"per_page": page_size, "cursor": cursor})
            payload = client.fetch(params, api_key=api_key)
            records, meta = parse_arxiv(payload) if source == "arxiv" else parse_openalex(payload)
            if len(records) > page_size:
                raise RetrievalError("pagination_mismatch", "API returned more entries than requested.")
            if source == "arxiv" and meta["start"] != len(result["records"]):
                raise RetrievalError("pagination_mismatch", "arXiv startIndex differs from the requested offset.")
            if result["reported_total"] is None:
                result["reported_total"] = meta["total"]
            elif result["reported_total"] != meta["total"]:
                count_changed = True
            result["pages"].append({"page": len(result["pages"]) + 1, "params": params,
                                     "reported_total": meta["total"], "returned": len(records)})
            if len(result["records"]) + len(records) > meta["total"]:
                raise RetrievalError("count_mismatch", "Retrieved entries exceed the API total; inspect pagination.")
            for record in records:
                if record["id"] in seen:
                    raise RetrievalError("duplicate_page_record", "An API page repeated a record; count reconciliation is incomplete.")
                seen.add(record["id"])
                result["records"].append(record)
            if len(result["records"]) == meta["total"]:
                if count_changed:
                    result["status"] = "partial"
                    result["coverage_notes"].append("Reported result count changed during pagination; no stable-snapshot completeness claim.")
                else:
                    result["status"] = "complete_for_query"
                break
            if not records:
                raise RetrievalError("premature_end", "API returned an empty page before its reported total was reached.")
            if len(result["records"]) >= limit:
                result["status"] = "partial"
                result["coverage_notes"].append("Configured record limit reached before the query's reported total.")
                break
            if source == "openalex":
                if not api_key:
                    result["status"] = "partial"
                    result["coverage_notes"].append("No OPENALEX_API_KEY: stopped after the first cursor page; additional pages are not covered.")
                    break
                next_cursor = meta["next_cursor"]
                if not next_cursor or next_cursor == cursor:
                    raise RetrievalError("invalid_cursor", "OpenAlex cursor ended or repeated before count reconciliation.")
                cursor = next_cursor
    except RetrievalError as exc:
        result["status"] = "partial" if result["records"] else "failed"
        result["errors"].append({"code": exc.code, "message": exc.message})
    except KeyboardInterrupt:
        result["status"] = "partial" if result["records"] else "failed"
        result["errors"].append({"code": "interrupted", "message": "Collection interrupted; saved retrieved records without claiming completeness."})
    result["returned"] = len(result["records"])
    result["finished_at"] = utc_now()
    return result


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
    parser.add_argument("--source", choices=sorted(ENDPOINTS), required=True)
    parser.add_argument("--query", required=True, help="arXiv fielded query or plain OpenAlex search text; never supply an API key here")
    parser.add_argument("--from-date", required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--to-date", required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=100, help="Maximum records, 1–1000 (default: 100)")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = collect(args.source, args.query, args.from_date, args.to_date, args.limit)
        write_json(args.output, result)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"output": str(args.output), "provider": result["provider"],
                      "status": result["status"], "returned": result["returned"],
                      "reported_total": result["reported_total"]}, ensure_ascii=False))
    return 0 if result["status"] == "complete_for_query" else 2


if __name__ == "__main__":
    raise SystemExit(main())
