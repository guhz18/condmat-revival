#!/usr/bin/env python3
"""Versioned local research records; persistence transport belongs to the host.

Only the Python standard library is required. Never store API credentials,
browser state, cookies, or temporary signed download URLs in this database.
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
import sqlite3
import sys
import tempfile
from urllib.parse import parse_qsl, unquote, urlsplit

SCHEMA_VERSION = 1
STATUSES = {"complete_for_query", "partial", "failed", "manual"}
KINDS = ("documents", "materials", "topics", "searches", "claims", "assessments", "runs")
SECRET_NAMES = {
    "apikey", "authorization", "accesstoken", "refreshtoken", "password",
    "cookie", "cookies", "secret", "clientsecret", "sessiontoken", "sessionkey",
    "browserstate", "storageState".lower(), "xapikey", "bearertoken",
}
SECRET_PARAMS = SECRET_NAMES | {
    "key", "token", "auth", "email", "mailto", "sig", "signature",
    "xamzcredential", "xamzsecuritytoken", "xamzsignature",
}
URL_RE = re.compile(r"https?://[^\s<>\"']+")

DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, mode TEXT NOT NULL,
  topic_mode TEXT NOT NULL, scope_hash TEXT NOT NULL, payload_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL, imported_at TEXT NOT NULL
);
CREATE TABLE documents (
  document_id TEXT PRIMARY KEY, title TEXT NOT NULL, payload_json TEXT NOT NULL,
  first_run_id TEXT NOT NULL REFERENCES runs(run_id),
  last_run_id TEXT NOT NULL REFERENCES runs(run_id)
);
CREATE TABLE document_aliases (
  alias_type TEXT NOT NULL, alias_value TEXT NOT NULL,
  document_id TEXT NOT NULL REFERENCES documents(document_id),
  PRIMARY KEY(alias_type, alias_value)
);
CREATE TABLE materials (
  material_id TEXT PRIMARY KEY, name TEXT NOT NULL, identity_hash TEXT NOT NULL,
  identity_json TEXT NOT NULL, payload_json TEXT NOT NULL,
  first_run_id TEXT NOT NULL REFERENCES runs(run_id),
  last_run_id TEXT NOT NULL REFERENCES runs(run_id)
);
CREATE TABLE topics (
  topic_id TEXT PRIMARY KEY, label TEXT NOT NULL, payload_json TEXT NOT NULL,
  first_run_id TEXT NOT NULL REFERENCES runs(run_id),
  last_run_id TEXT NOT NULL REFERENCES runs(run_id)
);
CREATE TABLE entity_versions (
  entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES runs(run_id), payload_json TEXT NOT NULL,
  PRIMARY KEY(entity_type, entity_id, run_id)
);
CREATE TABLE searches (
  search_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
  source TEXT NOT NULL, query TEXT NOT NULL, scope_hash TEXT NOT NULL,
  retrieved_at TEXT NOT NULL, status TEXT NOT NULL, result_count INTEGER,
  payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX searches_scope_time ON searches(scope_hash, retrieved_at DESC);
CREATE TABLE claims (
  claim_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
  subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, claim_type TEXT NOT NULL,
  text TEXT NOT NULL, supersedes_claim_id TEXT REFERENCES claims(claim_id),
  payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE claim_evidence (
  claim_id TEXT NOT NULL REFERENCES claims(claim_id), evidence_index INTEGER NOT NULL,
  document_id TEXT REFERENCES documents(document_id), url TEXT, locator TEXT NOT NULL,
  relation TEXT NOT NULL, payload_json TEXT NOT NULL,
  PRIMARY KEY(claim_id, evidence_index)
);
CREATE TABLE assessments (
  assessment_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
  topic_id TEXT NOT NULL REFERENCES topics(topic_id),
  material_id TEXT NOT NULL REFERENCES materials(material_id),
  tier TEXT NOT NULL, assessed_at TEXT NOT NULL,
  supersedes_assessment_id TEXT REFERENCES assessments(assessment_id),
  payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX assessments_pair_time ON assessments(topic_id, material_id, assessed_at DESC);
CREATE TABLE assessment_claims (
  assessment_id TEXT NOT NULL REFERENCES assessments(assessment_id),
  claim_id TEXT NOT NULL REFERENCES claims(claim_id),
  PRIMARY KEY(assessment_id, claim_id)
);
CREATE TABLE run_items (
  run_id TEXT NOT NULL REFERENCES runs(run_id), item_type TEXT NOT NULL,
  item_id TEXT NOT NULL, PRIMARY KEY(run_id, item_type, item_id)
);
"""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def timestamp(value, field="timestamp"):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date or timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field} must be an ISO date or timestamp") from None
    if parsed.tzinfo is None:
        if len(value) != 10:
            raise ValueError(f"{field} timestamp requires a timezone")
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def iso_time(value, field="timestamp"):
    return timestamp(value, field).isoformat(timespec="microseconds").replace("+00:00", "Z")


