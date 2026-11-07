#!/usr/bin/env python3
"""Validate an evidence-led condensed-matter shortlist and render offline HTML.

Only the Python standard library is used. Validation checks evidence structure,
not whether a scientific claim is true; the author must read the cited sources.
"""
from __future__ import annotations

import argparse
import copy
import html
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


STATUSES = {
    "recommended": "主推荐", "watchlist": "待核查",
    "benchmark": "基准 / 对照", "excluded": "未入选",
}
SEARCH_STATUSES = {
    "complete_for_query": "所列查询已完成", "complete": "所列查询已完成",
    "manual": "人工 / 浏览器审查", "partial": "覆盖不完整",
    "failed": "检索失败", "blocked": "访问受阻",
    "not_attempted": "尚未检索",
}
ACCESS_LEVELS = {
    "full_text": "已读全文", "abstract": "已读摘要", "metadata": "仅元数据",
    "user_provided": "用户提供原文", "not_accessed": "未取得原文",
    "unknown": "阅读深度未记录",
}
SC_LEVELS = {
    "intrinsic_bulk": "本征体超导", "pressure_induced": "压力诱导线索",
    "doping_induced": "掺杂诱导线索", "proximity": "超导邻近效应",
    "theory": "理论假说", "family_analogy": "同族类比",
    "probe_only": "仅使用超导探针", "none": "无超导加分依据",
    "unknown": "尚未核实",
}
SOURCING_STATUSES = {
    "confirmed_available": "已有可提供确认记录", "public_offer": "公开提供线索",
    "author_route": "作者渠道线索", "unknown": "可提供性未知",
}
SYNTHESIS_STATUSES = {"not_requested": "按需后续调研", "queued": "已列入后续调研", "reviewed": "已有详细审查"}
TOPIC_WEIGHTS = {"importance": 40, "momentum": 35, "feasibility": 15, "breadth": 10}
MATERIAL_WEIGHTS = {"importance": 45, "feasibility": 35, "novelty": 10, "evidence": 10}
SCORE_LABELS = {"importance": "物理重要性", "momentum": "近期动量", "feasibility": "可做性", "breadth": "影响范围", "novelty": "问题稀缺性", "evidence": "证据质量", "superconductivity_bonus": "超导加分"}
EMPTY_WORDS = {"", "unknown", "none", "null", "n/a", "tbd", "未知", "待确认", "未核验", "待核查"}
SENSITIVE_QUERY_KEYS = {"api_key", "apikey", "access_token", "token", "key", "signature", "sig", "password", "secret", "authorization", "auth", "accesskeyid"}


def text(value, default=""):
    return value if isinstance(value, str) else default


def useful(value):
    return text(value).strip().casefold() not in EMPTY_WORDS


def escaped(value):
    return html.escape(str(value), quote=True)


def paragraph(value):
    value = text(value).strip()
    return escaped(value).replace("\n", "<br>") if value else '<span class="muted">未记录</span>'


def string_list(value):
    return [v for v in value if isinstance(v, str) and v.strip()] if isinstance(value, list) else []


