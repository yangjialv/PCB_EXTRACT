#!/usr/bin/env python
from __future__ import annotations

import argparse
import copy
import json
import sys
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plagent.backend.evaluation.pi_knowledge_fusion import DeviceLibraryResolver, build_component_knowledge_map
from plagent.backend.evaluation.pi_mult_path_eval import _enrich_component_attrs, _extract_net_connections, _get_positions, _path_a, _path_b, _path_c
from tools.convert_benchmark_docx_labels import parse_docx_labels_with_fallback
from tools.run_hi3519_abc_recall import _find_path_a_evidence, _find_path_b_evidence, _find_path_c_evidence, _restrict_state_to_target_rails
from tools.run_numeric_real_board_eval import _build_state_from_board
from tools.run_real_evaluation_pipeline import _attach_routing_metrics_from_ipc, _build_state_from_netlist


GROUND_RAIL_ALIASES = {"GND", "PGND", "AGND", "DGND", "0", "0V", "G"}
RLC_REF_RE = re.compile(r"^[RLC]\d+$", flags=re.IGNORECASE)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_rail_token(text: Any) -> str:
    return str(text or "").strip().upper().replace(" ", "")


def _is_ground_rail(text: Any) -> bool:
    token = _normalize_rail_token(text)
    if token in GROUND_RAIL_ALIASES:
        return True
    return token.endswith("GND")


def _is_rlc_component(component: Any) -> bool:
    return bool(RLC_REF_RE.match(str(component or "").strip()))


def _choose_semantic_rail(
    component: str,
    original_rail: str,
    component_rails: Dict[str, List[str]],
    power_rails: set[str],
) -> Tuple[str, bool, str, str, bool]:
    normalized_component = str(component or "").strip().upper()
    normalized_rail = str(original_rail or "").strip()
    if not normalized_component or not _is_rlc_component(normalized_component):
        return normalized_rail, False, "not_rlc", "low", False
    if normalized_rail and not _is_ground_rail(normalized_rail):
        return normalized_rail, False, "kept_non_ground", "high", False
    candidates = [str(item).strip() for item in (component_rails.get(normalized_component) or []) if str(item).strip()]
    non_ground = [item for item in candidates if not _is_ground_rail(item)]
    if not non_ground:
        return normalized_rail, False, "ground_like_without_non_ground_candidate", "low", True
    power_candidates = [item for item in non_ground if item in power_rails]
    selected = ""
    confidence = "medium"
    reason = "rebind_non_ground_candidate"
    if normalized_component.startswith("C"):
        # Capacitor preferred pattern: power rail paired with ground return.
        if power_candidates:
            selected = power_candidates[0]
            reason = "capacitor_power_ground_pair"
            confidence = "high"
        else:
            selected = non_ground[0]
            reason = "capacitor_non_ground_fallback"
    else:
        if power_candidates:
            selected = power_candidates[0]
            reason = "rlc_power_domain_candidate"
            confidence = "high"
        else:
            selected = non_ground[0]
            reason = "rlc_non_ground_fallback"
    return selected, True, reason, confidence, False


def _resolve_path(value: Any) -> Path | None:
    if not value:
        return None
    p = Path(str(value).replace("\\", "/"))
    if p.is_absolute():
        return p
    return (PROJECT_ROOT / p).resolve()


def _resolve_with_evaltest_alias(value: Any) -> Path | None:
    path = _resolve_path(value)
    if not path or path.exists():
        return path
    parts = list(path.parts)
    idx = next((i for i, part in enumerate(parts) if str(part).lower() == "evaluation_test"), -1)
    if idx < 0 or idx + 2 >= len(parts):
        return path
    board = str(parts[idx + 1])
    case_name = str(parts[idx + 2])
    short_case = re.match(r"^\d+_(\d+)$", case_name)
    if not short_case:
        return path
    alias_parts = list(parts)
    alias_parts[idx + 2] = f"{board}_{short_case.group(1)}"
    alias = Path(*alias_parts)
    if alias.exists():
        return alias
    alias_parent = alias.parent
    if alias_parent.exists():
        modified_tag = re.search(r"(modified\d+)(\.[A-Za-z0-9]+)$", path.name, flags=re.IGNORECASE)
        if modified_tag:
            pattern = f"*{modified_tag.group(1)}{modified_tag.group(2)}"
            candidates = sorted(alias_parent.glob(pattern))
            if candidates:
                return candidates[0]
        ext_candidates = sorted(alias_parent.glob(f"*{path.suffix}")) if path.suffix else []
        if ext_candidates:
            return ext_candidates[0]
    return path