def required_text(obj, key):
    val = obj.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ValueError(f"{key} must be a nonempty string")
    return val.strip()


def object_value(obj, key, default=None):
    value = obj.get(key, {} if default is None else default)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def list_value(obj, key):
    value = obj.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array")
    return value


def normalized_name(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def safe_url(value):
    if not isinstance(value, str):
        raise ValueError("source URL must be a string")
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http"} or not parts.netloc or parts.username or parts.password:
        raise ValueError("source URL must be an ordinary public HTTP(S) URL")
    if any(normalized_name(k) in SECRET_PARAMS for k, _ in parse_qsl(parts.query)):
        raise ValueError("credential or contact data in a URL parameter; remove it before import")
    if parts.fragment and any(normalized_name(k) in SECRET_PARAMS for k, _ in parse_qsl(parts.fragment)):
        raise ValueError("credential-like data in a URL fragment; remove it before import")
    return value


def check_no_secrets(value):
    """Reject common secret fields/URLs; free text still needs human review."""
    if isinstance(value, dict):
        for key, child in value.items():
            if normalized_name(str(key)) in SECRET_NAMES:
                raise ValueError("credential-like field present; remove credentials before import")
            check_no_secrets(child)
    elif isinstance(value, list):
        for child in value:
            check_no_secrets(child)
    elif isinstance(value, str):
        if re.search(r"\bBearer\s+[A-Za-z0-9_.\-]{12,}|(?:^|\n)\s*(?:Cookie|Authorization)\s*:", value, re.I):
            raise ValueError("credential-like text present; remove credentials before import")
        for match in URL_RE.findall(value):
            safe_url(match.rstrip(".,);]"))
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("NaN and Infinity are not valid research data")


def read_json(path):
    if path == "-":
        raw = sys.stdin.read(50 * 1024 * 1024 + 1)
    else:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read(50 * 1024 * 1024 + 1)
    if len(raw) > 50 * 1024 * 1024:
        raise ValueError("input exceeds 50 MiB; split it into independent runs")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("input must be a JSON object")
    check_no_secrets(result)
    return result


def connect(path, initialize=False):
    path = Path(path).expanduser().resolve()
    if not initialize and not path.is_file():
        raise ValueError("database does not exist; restore it or run init first")
    if initialize:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == 0 and initialize:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if tables:
            connection.close()
            raise ValueError("refusing to initialize a nonempty, unrecognized database")
        connection.executescript(DDL)
        connection.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()
    elif version != SCHEMA_VERSION:
        connection.close()
        raise ValueError(f"unsupported schema version {version}; expected {SCHEMA_VERSION}")
    connection.execute("PRAGMA journal_mode=WAL")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return connection


def doi_alias(value):
    value = unquote(str(value)).strip()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.I).lower()
    if not re.fullmatch(r"10\.\d{4,9}/\S+", value):
        raise ValueError("invalid DOI alias")
    return value


def arxiv_alias(value):
    value = str(value).strip()
    value = re.sub(r"^(?:https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/|arxiv:\s*)", "", value, flags=re.I)
    value = re.sub(r"\.pdf$", "", value, flags=re.I)
    value = re.sub(r"v\d+$", "", value)
    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[A-Za-z][A-Za-z.\-]*/\d{7})", value):
        raise ValueError("invalid arXiv alias")
    return value.lower()