def parse_date(value):
    try:
        value = text(value).strip()
        return date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def safe_url(value):
    """Reject executable, credential-bearing and malformed links; do not echo them."""
    value = text(value).strip()
    if not value or re.search(r"[\s\x00-\x1f\x7f]", value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        if any(k.casefold() in SENSITIVE_QUERY_KEYS or k.casefold().startswith(("x-amz-", "x-goog-")) for k, _ in parse_qsl(parts.query)):
            return None
        # Validate the port as well; urlsplit itself allows malformed ports.
        _ = parts.port
    except ValueError:
        return None
    return value


def is_scholar(source):
    compact = re.sub(r"[\s_\-]+", "", text(source).casefold())
    return "googlescholar" in compact or "谷歌学术" in compact


def source_family(source):
    if is_scholar(source):
        return "google-scholar"
    compact = re.sub(r"[\s_\-]+", "", text(source).casefold())
    for token in ("openalex", "arxiv", "crossref", "semanticscholar", "webofscience", "scopus", "opencitations"):
        if token in compact:
            return token
    return text(source).strip().casefold()


def reviewed_search(search, as_of):
    status = search.get("status")
    day = parse_date(search.get("searched_at"))
    if not day or day > as_of or not useful(search.get("query")) or not useful(search.get("source")):
        return False
    if not useful(search.get("coverage_notes")) or search.get("truncated") is True:
        return False
    if status in {"complete_for_query", "complete"}:
        return True
    if status == "manual":
        count = search.get("reviewed_count")
        positive_review = type(count) is int and count > 0
        confirmed_empty = search.get("confirmed_empty") is True and search.get("result_count") == 0
        return positive_review or confirmed_empty
    return False


def validate_report(raw):
    errors, warnings = [], []
    if not isinstance(raw, dict):
        return {}, ["报告根节点必须是 JSON 对象。"], []
    report = copy.deepcopy(raw)
    if not useful(report.get("title")):
        errors.append("title 必须为非空字符串。")
    as_of = parse_date(report.get("as_of"))
    if not as_of:
        errors.append("as_of 必须是有效的 ISO 日期或日期时间。")
    if report.get("mode", "scout") not in {"scout", "audit"}:
        errors.append("mode 只能是 scout 或 audit。")
    report.setdefault("mode", "scout")
    report.setdefault("selection_mode", "discover")
    if report["selection_mode"] not in {"discover", "topic"}:
        errors.append("selection_mode 只能是 discover 或 topic。")
    if report.get("schema_version", 1) != 1:
        errors.append("不支持此 schema_version；当前版本为 1。")
    report.setdefault("schema_version", 1)
    for key in ("sources", "searches", "topics"):
        if not isinstance(report.get(key), list):
            errors.append(f"{key} 必须为数组，可以为空。")
            report[key] = []
    requirements = report.get("requirements", {})
    if not isinstance(requirements, dict):
        errors.append("requirements 必须是对象。")
        requirements = {}
    min_age = requirements.get("minimum_material_age_years", 20)
    if type(min_age) is not int or not 1 <= min_age <= 300:
        errors.append("minimum_material_age_years 必须是 1–300 的整数。")
        min_age = 20
    require_scholar = requirements.get("require_google_scholar", True)
    if type(require_scholar) is not bool:
        errors.append("require_google_scholar 必须为布尔值。")
        require_scholar = True
    report["requirements"] = {**requirements, "minimum_material_age_years": min_age, "require_google_scholar": require_scholar}

    source_map, search_map = {}, {}
    for index, source in enumerate(report["sources"], 1):
        if not isinstance(source, dict):
            errors.append(f"sources[{index - 1}] 必须为对象。")
            continue
        sid = text(source.get("id")).strip()
        if not sid or sid in source_map:
            errors.append(f"sources[{index - 1}] 缺少唯一 id。")
            continue
        source_map[sid] = source
        source["_anchor"] = f"source-{index}"
        source["_label"] = f"S{index}"
        source["_url"] = safe_url(source.get("url"))
        if not source["_url"]:
            source["url"] = None
            warnings.append(f"来源 {sid} 缺少有效公开链接；未生成外链，也不能用来满足主推荐证据门槛。")
        if not useful(source.get("title")):
            warnings.append(f"来源 {sid} 缺少论文或文献标题。")
        source.setdefault("access_level", "unknown")

    for index, search in enumerate(report["searches"], 1):
        if not isinstance(search, dict):
            errors.append(f"searches[{index - 1}] 必须为对象。")
            continue
        sid = text(search.get("id")).strip()
        if not sid or sid in search_map:
            errors.append(f"searches[{index - 1}] 缺少唯一 id。")
            continue
        search_map[sid] = search
        search["_anchor"] = f"search-{index}"
        if search.get("status") not in SEARCH_STATUSES:
            warnings.append(f"检索 {sid} 的状态未知，不能用来满足查新门槛。")
        for key in ("result_count", "reviewed_count"):
            number = search.get(key)
            if number is not None and (type(number) is not int or number < 0):
                errors.append(f"检索 {sid} 的 {key} 必须为非负整数或 null。")
        if search.get("status") in {"failed", "blocked", "not_attempted"} and search.get("result_count") == 0:
            search["result_count"] = None
            warnings.append(f"检索 {sid} 未成功；结果数由 0 改为未知，避免将访问失败解释为空结果。")

    def evidence_ok(ids):
        return any(
            sid in source_map and source_map[sid].get("_url")
            and useful(source_map[sid].get("title"))
            and source_map[sid].get("access_level") in {"full_text", "abstract", "user_provided"}
            for sid in string_list(ids)
        )

    def assess_scores(owner, weights, label, novelty_status=None):
        scores = owner.get("scores")
        if not isinstance(scores, dict):
            return
        reasons = scores.get("reasons") if isinstance(scores.get("reasons"), dict) else {}
        scores["reasons"] = reasons
        values = {}
        required = list(weights)
        if weights is MATERIAL_WEIGHTS:
            required.append("superconductivity_bonus")
        have_sources = evidence_ok(scores.get("evidence_ids"))
        for key in required:
            number = scores.get(key)
            valid_number = type(number) in {int, float} and math.isfinite(number) and 0 <= number <= 5
            if number is not None and not valid_number:
                warnings.append(f"{label} 的 {SCORE_LABELS[key]} 不在 0–5 范围内，改为未评分。")
            if valid_number and (not useful(reasons.get(key)) or not have_sources):
                warnings.append(f"{label} 的 {SCORE_LABELS[key]} 缺少理由或已读来源，暂不采用此分数。")
                valid_number = False
            known_saturation = key == "novelty" and number == 0 and owner.get("status") in {"benchmark", "excluded"} and have_sources and useful(reasons.get(key))
            if key == "novelty" and novelty_status != "audited" and valid_number and not known_saturation:
                warnings.append(f"{label} 的问题稀缺性尚未完成限定范围查新，暂不评分。")
                valid_number = False
            values[key] = number if valid_number else None
        scores.update(values)
        scores["kind"] = "judgment"
        complete = all(values.get(key) is not None for key in weights)
        scores["_base"] = round(sum(values[key] * weight / 5 for key, weight in weights.items()), 1) if complete else None
        scores["_total"] = scores["_base"]
        if weights is MATERIAL_WEIGHTS:
            bonus = values.get("superconductivity_bonus")
            scores["_total"] = round(scores["_base"] + bonus, 1) if scores["_base"] is not None and bonus is not None else None
        scores["_maximum"] = 105 if weights is MATERIAL_WEIGHTS else 100

    seen_topics = set()
    for ti, topic in enumerate(report["topics"], 1):
        if not isinstance(topic, dict):
            errors.append(f"topics[{ti - 1}] 必须为对象。")
            continue
        tid = text(topic.get("id")).strip()
        if not tid or tid in seen_topics:
            errors.append(f"topics[{ti - 1}] 缺少唯一 id。")
        seen_topics.add(tid)
        topic["_anchor"] = f"topic-{ti}"
        if not useful(topic.get("title")):
            errors.append(f"主题 {tid or ti} 缺少 title。")
        assess_scores(topic, TOPIC_WEIGHTS, f"主题 {tid or ti}")
        materials = topic.get("materials")
        if not isinstance(materials, list):
            errors.append(f"主题 {tid or ti} 的 materials 必须为数组。")
            topic["materials"] = []
            continue
        seen_materials = set()
        for mi, material in enumerate(materials, 1):
            if not isinstance(material, dict):
                errors.append(f"主题 {tid or ti} 的材料 {mi} 必须为对象。")
                continue
            mid = text(material.get("id")).strip()
            if not mid or mid in seen_materials:
                errors.append(f"主题 {tid or ti} 的材料 {mi} 缺少主题内唯一 id。")
            seen_materials.add(mid)
            label = text(material.get("formula"), mid or f"{ti}-{mi}")
            if not useful(material.get("formula")):
                errors.append(f"材料 {mid or mi} 缺少 formula。")
            material["_anchor"] = f"material-{ti}-{mi}"
            material.setdefault("status", "watchlist")
            if material["status"] not in STATUSES:
                errors.append(f"材料 {label} 的 status 无效。")
            for key in ("historical", "single_crystal", "novelty", "mechanism", "superconductivity", "sourcing", "synthesis"):
                if not isinstance(material.get(key), dict):
                    material[key] = {}
            historical, crystal = material["historical"], material["single_crystal"]
            novelty, mechanism = material["novelty"], material["mechanism"]
            all_refs = []
            for section in (historical, crystal, mechanism, material["superconductivity"]):
                all_refs.extend(string_list(section.get("evidence_ids")))
            missing = sorted(set(all_refs) - source_map.keys())
            if missing:
                warnings.append(f"材料 {label} 引用了未收录来源：{', '.join(missing)}。")
            missing_searches = sorted(set(string_list(novelty.get("search_ids"))) - search_map.keys())
            if missing_searches:
                warnings.append(f"材料 {label} 的查新记录不存在：{', '.join(missing_searches)}。")
            issues = []
            if missing:
                issues.append("部分关键引文尚未收录或核实。")
            if missing_searches:
                issues.append("部分查新记录尚未提供。")
            year = historical.get("year")
            if type(year) is not int or not as_of or not 1000 <= year <= as_of.year - min_age:
                issues.append(f"尚无达到 {min_age} 年历史门槛的已核验实验年份。")
            if not evidence_ok(historical.get("evidence_ids")):
                issues.append("历史实验缺少已阅读且可追溯的来源。")
            if crystal.get("grade") not in {"C2", "C3", "C4"} or not evidence_ok(crystal.get("evidence_ids")):
                issues.append("尚未满足 C2 及以上单晶证据要求。")
            if not useful(material.get("phase")):
                issues.append("材料晶相或结构身份仍不明确。")
            if not useful(material.get("reason")):
                issues.append("尚未给出具体入选理由。")
            if not useful(mechanism.get("facts")) or not useful(mechanism.get("hypothesis")) or not evidence_ok(mechanism.get("evidence_ids")):
                issues.append("机制桥梁尚未完整区分已知事实、证据和待验证假说。")
            if not useful(mechanism.get("next_experiment")):
                issues.append("缺少可检验的下一步实验。")
            searches = [search_map[s] for s in string_list(novelty.get("search_ids")) if s in search_map]
            successful = [s for s in searches if as_of and reviewed_search(s, as_of)]
            names = {source_family(s.get("source")) for s in successful}
            non_scholar_complete = any(not is_scholar(s.get("source")) and s.get("status") in {"complete_for_query", "complete"} for s in successful)
            if novelty.get("status") != "audited" or not useful(novelty.get("scope")) or len(names) < 2 or not non_scholar_complete:
                issues.append("查新尚未具备明确边界、两个互补已审查来源及至少一个完成所列查询的学术源。")
            if novelty.get("citation_chain_checked") is not True or not useful(novelty.get("citation_chain_note")):
                issues.append("后向 / 前向引用链审查尚未记录完整。")
            if require_scholar and not any(is_scholar(s.get("source")) for s in successful):
                issues.append("Google Scholar 补检尚未完成；需记录浏览器已审查条目或明确空结果。")
            material["_qualification_issues"] = issues
            if material["status"] == "recommended" and issues:
                material["_requested_status"] = "recommended"
                material["status"] = "watchlist"
                warnings.append(f"材料 {label} 已从主推荐降为待核查：{len(issues)} 项证据条件尚未满足。")
            effective_novelty = novelty.get("status") if not any("查新" in issue or "引用链" in issue or "Scholar" in issue for issue in issues) else "partial"
            assess_scores(material, MATERIAL_WEIGHTS, f"材料 {label}", effective_novelty)
            sourcing = material["sourcing"]
            sourcing.setdefault("availability_status", "unknown")
            if sourcing["availability_status"] not in SOURCING_STATUSES:
                sourcing["availability_status"] = "unknown"
            growers = sourcing.get("growers", [])
            if not isinstance(growers, list):
                growers = []
            sourcing["growers"] = [g for g in growers if isinstance(g, dict)]
            for grower in sourcing["growers"]:
                grower["_url"] = safe_url(grower.get("url"))
                if not grower["_url"]:
                    grower["url"] = None
                grower["_verified"] = evidence_ok(grower.get("evidence_ids"))
            if sourcing["availability_status"] in {"confirmed_available", "public_offer"}:
                checked = parse_date(sourcing.get("checked_at"))
                if not evidence_ok(sourcing.get("availability_evidence_ids")) or not checked or (as_of and checked > as_of):
                    sourcing["availability_status"] = "author_route" if any(g["_verified"] for g in sourcing["growers"]) else "unknown"
                    warnings.append(f"材料 {label} 的样品可提供性缺少专门证据或检查日期，已降为渠道线索/未知。")
            material["synthesis"].setdefault("status", "not_requested")
    report["_warnings"] = warnings
    return report, errors, warnings


CSS = """
:root{--ink:#17312e;--muted:#647673;--line:#dce5df;--paper:#fffefa;--bg:#f2f5ef;--green:#17674f;--pale:#e5f0e8;--amber:#9b6320;--amber-bg:#fbefd9;--red:#a43d3d}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,"Noto Sans SC","PingFang SC","Microsoft YaHei",system-ui,sans-serif;font-size:15px;line-height:1.75}a{color:var(--green);text-underline-offset:3px;overflow-wrap:anywhere}a:hover{color:#0b4532}button,input{font:inherit}button{cursor:pointer}main{max-width:1160px;margin:auto;padding:40px 28px 72px}.eyebrow{font-size:12px;letter-spacing:.15em;text-transform:uppercase;font-weight:700;color:var(--green)}h1{font-size:clamp(28px,4.5vw,44px);line-height:1.23;letter-spacing:-.03em;margin:14px 0 20px;max-width:920px}h2{font-size:22px;line-height:1.4;margin:0}h3{font-size:17px;margin:0 0 10px}p{margin:0 0 12px}.intro{max-width:950px;font-size:16px}.muted{color:var(--muted)}.meta,.chips{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.meta{font-size:13px;margin:20px 0}.chip{border-radius:100px;padding:3px 11px;font-size:12px;font-weight:650;background:#e7ede8;color:#445750;white-space:nowrap}.recommended{background:#dceee4;color:#135b40}.watchlist{background:var(--amber-bg);color:var(--amber)}.benchmark{background:#e6eaf4;color:#41577c}.excluded{background:#f2e5e3;color:#8b4641}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:28px 0}.stat{padding:17px 20px;border:1px solid var(--line);border-radius:14px;background:var(--paper)}.stat strong{display:block;font-size:29px;line-height:1.2;font-variant-numeric:tabular-nums}.stat span{font-size:12px;color:var(--muted)}.box{background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:19px 22px;margin:18px 0}.notice{border-left:4px solid #c39947}.notice ul{padding-left:22px;margin:6px 0}.section-head{display:flex;gap:15px;align-items:baseline;justify-content:space-between;margin:38px 0 15px}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}.toolbar input{flex:1;min-width:200px;padding:11px 15px;border:1px solid #bdcfc1;border-radius:10px;background:var(--paper)}.toolbar button{border:1px solid var(--line);background:var(--paper);color:var(--ink);padding:9px 15px;border-radius:10px}.toolbar button:hover{background:var(--pale)}details{scroll-margin-top:20px}summary{cursor:pointer;list-style:none}summary::-webkit-details-marker{display:none}.topic{background:var(--paper);border:1px solid #cfdcd1;border-radius:16px;margin:16px 0;overflow:hidden}.topic>summary{display:flex;gap:15px;padding:23px 24px;align-items:flex-start}.topic>summary:before,.material>summary:before{content:'＋';color:var(--green);font-size:21px;line-height:1.35;font-weight:400;flex:0 0 24px}.topic[open]>summary:before,.material[open]>summary:before{content:'−'}.topic>summary:hover{background:#f6f9f2}.topic-content{padding:0 24px 24px;border-top:1px solid var(--line)}.topic-summary{flex:1;min-width:0}.topic-title{font-size:21px;font-weight:700;line-height:1.4}.topic-question{font-size:14px;color:var(--muted);margin-top:7px}.topic-content>.heat{padding:19px 0 4px}.heat strong{font-size:13px;color:var(--green)}.group-label{font-size:12px;color:var(--muted);font-weight:700;letter-spacing:.07em;margin:23px 0 9px}.material{border:1px solid var(--line);border-radius:12px;margin:10px 0;background:white;overflow:hidden}.material>summary{display:flex;gap:9px;padding:17px 18px;align-items:flex-start}.material>summary:hover{background:#fafcf8}.material-head{flex:1;min-width:0}.formula{font-size:19px;font-weight:750;margin-right:9px;overflow-wrap:anywhere}.reason{font-size:14px;margin:8px 0 0;color:#425d52}.material-body{border-top:1px solid var(--line);padding:18px 22px}.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:22px}.evidence-block{margin-bottom:22px;min-width:0}.label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.1em;font-weight:750;color:var(--green);margin-bottom:6px}.evidence-block p{font-size:14px;margin:0 0 9px;overflow-wrap:anywhere}.refs{display:inline-flex;gap:5px;flex-wrap:wrap;vertical-align:baseline;margin-left:4px}.ref{font-size:11px;text-decoration:none;background:var(--pale);border-radius:5px;padding:1px 6px;font-weight:700}.invalid-ref{font-size:11px;color:var(--red)}.fact-grid{display:grid;grid-template-columns:90px 1fr;gap:6px 12px;font-size:13px;margin:0 0 14px}.fact-grid dt{color:var(--muted)}.fact-grid dd{margin:0;overflow-wrap:anywhere}.qualification{background:#fff8eb;border-radius:9px;padding:13px 16px;margin:8px 0 18px;font-size:13px}.qualification ul{margin:4px 0;padding-left:20px}.source-list{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:1fr 1fr;gap:12px}.source-list li{padding:17px 19px;border:1px solid var(--line);background:var(--paper);border-radius:12px;scroll-margin-top:20px;overflow-wrap:anywhere}.source-tag{font-size:12px;font-weight:750;color:var(--green);margin-right:8px}.source-title{font-size:14px;font-weight:650}.source-meta{font-size:12px;color:var(--muted);margin:8px 0 0}.source-note{font-size:12px;margin-top:8px}.audit{background:var(--paper);border:1px solid var(--line);border-radius:12px;margin:12px 0;padding:0 18px}.audit>summary{padding:15px 0;font-weight:650}.audit-body{border-top:1px solid var(--line);padding:14px 0}.query{white-space:pre-wrap;overflow-wrap:anywhere;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;background:#f0f4ed;border-radius:8px;padding:10px 12px}.small{font-size:13px}.empty{padding:24px;color:var(--muted);text-align:center}.footer{border-top:1px solid var(--line);padding-top:20px;margin-top:35px;color:var(--muted);font-size:12px}.backtop{float:right}.nojs{font-size:12px;color:var(--muted)}[hidden]{display:none!important}:focus-visible{outline:3px solid #91b9a1;outline-offset:3px}@media(max-width:720px){main{padding:26px 16px 50px}.stats{grid-template-columns:repeat(2,1fr);gap:9px}.stat{padding:14px}.detail-grid,.source-list{grid-template-columns:1fr}.detail-grid{gap:0}.topic>summary{padding:19px 16px;gap:8px}.topic-content{padding:0 14px 16px}.material>summary{padding:15px 12px}.material-body{padding:16px}.section-head{align-items:flex-start;flex-direction:column;gap:5px}.fact-grid{grid-template-columns:80px 1fr}.topic-title{font-size:19px}.box{padding:16px}.formula{font-size:18px}}@media print{body{background:white;font-size:10pt}main{max-width:none;padding:0}.toolbar,.backtop,.nojs{display:none!important}.topic,.material,.box,.source-list li,.audit{box-shadow:none;border-color:#bbb;break-inside:auto}.material>summary,.evidence-block,.source-list li{break-inside:avoid}.stats{grid-template-columns:repeat(4,1fr)}.topic>summary,.material>summary{padding:12px}.topic-content,.material-body{padding:12px}.detail-grid{gap:16px}.source-list{display:block}.source-list li{margin:8px 0}.ref{background:none;padding:0 3px}h1{font-size:26pt}a{color:#17312e;text-decoration:none}details::details-content{content-visibility:visible!important}details>*:not(summary){display:block!important}}
"""
CSS += """
.score-line{display:flex;gap:9px;flex-wrap:wrap;margin-top:10px;color:#476453;font-size:12px}.score-line b{font-variant-numeric:tabular-nums}.score-total{background:#edf4e7;border-radius:6px;padding:0 7px}.sourcing-preview{font-size:12px;color:#667762;margin-top:9px}.overview{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--paper);margin:15px 0 24px}.overview table{width:100%;border-collapse:collapse;min-width:590px;font-size:13px}.overview th{background:#eaf0e6;color:#476251;text-align:left;font-size:11px;letter-spacing:.03em;padding:12px 15px;white-space:nowrap}.overview td{padding:14px 15px;border-top:1px solid var(--line);vertical-align:top;font-variant-numeric:tabular-nums}.overview td:first-child{min-width:230px}.overview .topic-link{font-weight:700;text-decoration:none}.score-rationale{margin:8px 0 0;padding-left:18px;font-size:12px;color:#5a6e5d}.grower{font-size:13px;margin-bottom:8px}.muted-score{color:#88958b}.rating{font-size:13px;white-space:nowrap}@media print{.overview{overflow:visible}.overview table{min-width:0}}
"""

JS = """
const search = document.querySelector('#filter');
const topics = [...document.querySelectorAll('details.topic')];
search.addEventListener('input', () => {
  const q = search.value.trim().toLocaleLowerCase(); let visible = 0;
  for (const topic of topics) {
    const topicMatch = !q || topic.dataset.search.toLocaleLowerCase().includes(q);
    let any = false;
    for (const material of topic.querySelectorAll('details.material')) {
      const match = topicMatch || material.dataset.search.toLocaleLowerCase().includes(q);
      material.hidden = !match; any ||= match;
    }
    topic.hidden = !(topicMatch || any); if (!topic.hidden) visible++;
    if (q && !topic.hidden) topic.open = true;
  }
  document.querySelector('#no-matches').hidden = visible > 0;
  document.querySelector('#filter-status').textContent = q ? `显示 ${visible} 个主题` : '';
});
document.querySelector('#expand').addEventListener('click', () => document.querySelectorAll('details:not([hidden])').forEach(d => d.open = true));
document.querySelector('#collapse').addEventListener('click', () => document.querySelectorAll('details').forEach(d => d.open = false));
document.querySelector('#print').addEventListener('click', () => window.print());
document.querySelectorAll('[data-open-topic]').forEach(link => link.addEventListener('click', () => {const target = document.getElementById(link.dataset.openTopic); if(target) target.open = true;}));
let beforePrint = [];
window.addEventListener('beforeprint', () => {beforePrint = [...document.querySelectorAll('details')].map(d => [d,d.open]); beforePrint.forEach(([d]) => d.open = true);});
window.addEventListener('afterprint', () => beforePrint.forEach(([d,state]) => d.open = state));
"""


def render_html(report):
    sources = {s["id"]: s for s in report["sources"]}
    searches = {s["id"]: s for s in report["searches"]}

    def refs(ids):
        links = []
        for sid in dict.fromkeys(string_list(ids)):
            source = sources.get(sid)
            if source:
                links.append(f'<a class="ref" href="#{source["_anchor"]}" title="{escaped(source.get("title", sid))}">{source["_label"]}</a>')
            else:
                links.append(f'<span class="invalid-ref">未核验来源：{escaped(sid)}</span>')
        return '<span class="refs">' + "".join(links) + '</span>' if links else ""

    def bullet_list(items):
        return "<ul>" + "".join(f"<li>{paragraph(x)}</li>" for x in string_list(items)) + "</ul>"

    def evidence_block(label, body):
        return f'<div class="evidence-block"><span class="label">{escaped(label)}</span>{body}</div>'

    def rating(value, maximum=5):
        return f'<b>{value:g}</b> / {maximum}' if type(value) in {int, float} else '<span class="muted-score">未评分</span>'

    def score_line(owner):
        scores = owner.get("scores")
        if not isinstance(scores, dict):
            return ""
        total = scores.get("_total")
        base = scores.get("_base")
        total_text = rating(total, scores.get("_maximum", 100))
        if total is None and base is not None:
            total_text = f'基础 {rating(base, 100)}；超导加分未评分'
        return f'<div class="score-line"><span>物理重要性 {rating(scores.get("importance"))}</span><span>可做性 {rating(scores.get("feasibility"))}</span><span class="score-total">综合 {total_text}</span><span class="muted">启发式判断</span></div>'

    def score_details(owner, weights):
        scores = owner.get("scores")
        if not isinstance(scores, dict):
            return ""
        items = []
        keys = list(weights)
        if weights is MATERIAL_WEIGHTS:
            keys.append("superconductivity_bonus")
        for key in keys:
            weight = f'权重 {weights[key]}%' if key in weights else '额外 0–5 分'
            reason = scores.get("reasons", {}).get(key)
            items.append(f'<li><strong>{SCORE_LABELS[key]}</strong> · {rating(scores.get(key))} · {weight}：{paragraph(reason)}</li>')
        return evidence_block("评分依据 · 非统计排名 / 非发现概率", '<ul class="score-rationale">' + ''.join(items) + '</ul>' + refs(scores.get("evidence_ids")))

    def sourcing_summary(material):
        sourcing = material["sourcing"]
        growers = [g for g in sourcing.get("growers", []) if g.get("_verified")]
        names = []
        for grower in growers[:3]:
            name = text(grower.get("name"), "未注明姓名")
            institution = text(grower.get("institution"))
            names.append(escaped(name + (f'（{institution}）' if institution else '')))
        label = "；".join(names) if names else "尚无已核实的生长团队 / 作者线索"
        availability = SOURCING_STATUSES[sourcing["availability_status"]]
        return f'<div class="sourcing-preview">生长 / 作者线索：{label} · {availability}</div>'

    def sourcing_details(material):
        sourcing, synthesis = material["sourcing"], material["synthesis"]
        growers = []
        for grower in sourcing.get("growers", []):
            name = escaped(grower.get("name", "未注明姓名"))
            if grower.get("_url"):
                name = f'<a href="{escaped(grower["_url"])}" target="_blank" rel="noopener noreferrer">{name} ↗</a>'
            verification = '' if grower.get("_verified") else ' · 尚未核实角色'
            growers.append(f'<div class="grower"><strong>{name}</strong> · {paragraph(grower.get("institution"))}<br>{paragraph(grower.get("role"))}{verification}{refs(grower.get("evidence_ids"))}</div>')
        availability = SOURCING_STATUSES[sourcing["availability_status"]]
        details = ''.join(growers) + f'<p><strong>{availability}</strong>{refs(sourcing.get("availability_evidence_ids"))}</p><p>{paragraph(sourcing.get("note"))}</p>'
        if sourcing.get("checked_at"):
            details += f'<p class="small muted">线索核查日期：{escaped(sourcing["checked_at"])}</p>'
        synth_status = SYNTHESIS_STATUSES.get(synthesis.get("status"), "状态未记录")
        details += f'<p class="small"><strong>详细合成：</strong>{synth_status}</p>'
        if useful(synthesis.get("note")):
            details += f'<p class="small">{paragraph(synthesis["note"])}</p>'
        return evidence_block("生长团队与样品渠道", details)

    def material_html(material):
        hist, crystal = material["historical"], material["single_crystal"]
        novelty, mech, sc = material["novelty"], material["mechanism"], material["superconductivity"]
        status = material["status"]
        formula = material["formula"]
        tag = f'<span class="chip {status}">{STATUSES[status]}</span>'
        grade = text(crystal.get("grade"), "未分级")
        identity = f'<dl class="fact-grid"><dt>晶相 / 结构</dt><dd>{paragraph(material.get("phase"))}</dd><dt>检索别名</dt><dd>{paragraph("、".join(string_list(material.get("aliases"))))}</dd><dt>单晶等级</dt><dd>{escaped(grade)}</dd></dl>'
        historical = f'<p><strong>已核验历史年：{escaped(hist.get("year") if hist.get("year") is not None else "未知")}</strong>{refs(hist.get("evidence_ids"))}</p><p>{paragraph(hist.get("note"))}</p>'
        single = f'<p>{paragraph(crystal.get("note"))}{refs(crystal.get("evidence_ids"))}</p><dl class="fact-grid"><dt>生长方法</dt><dd>{paragraph(crystal.get("method"))}</dd><dt>样品尺寸</dt><dd>{paragraph(crystal.get("dimensions"))}</dd></dl>'
        search_links = " ".join(f'<a class="ref" href="#{searches[s]["_anchor"]}">检索 {escaped(s)}</a>' for s in string_list(novelty.get("search_ids")) if s in searches)
        novel = f'<p>{paragraph(novelty.get("scope"))}</p><p>{paragraph(novelty.get("note"))}</p><p class="small">引用链：{paragraph(novelty.get("citation_chain_note"))}</p><p>{search_links}</p>'
        facts = f'<p>{paragraph(mech.get("facts"))}{refs(mech.get("evidence_ids"))}</p>'
        hypothesis = f'<p>{paragraph(mech.get("hypothesis"))}</p>'
        counter = f'<p>{paragraph(mech.get("counterevidence"))}</p>'
        experiment = f'<p>{paragraph(mech.get("next_experiment"))}</p>'
        superc = f'<p><strong>{escaped(SC_LEVELS.get(sc.get("level"), text(sc.get("level"), "尚未核实")))}</strong>{refs(sc.get("evidence_ids"))}</p><p>{paragraph(sc.get("note"))}</p>'
        checks = ""
        issues = material.get("_qualification_issues", [])
        if status == "watchlist" and issues:
            intro = "本条已从主推荐降为待核查" if material.get("_requested_status") else "仍待补齐的主推荐条件"
            checks = f'<div class="qualification"><strong>{intro}</strong>{bullet_list(issues)}</div>'
        changes = string_list(material.get("changes"))
        change_html = evidence_block("本期变化", bullet_list(changes)) if changes else ""
        terms = " ".join([formula, text(material.get("phase")), " ".join(string_list(material.get("aliases"))), text(material.get("reason")), text(mech.get("facts")), text(mech.get("hypothesis"))])
        return f'''<details class="material" id="{material["_anchor"]}" data-search="{escaped(terms)}"><summary><div class="material-head"><span class="formula">{escaped(formula)}</span>{tag}<p class="reason">{paragraph(material.get("reason"))}</p>{score_line(material)}{sourcing_summary(material)}</div></summary><div class="material-body">{identity}{checks}<div class="detail-grid"><div>{evidence_block("历史实验证据", historical)}{evidence_block("单晶与生长", single)}{sourcing_details(material)}{evidence_block("查新范围与现状", novel)}{evidence_block("超导关联 · 单独标注", superc)}</div><div>{score_details(material, MATERIAL_WEIGHTS)}{evidence_block("已知事实", facts)}{evidence_block("研究假说 · 尚待验证", hypothesis)}{evidence_block("反证与竞争解释", counter)}{evidence_block("下一步验证", experiment)}{change_html}</div></div></div></details>'''

    topic_html = []
    counts = Counter()
    for topic in report["topics"]:
        materials = topic["materials"]
        counts.update(m["status"] for m in materials)
        local = Counter(m["status"] for m in materials)
        tags = " ".join(f'<span class="chip {status}">{STATUSES[status]} {local[status]}</span>' for status in STATUSES if local[status])
        groups = []
        for status in STATUSES:
            group = [m for m in materials if m["status"] == status]
            if group:
                groups.append(f'<div class="group-label">{STATUSES[status]} · {len(group)}</div>' + "".join(material_html(m) for m in group))
        no_materials = '<p class="empty">本轮没有证据充分的材料条目；保留主题，等待补齐检索。</p>' if not materials else ""
        heat = f'<div class="heat"><strong>近期关注依据</strong><p>{paragraph(topic.get("heat_status"))}{refs(topic.get("heat_evidence_ids"))}</p></div>'
        terms = text(topic.get("title")) + " " + text(topic.get("question"))
        topic_html.append(f'''<details class="topic" id="{topic["_anchor"]}" data-search="{escaped(terms)}"><summary><div class="topic-summary"><div class="topic-title">{escaped(topic["title"])}</div><div class="topic-question">{paragraph(topic.get("question"))}</div>{score_line(topic)}<div class="chips" style="margin-top:12px">{tags or '<span class="chip">待建立材料清单</span>'}</div></div></summary><div class="topic-content">{heat}{score_details(topic, TOPIC_WEIGHTS)}{''.join(groups)}{no_materials}</div></details>''')

    source_html = []
    for source in report["sources"]:
        title = escaped(source.get("title") or source["id"])
        if source.get("_url"):
            title = f'<a href="{escaped(source["_url"])}" target="_blank" rel="noopener noreferrer">{title} ↗</a>'
        else:
            title += ' <span class="invalid-ref">无有效公开链接</span>'
        meta = [str(source["year"]) if source.get("year") is not None else "年份未记录", text(source.get("kind"), "来源类型未记录"), ACCESS_LEVELS.get(source.get("access_level"), text(source.get("access_level"), "阅读深度未记录"))]
        source_html.append(f'<li id="{source["_anchor"]}"><span class="source-tag">{source["_label"]}</span><span class="source-title">{title}</span><p class="source-meta">{escaped(" · ".join(meta))}</p><p class="source-note">{paragraph(source.get("note"))}</p></li>')

    audit_html = []
    for search in report["searches"]:
        status = SEARCH_STATUSES.get(search.get("status"), "状态未确认")
        count = search.get("result_count")
        count_text = str(count) if type(count) is int else "未知 / 未统计"
        reviewed = search.get("reviewed_count")
        reviewed_text = str(reviewed) if type(reviewed) is int else "未记录"
        results = string_list(search.get("result_ids"))
        result_text = "、".join(results) if results else "未列出；不能由此判断无文献"
        if search.get("confirmed_empty") is True and count == 0:
            result_text = "检索记录明确确认本查询范围内为空；不等于全球无研究"
        audit_html.append(f'''<details class="audit" id="{search["_anchor"]}"><summary>{escaped(search.get("id"))} · {escaped(search.get("source", "来源未记录"))} <span class="chip">{escaped(status)}</span></summary><div class="audit-body"><p class="small muted">检索时间：{escaped(search.get("searched_at", "未记录"))}　记录数：{count_text}　已审查：{reviewed_text}</p><div class="query">{escaped(search.get("query", "未记录"))}</div><p class="small" style="margin-top:12px">{paragraph(search.get("coverage_notes"))}</p><p class="small muted">条目标识：{escaped(result_text)}</p></div></details>''')

    run = report.get("run") if isinstance(report.get("run"), dict) else {}
    changes = string_list(run.get("change_summary"))
    change_block = f'<section class="box"><h3>相对上期的变化</h3>{bullet_list(changes)}</section>' if changes else ""
    limitations = string_list(report.get("limitations"))
    if report["mode"] == "scout":
        limitations.insert(0, "本轮为探索性检索。主题按证据呈现，未构成完整领域语料的定量热度排名。")
    limitations.append("未检出仅限所列查询与日期；历史单晶存在不等于当前生长路线已复现，研究假说不等于已证实性质。")
    if any(isinstance(t.get("scores"), dict) or any(isinstance(m.get("scores"), dict) for m in t["materials"]) for t in report["topics"]):
        limitations.append("分数为基于列明证据的启发式判断，强调物理重要性与可做性；不是统计热度名次或发现成功概率。未知子分不填零，不对剩余权重重新归一化。")
    limits_block = f'<section class="box notice"><h3>范围与证据边界</h3>{bullet_list(list(dict.fromkeys(limitations)))}</section>'
    warnings = report.get("_warnings", [])
    warning_block = f'<details class="audit"><summary>待补齐与质量检查 · {len(warnings)} 项</summary><div class="audit-body small">{bullet_list(warnings)}</div></details>' if warnings else ""
    mode_label = "语料审计" if report["mode"] == "audit" else "探索清单"
    selection_label = "指定话题" if report["selection_mode"] == "topic" else "热点发现"
    report_day = parse_date(report["as_of"]).isoformat()
    run_label = f'本期：{escaped(run.get("id", "未记录"))}'
    if run.get("previous_run_id"):
        run_label += f' · 上期：{escaped(run["previous_run_id"])}'
    stats = [(len(report["topics"]), "研究主题"), (sum(counts.values()), "材料 × 主题条目"), (counts["recommended"], "主推荐"), (counts["watchlist"], "待核查")]
    stat_html = "".join(f'<div class="stat"><strong>{n}</strong><span>{label}</span></div>' for n, label in stats)
    overview_rows = []
    for topic in report["topics"]:
        scores = topic.get("scores") if isinstance(topic.get("scores"), dict) else {}
        overview_rows.append(f'<tr><td><a class="topic-link" data-open-topic="{topic["_anchor"]}" href="#{topic["_anchor"]}">{escaped(topic["title"])}</a><div class="small muted">{len(topic["materials"])} 条材料记录</div></td><td class="rating">{rating(scores.get("importance"))}</td><td class="rating">{rating(scores.get("momentum"))}</td><td class="rating">{rating(scores.get("feasibility"))}</td><td class="rating">{rating(scores.get("_total"), 100)}</td></tr>')
    overview = '<div class="overview"><table><thead><tr><th>话题 · 点击展开</th><th>物理重要性</th><th>近期动量</th><th>可做性</th><th>综合 / 100</th></tr></thead><tbody>' + ''.join(overview_rows) + '</tbody></table></div>' if overview_rows else ''
    title = escaped(report["title"])
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><title>{title}</title><style>{CSS}</style></head>
<body><main id="top"><header><div class="eyebrow">Condensed Matter · Research Record</div><h1>{title}</h1><div class="intro">{paragraph(report.get("summary"))}</div><div class="meta"><span class="chip">{selection_label}</span><span class="chip">{mode_label}</span><span>检索截止：{report_day}</span><span>单晶优先 · 超导加分</span></div><p class="small muted">{paragraph(report.get("scope"))}</p><p class="small muted">{run_label}</p></header><div class="stats">{stat_html}</div>{overview}{change_block}{limits_block}{warning_block}<section aria-labelledby="topic-heading"><div class="section-head"><h2 id="topic-heading">热点话题与材料清单</h2><span class="small muted">点击话题，再展开材料证据</span></div><div class="toolbar"><input id="filter" type="search" aria-label="筛选主题、材料或机制" placeholder="搜索话题、化学式、别名或机制…"><button id="expand" type="button">全部展开</button><button id="collapse" type="button">全部收起</button><button id="print" type="button">打印 / PDF</button></div><p id="filter-status" class="small muted" role="status" aria-live="polite"></p><noscript><p class="nojs">当前未启用脚本；点击每个标题仍可展开全部证据。</p></noscript>{''.join(topic_html) or '<p class="empty">本轮尚无可列出的主题。</p>'}<p id="no-matches" class="empty" hidden>未找到匹配条目。试试化学式别名或更短的关键词。</p></section><section aria-labelledby="sources-heading"><div class="section-head"><h2 id="sources-heading">文献与证据出处</h2><span class="small muted">{len(sources)} 条来源 · 保留阅读深度</span></div><ol class="source-list">{''.join(source_html)}</ol></section><section aria-labelledby="audit-heading"><div class="section-head"><h2 id="audit-heading">检索记录</h2><span class="small muted">查询、时间、覆盖与失败状态</span></div>{''.join(audit_html) or '<p class="empty">尚未记录检索日志；不能据此作新颖性结论。</p>'}</section><footer class="footer"><a class="backtop" href="#top">返回顶部 ↑</a>凝聚态热点 · 老材料新机会。保留原始来源、研究假说与版本变化，供后续复核与增量更新。<br>名单资格与证据完整度分开呈现；不将未知值填为零，不将来源缺失解释为研究空白。</footer></main><script>{JS}</script></body></html>'''


def public_report(value):
    if isinstance(value, dict):
        return {k: public_report(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [public_report(v) for v in value]
    return value


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main(argv=None):
    parser = argparse.ArgumentParser(description="校验凝聚态材料清单并生成可离线展开的 HTML 报告。")
    parser.add_argument("input", type=Path, help="符合 references/output-schema.md 的 JSON")
    parser.add_argument("--output", type=Path, help="HTML 输出路径；非 validate-only 时必填")
    parser.add_argument("--validate-only", action="store_true", help="仅校验并输出 JSON 摘要")
    parser.add_argument("--normalized-json", type=Path, help="可选保存降级后的报告 JSON；保留原始输入不变")
    parser.add_argument("--fail-on-downgrade", action="store_true", help="主推荐发生证据降级时退出 2")
    args = parser.parse_args(argv)
    if not args.validate_only and not args.output:
        parser.error("请提供 --output，或使用 --validate-only。")
    paths = [p.resolve() for p in [args.input, args.output, args.normalized_json] if p is not None]
    if len(set(paths)) != len(paths):
        parser.error("输入、HTML 和规范化 JSON 必须使用不同路径。")
    try:
        raw = json.loads(args.input.read_text(encoding="utf-8"))
        report, errors, warnings = validate_report(raw)
    except (OSError, ValueError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False), file=sys.stderr)
        return 1
    downgrades = sum(m.get("_requested_status") == "recommended" for t in report.get("topics", []) if isinstance(t, dict) for m in t.get("materials", []) if isinstance(m, dict))
    summary = {"valid": not errors, "errors": errors, "warnings": warnings, "downgraded_count": downgrades}
    if errors or (args.fail_on_downgrade and downgrades):
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if errors else 2
    try:
        if args.normalized_json:
            atomic_write(args.normalized_json, json.dumps(public_report(report), ensure_ascii=False, indent=2) + "\n")
        if not args.validate_only:
            atomic_write(args.output, render_html(report))
            summary["output"] = str(args.output.resolve())
    except OSError as exc:
        summary["valid"] = False
        summary["errors"].append(str(exc))
        print(json.dumps(summary, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