def _derive_case_identity(report_path: Path, report_payload: Dict[str, Any]) -> Tuple[str, str]:
    ipc = str(((report_payload.get("inputs") or {}).get("ipc_xml") or ""))
    ipc_path = _resolve_path(ipc)
    if ipc_path and "evaluation_test" in [part.lower() for part in ipc_path.parts]:
        parts = list(ipc_path.parts)
        idx = next((i for i, part in enumerate(parts) if part.lower() == "evaluation_test"), -1)
        if idx >= 0 and idx + 2 < len(parts):
            board_name = parts[idx + 1]
            case_name = parts[idx + 2]
            return board_name, case_name
    name = report_path.parent.name
    low = name.lower()
    if "hi3519" in low:
        return "Hi3519", "Hi3519_1" if "modified1" in low else ("Hi3519_2" if "modified2" in low else "unknown")
    if "hub" in low:
        return "HUB", "HUB_1" if "modified1" in low else ("HUB_2" if "modified2" in low else "unknown")
    if "t1" in low:
        if "modified1" in low:
            return "t1", "t1_1"
        if "modified2" in low:
            return "t1", "t1_2"
        if "modified3" in low:
            return "t1", "t1_3"
    if "t2" in low:
        if "modified1" in low:
            return "t2", "t2_1"
        if "modified2" in low:
            return "t2", "t2_2"
        if "modified3" in low:
            return "t2", "t2_3"
    if "t3" in low:
        return "t3", "t3_1"
    return "unknown", "unknown"


def _extract_relaxed_component_labels(docx_path: Path) -> List[Dict[str, Any]]:
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(docx_path, "r") as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    labels: List[Dict[str, Any]] = []
    idx = 0
    for para in root.findall(".//w:p", ns):
        text = "".join((node.text or "") for node in para.findall(".//w:t", ns)).strip()
        if not text:
            continue
        if not re.match(r"^\s*\d+[\.\、\)]\s*", text):
            continue
        refs = []
        for item in re.findall(r"([CRL]\d{1,5})", text, flags=re.IGNORECASE):
            token = item.upper()
            if token not in refs:
                refs.append(token)
        for ref_idx, ref in enumerate(refs, start=1):
            idx += 1
            labels.append(
                {
                    "id": f"RLX{idx}_{ref_idx}",
                    "description": text,
                    "component": ref,
                    "rail": "",
                    "group": text,
                    "expected": True,
                }
            )
    return labels


def _extract_relaxed_labels_v2(docx_path: Path) -> List[Dict[str, Any]]:
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(docx_path, "r") as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    labels: List[Dict[str, Any]] = []
    idx = 0
    rail_like = re.compile(
        r"(\+[0-9]+V[0-9A-Z_\.-]*|[0-9]+(?:\.[0-9]+)?V[_A-Z0-9/\.-]*|VIN|VOUT|AVDD|DVDD|VDD[A-Z0-9_\.-]*|VCC[A-Z0-9_\.-]*)",
        re.IGNORECASE,
    )
    for para in root.findall(".//w:p", ns):
        text = "".join((node.text or "") for node in para.findall(".//w:t", ns)).strip()
        if not text:
            continue
        if not re.match(r"^\s*\d+[\.、\)]\s*", text):
            continue
        refs: List[str] = []
        for item in re.findall(r"([CRLUQD]\d{1,5})", text, flags=re.IGNORECASE):
            token = item.upper()
            if token not in refs:
                refs.append(token)
        rails: List[str] = []
        for item in rail_like.findall(text):
            token = str(item).replace(" ", "").upper()
            if token and token not in rails:
                rails.append(token)
        for ref_idx, ref in enumerate(refs, start=1):
            idx += 1
            labels.append(
                {
                    "id": f"RLX2_{idx}_{ref_idx}",
                    "description": text,
                    "component": ref,
                    "rail": "",
                    "group": text,
                    "expected": True,
                }
            )
        if not refs and rails:
            for rail_idx, rail in enumerate(rails, start=1):
                idx += 1
                labels.append(
                    {
                        "id": f"RLX2_RAIL_{idx}_{rail_idx}",
                        "description": text,
                        "component": "",
                        "rail": rail,
                        "group": text,
                        "expected": True,
                    }
                )
    return labels