def document_aliases(doc):
    aliases = []
    if doc.get("document_id"):
        aliases.append(("id", required_text(doc, "document_id")))
    for field, alias_type, normalize in (("doi", "doi", doi_alias), ("arxiv_id", "arxiv", arxiv_alias)):
        if doc.get(field):
            aliases.append((alias_type, normalize(doc[field])))
    for item in list_value(doc, "aliases"):
        if not isinstance(item, dict):
            raise ValueError("document aliases must contain type/value objects")
        typ = required_text(item, "type").lower()
        val = required_text(item, "value")
        if typ == "doi":
            val = doi_alias(val)
        elif typ == "arxiv":
            val = arxiv_alias(val)
        aliases.append((typ, val))
    if not aliases:
        raise ValueError("document requires a document_id, DOI or arXiv identifier; no title-based merging")
    return list(dict.fromkeys(aliases))


def resolve_document(db, identifier):
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("document reference must be nonempty")
    row = db.execute("SELECT document_id FROM documents WHERE document_id=?", (identifier,)).fetchone()
    if row:
        return row[0]
    alternatives = [("id", identifier)]
    for typ, normalize in (("doi", doi_alias), ("arxiv", arxiv_alias)):
        try:
            alternatives.append((typ, normalize(identifier)))
        except ValueError:
            pass
    found = set()
    for typ, val in alternatives:
        row = db.execute("SELECT document_id FROM document_aliases WHERE alias_type=? AND alias_value=?", (typ, val)).fetchone()
        if row:
            found.add(row[0])
    if len(found) != 1:
        raise ValueError("unknown or ambiguous document reference; import its metadata first")
    return found.pop()


def link_run(db, run_id, kind, item_id):
    db.execute("INSERT OR IGNORE INTO run_items VALUES (?,?,?)", (run_id, kind, item_id))


def version_entity(db, kind, item_id, run_id, payload):
    db.execute("INSERT INTO entity_versions VALUES (?,?,?,?)", (kind, item_id, run_id, canonical(payload)))
    link_run(db, run_id, kind, item_id)


def import_documents(db, docs, run_id):
    mapping = {}
    seen = set()
    for original in docs:
        if not isinstance(original, dict):
            raise ValueError("documents must contain objects")
        doc = dict(original)
        title = required_text(doc, "title")
        aliases = document_aliases(doc)
        existing = set()
        for typ, val in aliases:
            row = db.execute("SELECT document_id FROM document_aliases WHERE alias_type=? AND alias_value=?", (typ, val)).fetchone()
            if row:
                existing.add(row[0])
        if len(existing) > 1:
            raise ValueError("identifier bridge joins multiple existing documents; resolve identity explicitly before import")
        preferred = doc.get("document_id") or ("doi:" + doi_alias(doc["doi"]) if doc.get("doi") else
                      "arxiv:" + arxiv_alias(doc["arxiv_id"]) if doc.get("arxiv_id") else aliases[0][0] + ":" + aliases[0][1])
        doc_id = next(iter(existing), preferred)
        if doc_id in seen:
            raise ValueError("duplicate document identities in one run; combine metadata before import")
        seen.add(doc_id)
        doc["document_id"] = doc_id
        if doc.get("doi"):
            doc["doi"] = doi_alias(doc["doi"])
        if doc.get("arxiv_id"):
            doc["arxiv_id"] = arxiv_alias(doc["arxiv_id"])
        if doc.get("url"):
            safe_url(doc["url"])
        db.execute("""INSERT INTO documents VALUES (?,?,?,?,?)
            ON CONFLICT(document_id) DO UPDATE SET title=excluded.title,
            payload_json=excluded.payload_json,last_run_id=excluded.last_run_id""",
                   (doc_id, title, canonical(doc), run_id, run_id))
        for typ, val in aliases + [("id", doc_id)]:
            db.execute("INSERT OR IGNORE INTO document_aliases VALUES (?,?,?)", (typ, val, doc_id))
        version_entity(db, "documents", doc_id, run_id, doc)
        mapping[original.get("document_id", preferred)] = doc_id
    return mapping


def import_materials(db, materials, run_id):
    for material in materials:
        material_id = required_text(material, "material_id")
        name = required_text(material, "name")
        identity = object_value(material, "identity")
        required_text(identity, "formula")
        required_text(identity, "phase")
        required_text(identity, "sample_form")
        identity_hash = digest(identity)
        old = db.execute("SELECT identity_hash FROM materials WHERE material_id=?", (material_id,)).fetchone()
        if old and old[0] != identity_hash:
            raise ValueError("material identity changed; create a phase-specific material_id and record its relation instead")
        db.execute("""INSERT INTO materials VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(material_id) DO UPDATE SET name=excluded.name,
            payload_json=excluded.payload_json,last_run_id=excluded.last_run_id""",
                   (material_id, name, identity_hash, canonical(identity), canonical(material), run_id, run_id))
        version_entity(db, "materials", material_id, run_id, material)


def import_topics(db, topics, run_id):
    for topic in topics:
        topic_id = required_text(topic, "topic_id")
        label = required_text(topic, "label")
        required_text(topic, "definition")
        db.execute("""INSERT INTO topics VALUES (?,?,?,?,?)
            ON CONFLICT(topic_id) DO UPDATE SET label=excluded.label,
            payload_json=excluded.payload_json,last_run_id=excluded.last_run_id""",
                   (topic_id, label, canonical(topic), run_id, run_id))
        version_entity(db, "topics", topic_id, run_id, topic)


def search_scope(record):
    depth = record.get("research_depth", record.get("level"))
    if depth not in {"screening", "sourcing", "synthesis"}:
        raise ValueError("research_depth must be screening, sourcing or synthesis")
    if record.get("level") is not None and record["level"] != depth:
        raise ValueError("level and research_depth disagree")
    query_version = record.get("query_version", "1")
    if not isinstance(query_version, str) or not query_version.strip():
        raise ValueError("query_version must be a nonempty string")
    material_id = record.get("material_id")
    if material_id is not None and (not isinstance(material_id, str) or not material_id):
        raise ValueError("material_id must be null or an explicit material identifier")
    return {"source": required_text(record, "source"),
            "query": required_text(record, "query"),
            "filters": object_value(record, "filters"),
            "query_version": query_version,
            "research_depth": depth,
            "phase": required_text(record, "phase"),
            "problem": required_text(record, "problem"),
            "material_id": material_id}


def immutable_exists(db, table, id_field, item_id, payload):
    row = db.execute(f"SELECT payload_hash FROM {table} WHERE {id_field}=?", (item_id,)).fetchone()
    if row and row[0] != digest(payload):
        raise ValueError(f"{id_field} already exists with different content; use a new version identifier")
    return row is not None


def import_searches(db, searches, run_id):
    for original in searches:
        record = dict(original)
        search_id = required_text(record, "search_id")
        scope = search_scope(record)
        record.update(scope)
        status = required_text(record, "status")
        if status not in STATUSES:
            raise ValueError("invalid search status")
        required_text(record, "coverage")
        record["retrieved_at"] = iso_time(record.get("retrieved_at"), "retrieved_at")
        results = list_value(record, "result_ids")
        if any(not isinstance(x, str) or not x for x in results):
            raise ValueError("result_ids must contain nonempty strings")
        if len(results) != len(set(results)):
            raise ValueError("result_ids must use unique source record identifiers")
        record["result_ids"] = results
        count = record.get("result_count", None if status == "failed" else len(results))
        if status == "failed":
            if count is not None or results:
                raise ValueError("failed query must have result_count=null and no results; partial retrieval uses partial")
        elif not isinstance(count, int) or isinstance(count, bool) or count != len(results):
            raise ValueError("result_count must equal the number of recorded result_ids")
        record["result_count"] = count
        total = record.get("reported_total")
        if total is not None:
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise ValueError("reported_total must be null or a nonnegative integer")
            if status == "failed":
                raise ValueError("failed query must not assert a reported_total")
            if count is not None and total < count:
                raise ValueError("reported_total cannot be smaller than collected source records")
            if status == "complete_for_query" and total != count:
                raise ValueError("complete query counts do not reconcile; use partial and explain the gap")
        if status == "complete_for_query" and record.get("truncated", False):
            raise ValueError("truncated search cannot be complete_for_query")
        if not immutable_exists(db, "searches", "search_id", search_id, record):
            db.execute("INSERT INTO searches VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (search_id, run_id, scope["source"], scope["query"], digest(scope), record["retrieved_at"],
                        status, count, digest(record), canonical(record)))
        link_run(db, run_id, "searches", search_id)