def _load_labels_from_docx(board: str, case_name: str, labels_dir: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    case_dir = PROJECT_ROOT / "data" / "evaluation_test" / board / case_name
    if not case_dir.exists():
        alias_candidates: List[str] = [f"{board}_{case_name}"]
        short_case = re.match(r"^\d+_(\d+)$", case_name)
        if short_case:
            alias_candidates.insert(0, f"{board}_{short_case.group(1)}")
        for alias in alias_candidates:
            alias_dir = PROJECT_ROOT / "data" / "evaluation_test" / board / alias
            if alias_dir.exists():
                case_dir = alias_dir
                break
    docx_files = sorted(case_dir.glob("*.docx"))
    if not docx_files:
        return [], {"docx_found": False, "docx_path": None, "warnings": [], "errors": [f"No docx found under {case_dir.as_posix()}"]}
    docx_path = docx_files[0]
    hint_board = board
    if board.lower() == "hi3519":
        hint_board = "Hi3519_2" if "2" in case_name else "Hi3519_1"
    parsed = parse_docx_labels_with_fallback(docx_path, hint_board)
    labels = [item for item in parsed.labels if bool(item.get("expected", True))]
    if not labels:
        labels = _extract_relaxed_labels_v2(docx_path)
        if labels:
            parsed.warnings.append("Using relaxed paragraph extraction v2 (component/rail hybrid); missing rail/component will be inferred from net connections.")
    labels_dir.mkdir(parents=True, exist_ok=True)
    output_json = labels_dir / f"{board}_{case_name}_labels.json"
    payload = {
        "meta": {
            "board": board,
            "case": case_name,
            "source_docx": docx_path.as_posix(),
            "schema_version": 1,
            "extracted_at": datetime.utcnow().isoformat() + "Z",
        },
        "labels": labels,
        "quality": {
            "row_count": len(labels),
            "parse_warnings": parsed.warnings,
            "parse_errors": parsed.errors,
        },
    }
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return labels, {
        "docx_found": True,
        "docx_path": docx_path.as_posix(),
        "labels_json": output_json.as_posix(),
        "warnings": parsed.warnings,
        "errors": parsed.errors,
    }


def _evaluate_case(
    report_path: Path,
    report_payload: Dict[str, Any],
    labels: List[Dict[str, Any]],
    cache_dir: Path,
) -> Dict[str, Any]:
    inputs = report_payload.get("inputs") or {}
    datastruct_json = _resolve_with_evaltest_alias(inputs.get("datastruct_json"))
    ipc_xml = _resolve_with_evaltest_alias(inputs.get("ipc_xml"))
    run_output_dir = cache_dir / "runtime" / report_path.parent.name
    if not ipc_xml or not ipc_xml.exists():
        raise FileNotFoundError(f"ipc_xml missing: {ipc_xml}")
    netlist_candidates = sorted(report_path.parent.glob("*_real_numeric_netlist.json"))
    if netlist_candidates:
        state = _build_state_from_netlist(netlist_candidates[0])
        ipc_info = _attach_routing_metrics_from_ipc(state, ipc_xml)
        datastruct_info = {
            "enabled": bool(datastruct_json and datastruct_json.exists()),
            "datastruct_path": datastruct_json.as_posix() if datastruct_json else None,
            "positions_loaded": 0,
            "placements_replaced": 0,
        }
        local_doc_info = {"fast_mode": True, "source": "report_netlist"}
    else:
        board_dir = _resolve_with_evaltest_alias(inputs.get("board_dir"))
        if not board_dir or not board_dir.exists():
            raise FileNotFoundError(f"board_dir missing and no netlist in report dir: {board_dir}")
        state, datastruct_info, ipc_info, local_doc_info = _build_state_from_board(
            board_dir,
            datastruct_json,
            ipc_xml,
            run_output_dir,
            datasheet_use_llm=False,
        )
    full_net_conn = _extract_net_connections(state)
    component_rails: Dict[str, List[str]] = {}
    for rail_name, conns in full_net_conn.items():
        for conn in conns:
            if not isinstance(conn, str) or "." not in conn:
                continue
            ref = conn.split(".", 1)[0].upper()
            component_rails.setdefault(ref, [])
            if rail_name not in component_rails[ref]:
                component_rails[ref].append(rail_name)
    power_rails = {str(item) for item in (state.power_domains or {}).keys()}

    normalized_labels: List[Dict[str, Any]] = []
    for label in labels:
        row = dict(label)
        rail = str(row.get("rail") or "").strip()
        component = str(row.get("component") or "").strip().upper()
        original_rail = rail
        rail_inferred_from_component = False
        if not rail and component:
            candidates = component_rails.get(component, [])
            preferred = [item for item in candidates if item in power_rails]
            rail = (preferred or candidates or [""])[0]
            rail_inferred_from_component = bool(rail)
        if rail and not component:
            inferred_component = ""
            for conn in full_net_conn.get(rail, []):
                if not isinstance(conn, str) or "." not in conn:
                    continue
                ref = conn.split(".", 1)[0].upper()
                if re.match(r"^[CRL]\d+$", ref):
                    inferred_component = ref
                    break
                if not inferred_component:
                    inferred_component = ref
            component = inferred_component
        if not rail or not component:
            continue
        normalized_rail, rebound, reason, confidence, unresolved = _choose_semantic_rail(
            component=component,
            original_rail=rail,
            component_rails=component_rails,
            power_rails=power_rails,
        )
        row["component"] = component
        row["original_rail"] = original_rail
        row["normalized_rail"] = normalized_rail
        row["rail"] = normalized_rail or rail
        row["rail_semantic_rebind"] = bool(rebound)
        row["normalization_reason"] = reason
        row["normalization_confidence"] = confidence
        if rail_inferred_from_component and row["rail"] not in power_rails:
            unresolved = True
            row["normalization_reason"] = "inferred_non_power_candidate"
            row["normalization_confidence"] = "low"
        elif rail_inferred_from_component and row["rail"] in power_rails and row["normalization_reason"] == "kept_non_ground":
            candidates = component_rails.get(component, [])
            power_candidates = [item for item in candidates if item in power_rails]
            has_ground_candidate = any(_is_ground_rail(item) for item in candidates)
            if len(power_candidates) == 1 and has_ground_candidate:
                row["normalization_reason"] = "inferred_power_candidate"
                row["normalization_confidence"] = "medium"
            else:
                unresolved = True
                row["normalization_reason"] = "inferred_power_ambiguous"
                row["normalization_confidence"] = "low"
        row["unresolved_semantic_label"] = bool(unresolved)
        row["rlc_component"] = _is_rlc_component(component)
        row["rlc_power_scope"] = bool(row["rlc_component"] and (not _is_ground_rail(row["rail"])) and (not unresolved))
        normalized_labels.append(row)
    labels = normalized_labels
    target_rails = sorted({str(item.get("rail") or "") for item in labels if str(item.get("rail") or "").strip()})
    _restrict_state_to_target_rails(state, target_rails)

    positions = _get_positions(state)
    net_conn = _extract_net_connections(state)
    resolver = DeviceLibraryResolver(cache_dir=str(cache_dir / "component_cache"))
    try:
        component_knowledge = build_component_knowledge_map(state, net_conn, resolver=resolver)
        comp_attrs = _enrich_component_attrs(state, component_knowledge=component_knowledge)
        path_a = _path_a(state, positions, net_conn, comp_attrs)
        path_b = _path_b(state, positions, net_conn, comp_attrs)
        path_c = _path_c(state, positions, net_conn, comp_attrs, component_knowledge=component_knowledge)
    finally:
        resolver.close()

    details: List[Dict[str, Any]] = []
    for label in labels:
        component = str(label.get("component") or "")
        rail = str(label.get("rail") or "")
        if not component or not rail:
            continue
        a_evidence = _find_path_a_evidence(((path_a.get("rails") or {}).get(rail) or {}), component)
        b_evidence = _find_path_b_evidence(((path_b.get("rails") or {}).get(rail) or {}), component)
        c_evidence = _find_path_c_evidence(path_c, rail, component)
        details.append(
            {
                "id": label.get("id"),
                "description": label.get("description"),
                "component": component,
                "rail": rail,
                "original_rail": label.get("original_rail"),
                "normalized_rail": label.get("normalized_rail") or rail,
                "rail_semantic_rebind": bool(label.get("rail_semantic_rebind")),
                "normalization_reason": label.get("normalization_reason") or "",
                "normalization_confidence": label.get("normalization_confidence") or "low",
                "unresolved_semantic_label": bool(label.get("unresolved_semantic_label")),
                "rlc_component": bool(label.get("rlc_component")),
                "rlc_power_scope": bool(label.get("rlc_power_scope")),
                "group": label.get("group") or label.get("description") or "",
                "path_a": a_evidence,
                "path_b": b_evidence,
                "path_c": c_evidence,
                "abc_any_hit": bool(a_evidence.get("hit") or b_evidence.get("hit") or c_evidence.get("hit")),
            }
        )

    def _recall(key: str) -> Dict[str, Any]:
        hits = sum(1 for item in details if bool((item.get(key) or {}).get("hit")))
        total = len(details)
        return {"hits": hits, "total": total, "recall": (hits / total) if total else 0.0}

    def _filtered_any(predicate) -> Dict[str, Any]:
        subset = [item for item in details if predicate(item)]
        hits = sum(1 for item in subset if bool(item.get("abc_any_hit")))
        total = len(subset)
        return {"hits": hits, "total": total, "recall": (hits / total) if total else 0.0}

    any_hits = sum(1 for item in details if bool(item.get("abc_any_hit")))
    rlc_all = _filtered_any(lambda item: bool(item.get("rlc_component")))
    rlc_power = _filtered_any(lambda item: bool(item.get("rlc_power_scope")))
    rlc_power_strict = _filtered_any(lambda item: bool(item.get("rlc_power_scope")) and str(item.get("normalization_confidence") or "").lower() == "high")
    rlc_power_medium = _filtered_any(lambda item: bool(item.get("rlc_power_scope")) and str(item.get("normalization_confidence") or "").lower() == "medium")
    unresolved_rows = [item for item in details if bool(item.get("unresolved_semantic_label")) and bool(item.get("rlc_component"))]
    missed_rows = [item for item in details if (not bool(item.get("abc_any_hit"))) and (not bool(item.get("unresolved_semantic_label")))]
    matched_rows = [item for item in details if bool(item.get("abc_any_hit"))]
    target_threshold = 0.75
    rlc_power_gate = rlc_power["recall"] >= target_threshold if rlc_power["total"] else False
    return {
        "report_dir": report_path.parent.as_posix(),
        "path_a_recall": _recall("path_a"),
        "path_b_recall": _recall("path_b"),
        "path_c_recall": _recall("path_c"),
        "abc_any_recall": {"hits": any_hits, "total": len(details), "recall": any_hits / max(1, len(details))},
        "rlc_all_recall": rlc_all,
        "rlc_power_recall": rlc_power,
        "rlc_power_strict_recall": rlc_power_strict,
        "rlc_power_medium_recall": rlc_power_medium,
        "quality": {
            "target_threshold": target_threshold,
            "target_passed": bool(rlc_power_gate),
            "unresolved_count": len(unresolved_rows),
            "unresolved_ratio_vs_rlc_all": (len(unresolved_rows) / max(1, rlc_all["total"])) if rlc_all["total"] else 0.0,
        },
        "review": {
            "matched": matched_rows,
            "missed": missed_rows,
            "unresolved": unresolved_rows,
            "counts": {
                "matched": len(matched_rows),
                "missed": len(missed_rows),
                "unresolved": len(unresolved_rows),
            },
        },
        "details": details,
        "datastruct_info": datastruct_info,
        "ipc_info": ipc_info,
        "local_doc_info": local_doc_info,
    }


def _build_aggregate(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    totals = {
        "cases": len(cases),
        "labels": 0,
        "path_a_hits": 0,
        "path_b_hits": 0,
        "path_c_hits": 0,
        "abc_hits": 0,
        "rlc_all_hits": 0,
        "rlc_all_total": 0,
        "rlc_power_hits": 0,
        "rlc_power_total": 0,
        "rlc_power_strict_hits": 0,
        "rlc_power_strict_total": 0,
        "rlc_power_medium_hits": 0,
        "rlc_power_medium_total": 0,
        "unresolved": 0,
    }
    per_board: Dict[str, Dict[str, float]] = {}
    by_case: Dict[str, Dict[str, Any]] = {}
    for case in cases:
        board = str(case.get("board") or "unknown")
        case_name = str(case.get("case") or "unknown")
        rec = case.get("recall") or {}
        total = int(((rec.get("abc_any_recall") or {}).get("total")) or 0)
        a_hits = int(((rec.get("path_a_recall") or {}).get("hits")) or 0)
        b_hits = int(((rec.get("path_b_recall") or {}).get("hits")) or 0)
        c_hits = int(((rec.get("path_c_recall") or {}).get("hits")) or 0)
        any_hits = int(((rec.get("abc_any_recall") or {}).get("hits")) or 0)
        rlc_all_hits = int(((rec.get("rlc_all_recall") or {}).get("hits")) or 0)
        rlc_all_total = int(((rec.get("rlc_all_recall") or {}).get("total")) or 0)
        rlc_power_hits = int(((rec.get("rlc_power_recall") or {}).get("hits")) or 0)
        rlc_power_total = int(((rec.get("rlc_power_recall") or {}).get("total")) or 0)
        rlc_power_strict_hits = int(((rec.get("rlc_power_strict_recall") or {}).get("hits")) or 0)
        rlc_power_strict_total = int(((rec.get("rlc_power_strict_recall") or {}).get("total")) or 0)
        rlc_power_medium_hits = int(((rec.get("rlc_power_medium_recall") or {}).get("hits")) or 0)
        rlc_power_medium_total = int(((rec.get("rlc_power_medium_recall") or {}).get("total")) or 0)
        unresolved_count = int(((rec.get("quality") or {}).get("unresolved_count")) or 0)
        totals["labels"] += total
        totals["path_a_hits"] += a_hits
        totals["path_b_hits"] += b_hits
        totals["path_c_hits"] += c_hits
        totals["abc_hits"] += any_hits
        totals["rlc_all_hits"] += rlc_all_hits
        totals["rlc_all_total"] += rlc_all_total
        totals["rlc_power_hits"] += rlc_power_hits
        totals["rlc_power_total"] += rlc_power_total
        totals["rlc_power_strict_hits"] += rlc_power_strict_hits
        totals["rlc_power_strict_total"] += rlc_power_strict_total
        totals["rlc_power_medium_hits"] += rlc_power_medium_hits
        totals["rlc_power_medium_total"] += rlc_power_medium_total
        totals["unresolved"] += unresolved_count
        bucket = per_board.setdefault(
            board,
            {
                "labels": 0.0,
                "path_a_hits": 0.0,
                "path_b_hits": 0.0,
                "path_c_hits": 0.0,
                "abc_hits": 0.0,
                "rlc_all_hits": 0.0,
                "rlc_all_total": 0.0,
                "rlc_power_hits": 0.0,
                "rlc_power_total": 0.0,
                "rlc_power_strict_hits": 0.0,
                "rlc_power_strict_total": 0.0,
                "rlc_power_medium_hits": 0.0,
                "rlc_power_medium_total": 0.0,
                "unresolved": 0.0,
            },
        )
        bucket["labels"] += total
        bucket["path_a_hits"] += a_hits
        bucket["path_b_hits"] += b_hits
        bucket["path_c_hits"] += c_hits
        bucket["abc_hits"] += any_hits
        bucket["rlc_all_hits"] += rlc_all_hits
        bucket["rlc_all_total"] += rlc_all_total
        bucket["rlc_power_hits"] += rlc_power_hits
        bucket["rlc_power_total"] += rlc_power_total
        bucket["rlc_power_strict_hits"] += rlc_power_strict_hits
        bucket["rlc_power_strict_total"] += rlc_power_strict_total
        bucket["rlc_power_medium_hits"] += rlc_power_medium_hits
        bucket["rlc_power_medium_total"] += rlc_power_medium_total
        bucket["unresolved"] += unresolved_count
        by_case[f"{board}:{case_name}"] = {
            "board": board,
            "case": case_name,
            "path_a_recall": (a_hits / total) if total else 0.0,
            "path_b_recall": (b_hits / total) if total else 0.0,
            "path_c_recall": (c_hits / total) if total else 0.0,
            "abc_any_recall": (any_hits / total) if total else 0.0,
            "rlc_all_recall": (rlc_all_hits / rlc_all_total) if rlc_all_total else 0.0,
            "rlc_power_recall": (rlc_power_hits / rlc_power_total) if rlc_power_total else 0.0,
            "rlc_power_strict_recall": (rlc_power_strict_hits / rlc_power_strict_total) if rlc_power_strict_total else 0.0,
            "rlc_power_medium_recall": (rlc_power_medium_hits / rlc_power_medium_total) if rlc_power_medium_total else 0.0,
            "unresolved_labels": unresolved_count,
        }

    def _ratio(num: float, den: float) -> float:
        return num / den if den else 0.0

    aggregate_boards: Dict[str, Any] = {}
    for board, row in per_board.items():
        aggregate_boards[board] = {
            "labels": int(row["labels"]),
            "path_a_recall": _ratio(row["path_a_hits"], row["labels"]),
            "path_b_recall": _ratio(row["path_b_hits"], row["labels"]),
            "path_c_recall": _ratio(row["path_c_hits"], row["labels"]),
            "abc_any_recall": _ratio(row["abc_hits"], row["labels"]),
            "rlc_all_recall": _ratio(row["rlc_all_hits"], row["rlc_all_total"]),
            "rlc_power_recall": _ratio(row["rlc_power_hits"], row["rlc_power_total"]),
            "rlc_power_strict_recall": _ratio(row["rlc_power_strict_hits"], row["rlc_power_strict_total"]),
            "rlc_power_medium_recall": _ratio(row["rlc_power_medium_hits"], row["rlc_power_medium_total"]),
            "unresolved_labels": int(row["unresolved"]),
        }
    return {
        "overall": {
            "cases": totals["cases"],
            "labels": totals["labels"],
            "path_a_recall": _ratio(totals["path_a_hits"], totals["labels"]),
            "path_b_recall": _ratio(totals["path_b_hits"], totals["labels"]),
            "path_c_recall": _ratio(totals["path_c_hits"], totals["labels"]),
            "abc_any_recall": _ratio(totals["abc_hits"], totals["labels"]),
            "rlc_all_recall": _ratio(totals["rlc_all_hits"], totals["rlc_all_total"]),
            "rlc_power_recall": _ratio(totals["rlc_power_hits"], totals["rlc_power_total"]),
            "rlc_power_strict_recall": _ratio(totals["rlc_power_strict_hits"], totals["rlc_power_strict_total"]),
            "rlc_power_medium_recall": _ratio(totals["rlc_power_medium_hits"], totals["rlc_power_medium_total"]),
            "unresolved_labels": totals["unresolved"],
            "target_threshold": 0.75,
            "target_passed": _ratio(totals["rlc_power_hits"], totals["rlc_power_total"]) >= 0.75 if totals["rlc_power_total"] else False,
        },
        "by_board": aggregate_boards,
        "by_case": by_case,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run recall benchmark for all archived evaluation test boards.")
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "outputs" / "archive" / "evaluation" / "20260328_232730_v20260326a_pre_batch_rerun_20260328_232719",
    )
    parser.add_argument(
        "--labels-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "outputs" / "benchmark" / "labels",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "outputs" / "benchmark" / "archived_recall_runtime",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "data" / "outputs" / "benchmark" / "archived_testboard_recall_20260329.json",
    )
    args = parser.parse_args()

    reports = sorted(args.archive_root.rglob("numeric_eval_report.json"))
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.labels_dir.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []

    for report_path in reports:
        payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
        board, case_name = _derive_case_identity(report_path, payload)
        labels, label_meta = _load_labels_from_docx(board, case_name, args.labels_dir)
        if not labels:
            rows.append(
                {
                    "board": board,
                    "case": case_name,
                    "report_path": report_path.as_posix(),
                    "status": "skipped",
                    "reason": "no_labels",
                    "label_meta": label_meta,
                }
            )
            continue
        try:
            recall = _evaluate_case(report_path, payload, labels, args.cache_dir)
            rows.append(
                {
                    "board": board,
                    "case": case_name,
                    "report_path": report_path.as_posix(),
                    "status": "ok",
                    "label_count": len(labels),
                    "label_meta": label_meta,
                    "recall": recall,
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "board": board,
                    "case": case_name,
                    "report_path": report_path.as_posix(),
                    "status": "error",
                    "label_count": len(labels),
                    "label_meta": label_meta,
                    "error": str(exc),
                }
            )

    successful = [row for row in rows if row.get("status") == "ok"]
    report = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "archive_root": args.archive_root.as_posix(),
        "labels_dir": args.labels_dir.as_posix(),
        "case_count": len(rows),
        "successful_cases": len(successful),
        "aggregate": _build_aggregate(successful),
        "cases": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output.as_posix())
    print(json.dumps(report["aggregate"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