def exists(db, table, id_field, identifier):
    if not db.execute(f"SELECT 1 FROM {table} WHERE {id_field}=?", (identifier,)).fetchone():
        raise ValueError(f"referenced {id_field} does not exist")


def import_claims(db, claims, run_id):
    for original in claims:
        record = json.loads(canonical(original))
        claim_id = required_text(record, "claim_id")
        text = required_text(record, "text")
        subject_type = required_text(record, "subject_type")
        subject_id = required_text(record, "subject_id")
        typ = required_text(record, "claim_type")
        if subject_type not in {"material", "topic", "document", "general"}:
            raise ValueError("invalid claim subject_type")
        if typ not in {"fact", "historical_interpretation", "hypothesis", "limitation"}:
            raise ValueError("invalid claim_type")
        if subject_type == "document":
            subject_id = resolve_document(db, subject_id)
            record["subject_id"] = subject_id
        elif subject_type in {"material", "topic"}:
            exists(db, subject_type + "s", subject_type + "_id", subject_id)
        evidence = list_value(record, "evidence")
        if typ in {"fact", "historical_interpretation"} and not evidence:
            raise ValueError("facts and historical interpretations require source evidence")
        for item in evidence:
            if not isinstance(item, dict):
                raise ValueError("evidence must contain objects")
            if not item.get("document_id") and not item.get("url"):
                raise ValueError("evidence requires a document_id or source URL")
            if item.get("document_id"):
                item["document_id"] = resolve_document(db, item["document_id"])
            if item.get("url"):
                safe_url(item["url"])
            required_text(item, "locator")
            item.setdefault("relation", "supports")
            if item["relation"] not in {"supports", "contradicts", "context", "basis_for_hypothesis"}:
                raise ValueError("invalid evidence relation")
        record["evidence"] = evidence
        previous = record.get("supersedes_claim_id")
        if previous:
            if previous == claim_id:
                raise ValueError("a claim cannot supersede itself")
            prior_claim = db.execute("SELECT subject_type,subject_id FROM claims WHERE claim_id=?", (previous,)).fetchone()
            if not prior_claim or tuple(prior_claim) != (subject_type, subject_id):
                raise ValueError("superseded claim must refer to the same research subject")
        if not immutable_exists(db, "claims", "claim_id", claim_id, record):
            db.execute("INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?)",
                       (claim_id, run_id, subject_type, subject_id, typ, text, previous, digest(record), canonical(record)))
            for index, item in enumerate(evidence):
                db.execute("INSERT INTO claim_evidence VALUES (?,?,?,?,?,?,?)",
                           (claim_id, index, item.get("document_id"), item.get("url"), item["locator"], item["relation"], canonical(item)))
        link_run(db, run_id, "claims", claim_id)


def import_assessments(db, assessments, run_id, run_as_of):
    for original in assessments:
        record = dict(original)
        assessment_id = required_text(record, "assessment_id")
        topic_id = required_text(record, "topic_id")
        material_id = required_text(record, "material_id")
        tier = required_text(record, "tier")
        if tier not in {"recommend", "watchlist", "benchmark", "excluded"}:
            raise ValueError("invalid assessment tier")
        depth = record.get("research_depth", record.get("level", "screening"))
        if depth not in {"screening", "sourcing", "synthesis"}:
            raise ValueError("assessment research_depth must be screening, sourcing or synthesis")
        if record.get("level") is not None and record["level"] != depth:
            raise ValueError("assessment level and research_depth disagree")
        record["research_depth"] = depth
        for field in ("scores", "sourcing", "synthesis"):
            if field in record:
                object_value(record, field)
        if record.get("sourcing", {}).get("status") not in {None, "confirmed", "public_offer", "author_route", "unknown"}:
            raise ValueError("sourcing.status must be confirmed, public_offer, author_route or unknown")
        exists(db, "topics", "topic_id", topic_id)
        exists(db, "materials", "material_id", material_id)
        reasons = list_value(record, "reasons")
        if not reasons or any(not isinstance(x, str) or not x.strip() for x in reasons):
            raise ValueError("every assessment requires nonempty reasons")
        claim_ids = list_value(record, "claim_ids")
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("assessment claim_ids must be unique")
        if tier == "recommend" and not claim_ids:
            raise ValueError("recommended materials require evidence-linked claims")
        for claim_id in claim_ids:
            exists(db, "claims", "claim_id", claim_id)
        if tier == "recommend":
            count = sum(db.execute("SELECT COUNT(*) FROM claim_evidence WHERE claim_id=?", (cid,)).fetchone()[0] for cid in claim_ids)
            if count == 0:
                raise ValueError("recommended material claims lack linked sources")
        record["assessed_at"] = iso_time(record.get("assessed_at", run_as_of), "assessed_at")
        previous = record.get("supersedes_assessment_id")
        if previous:
            if previous == assessment_id:
                raise ValueError("an assessment cannot supersede itself")
            old = db.execute("SELECT topic_id,material_id FROM assessments WHERE assessment_id=?", (previous,)).fetchone()
            if not old or tuple(old) != (topic_id, material_id):
                raise ValueError("superseded assessment must belong to the same material-topic pair")
        if not immutable_exists(db, "assessments", "assessment_id", assessment_id, record):
            db.execute("INSERT INTO assessments VALUES (?,?,?,?,?,?,?,?,?)",
                       (assessment_id, run_id, topic_id, material_id, tier, record["assessed_at"], previous, digest(record), canonical(record)))
            for claim_id in claim_ids:
                db.execute("INSERT INTO assessment_claims VALUES (?,?)", (assessment_id, claim_id))
        link_run(db, run_id, "assessments", assessment_id)


def import_run(db, data):
    check_no_secrets(data)
    if data.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ValueError("unsupported import schema_version")
    for section in ("documents", "materials", "topics", "searches", "claims", "assessments", "reuse"):
        if any(not isinstance(item, dict) for item in list_value(data, section)):
            raise ValueError(f"{section} must contain objects")
    run = object_value(data, "run")
    run_id = required_text(run, "run_id")
    as_of = iso_time(run.get("as_of"), "run.as_of")
    mode = required_text(run, "mode")
    if mode not in {"scout", "audit"}:
        raise ValueError("run.mode must be scout or audit")
    topic_mode = run.get("topic_mode", "targeted")
    if topic_mode not in {"targeted", "discovery"}:
        raise ValueError("topic_mode must be targeted or discovery")
    scope = object_value(run, "scope")
    payload_hash = digest(data)
    previous = db.execute("SELECT payload_hash FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if previous:
        if previous[0] != payload_hash:
            raise ValueError("run_id is immutable and already has different content; create a new run_id")
        return {"ok": True, "run_id": run_id, "idempotent": True, "changed": False}
    with db:
        db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
                   (run_id, as_of, mode, topic_mode, digest(scope), payload_hash, canonical(data), now()))
        mapping = import_documents(db, list_value(data, "documents"), run_id)
        import_materials(db, list_value(data, "materials"), run_id)
        import_topics(db, list_value(data, "topics"), run_id)
        import_searches(db, list_value(data, "searches"), run_id)
        import_claims(db, list_value(data, "claims"), run_id)
        import_assessments(db, list_value(data, "assessments"), run_id, as_of)
        for ref in list_value(data, "reuse"):
            kind = required_text(ref, "kind")
            item_id = required_text(ref, "id")
            if kind not in KINDS or kind == "runs":
                raise ValueError("invalid reuse kind")
            id_field = {"documents": "document_id", "materials": "material_id", "topics": "topic_id",
                        "searches": "search_id", "claims": "claim_id", "assessments": "assessment_id"}[kind]
            if kind == "documents":
                item_id = resolve_document(db, item_id)
            exists(db, kind, id_field, item_id)
            link_run(db, run_id, kind, item_id)
    return {"ok": True, "run_id": run_id, "idempotent": False, "changed": True,
            "document_id_map": mapping,
            "items": {kind: len(data.get(kind, [])) for kind in KINDS if kind != "runs"},
            "reused_items": len(data.get("reuse", []))}


def search_records(db, args):
    results = []
    kinds = KINDS if args.kind == "all" else (args.kind,)
    for kind in kinds:
        id_field = {"documents": "document_id", "materials": "material_id", "topics": "topic_id",
                    "searches": "search_id", "claims": "claim_id", "assessments": "assessment_id", "runs": "run_id"}[kind]
        where, values = [], []
        if args.text:
            where.append("payload_json LIKE ? ESCAPE '\\'")
            term = args.text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            values.append("%" + term + "%")
        if args.id:
            where.append(id_field + "=?")
            values.append(resolve_document(db, args.id) if kind == "documents" else args.id)
        if args.run_id:
            if kind == "runs":
                where.append("run_id=?")
                values.append(args.run_id)
            else:
                where.append(f"{id_field} IN (SELECT item_id FROM run_items WHERE run_id=? AND item_type=?)")
                values.extend((args.run_id, kind))
        sql = f"SELECT {id_field},payload_json FROM {kind}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY rowid DESC LIMIT ?"
        values.append(args.limit)
        for row in db.execute(sql, values):
            item = {"kind": kind, "id": row[0], "data": json.loads(row[1])}
            if kind == "documents":
                item["all_known_aliases"] = [dict(x) for x in db.execute(
                    "SELECT alias_type,alias_value FROM document_aliases WHERE document_id=? ORDER BY alias_type,alias_value", (row[0],))]
            results.append(item)
    return {"ok": True, "results": results, "limit_per_kind": args.limit,
            "note": "Entity records show latest metadata; immutable run payloads retain their original evidence snapshot."}


def cache_entry(rows, as_of, ttl_days):
    newest = rows[0]
    eligible = []
    for row in rows:
        age = (as_of - timestamp(row["retrieved_at"])).total_seconds() / 86400
        if row["status"] == "complete_for_query" and 0 <= age <= ttl_days:
            eligible.append((row, age))
    if eligible:
        chosen, age = eligible[0]
        decision = "reusable"
    else:
        chosen = newest
        age = (as_of - timestamp(chosen["retrieved_at"])).total_seconds() / 86400
        decision = "refresh"
    payload = json.loads(chosen["payload_json"])
    return {"scope_hash": chosen["scope_hash"], "scope": search_scope(payload), "decision": decision,
            "selected_search_id": chosen["search_id"], "selected_status": chosen["status"],
            "retrieved_at": chosen["retrieved_at"], "age_days": round(age, 3),
            "result_count": chosen["result_count"], "latest_attempt": {
                "search_id": newest["search_id"], "status": newest["status"], "retrieved_at": newest["retrieved_at"]},
            "reason": "Exact query/scope and complete cached result within TTL." if eligible else
                "No complete result in the exact scope within TTL; partial/failed/manual/future-dated records are not completion evidence."}


def cache_status(db, args):
    if not math.isfinite(args.ttl_days) or args.ttl_days < 0:
        raise ValueError("ttl-days must be finite and nonnegative")
    as_of = timestamp(args.as_of or now())
    groups = {}
    if args.scope:
        scope = search_scope(read_json(args.scope))
        scope_hash = digest(scope)
        rows = db.execute("SELECT * FROM searches WHERE scope_hash=? ORDER BY retrieved_at DESC,rowid DESC", (scope_hash,)).fetchall()
        if not rows:
            return {"ok": True, "as_of": as_of.isoformat(), "ttl_days": args.ttl_days,
                    "entries": [{"scope_hash": scope_hash, "scope": scope, "decision": "miss",
                                 "result_count": None, "reason": "No prior exact-scope search; this is not a zero-hit result."}]}
        groups[scope_hash] = rows
    else:
        for row in db.execute("SELECT * FROM searches ORDER BY retrieved_at DESC,rowid DESC"):
            groups.setdefault(row["scope_hash"], []).append(row)
    entries = [cache_entry(rows, as_of, args.ttl_days) for rows in groups.values()]
    if args.needs_refresh:
        entries = [entry for entry in entries if entry["decision"] != "reusable"]
    return {"ok": True, "as_of": as_of.isoformat(), "ttl_days": args.ttl_days, "entries": entries,
            "note": "Reuse is only a recorded-query caching decision, never a guarantee of global literature coverage."}


def snapshot(db, database, output, replace=False):
    output = Path(output).expanduser().resolve()
    if output == Path(database).expanduser().resolve():
        raise ValueError("snapshot output must differ from the open database")
    if output.exists() and not replace:
        raise ValueError("snapshot target exists; choose another path or explicitly use --replace")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".research-snapshot-", suffix=".sqlite3", dir=output.parent)
    os.close(fd)
    try:
        destination = sqlite3.connect(temp_path)
        try:
            db.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE")
            result = destination.execute("PRAGMA integrity_check").fetchall()
            if result != [("ok",)] or destination.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("snapshot integrity verification failed")
            destination.commit()
        finally:
            destination.close()
        if output.exists() and not replace:
            raise ValueError("snapshot target appeared while saving; preserve it and retry with another path")
        os.replace(temp_path, output)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    sha = hashlib.sha256(output.read_bytes()).hexdigest()
    return {"ok": True, "path": str(output), "schema_version": SCHEMA_VERSION,
            "size_bytes": output.stat().st_size, "sha256": sha,
            "integrity": "ok", "note": "Consistent single-file SQLite backup; save this file with the existing persistent file identity."}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, help="restored/local working SQLite database")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create schema v1, or verify an existing compatible database")
    imp = sub.add_parser("import-run", help="transactionally append an immutable research run")
    imp.add_argument("--file", required=True, help="run JSON file, or - for stdin")
    search = sub.add_parser("search", help="inspect records, evidence, and run snapshots")
    search.add_argument("--kind", choices=("all",) + KINDS, default="all")
    search.add_argument("--text", default="")
    search.add_argument("--id")
    search.add_argument("--run-id")
    search.add_argument("--limit", type=int, default=20, help="per-kind limit, 1–200")
    cache = sub.add_parser("cache-status", help="decide whether an exact query can reuse complete cached records")
    cache.add_argument("--scope", help="JSON containing source, query, filters, optional query_version; otherwise list all")
    cache.add_argument("--ttl-days", type=float, default=7)
    cache.add_argument("--as-of", help="ISO timestamp with timezone; default is current UTC")
    cache.add_argument("--needs-refresh", action="store_true")
    snap = sub.add_parser("snapshot", help="make and verify a WAL-safe single-file backup for persistence")
    snap.add_argument("--output", required=True)
    snap.add_argument("--replace", action="store_true")
    return p


def main():
    args = parser().parse_args()
    db = None
    try:
        db = connect(args.db, initialize=args.command == "init")
        if args.command == "init":
            result = {"ok": True, "schema_version": SCHEMA_VERSION, "path": str(Path(args.db).resolve())}
        elif args.command == "import-run":
            result = import_run(db, read_json(args.file))
        elif args.command == "search":
            if not 1 <= args.limit <= 200:
                raise ValueError("limit must be between 1 and 200")
            result = search_records(db, args)
        elif args.command == "cache-status":
            result = cache_status(db, args)
        else:
            result = snapshot(db, args.db, args.output, args.replace)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, TypeError, KeyError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    sys.exit(main())
