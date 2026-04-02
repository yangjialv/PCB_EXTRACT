from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import math
import mimetypes
import re
import sys
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from urllib.parse import quote

from plagent.backend.knowledge.spec_assets import (
    DEFAULT_SPEC_ASSET_ROOT,
    compile_spec_asset_pi_requirements,
    load_spec_assets_for_parts,
    normalize_part_key,
)
from plagent.backend.utils import pi_requirements as pi_req


FRONTEND_STATIC_DIR = Path(__file__).with_name("static")
DEFAULT_REPORT_ROOT = Path("data") / "outputs" / "evaluation"
BENCHMARK_ROOT = Path("data") / "outputs" / "benchmark"
_REPORT_CACHE: Dict[str, Tuple[int, LoadedReport]] = {}
_SUMMARY_CACHE: Dict[str, Tuple[int, Tuple[Tuple[str, int], ...], Tuple[Tuple[str, int], ...], Dict[str, Any]]] = {}
_RAIL_DETAIL_CACHE: Dict[Tuple[str, str], Tuple[int, Tuple[Tuple[str, int], ...], Dict[str, Any]]] = {}
_SPEC_REQUIREMENT_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}
_IPC_CACHE: Dict[str, Tuple[int, Dict[str, Any]]] = {}
_LAYOUT_CACHE: Dict[str, Tuple[int, Dict[str, Any]]] = {}
_RERUN_STATE_CACHE: Dict[str, Tuple[int, Any]] = {}
_RERUN_RAIL_SUBGRAPH_CACHE: Dict[Tuple[str, int, str], Any] = {}
_BENCHMARK_CACHE: Dict[str, Tuple[Tuple[Tuple[str, int], ...], Dict[str, List[Dict[str, Any]]], List[str]]] = {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _safe_path(path_value: Any) -> str:
    try:
        path = Path(str(path_value))
    except Exception:
        return str(path_value or "")
    try:
        return path.resolve().as_posix()
    except Exception:
        return path.as_posix()


def _rotate_point(x: float, y: float, rotation_deg: float) -> Tuple[float, float]:
    radians = math.radians(rotation_deg)
    cos_v = math.cos(radians)
    sin_v = math.sin(radians)
    return (x * cos_v - y * sin_v, x * sin_v + y * cos_v)


def _load_ipc_parser():
    module_path = Path(__file__).resolve().parents[2] / "tools" / "ipc2581_eval_render.py"
    module_name = "plagent_ipc2581_eval_render"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if not spec or not spec.loader:
        raise ImportError(f"Unable to load IPC parser from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _slug(text: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in text).strip("-") or "unknown"


def _normalize_token(text: Any) -> str:
    return str(text or "").strip().strip("'").strip('"').strip().lower()


def _canonical_rail_name(rail_name: Any) -> str:
    return str(rail_name or "").strip().strip("'").strip('"').strip()


def _normalize_rail_keyed_map(mapping: Dict[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for key, value in (mapping or {}).items():
        normalized[_canonical_rail_name(key)] = value
    return normalized


def _infer_voltage_from_rail_name(rail_name: str) -> Optional[float]:
    raw = _canonical_rail_name(rail_name).upper().strip()
    spaced = raw.replace("-", "_").replace(" ", "_")
    underscore = re.search(r"(\d+)[_\.](\d+)V\b", spaced)
    if underscore:
        try:
            return float(f"{int(underscore.group(1))}.{underscore.group(2)}")
        except ValueError:
            return None
    compact = re.sub(r"[^A-Z0-9\.]", "", raw)
    patterns = [
        r"(\d+)V(\d+)",
        r"(\d+(?:\.\d+)?)V\b",
        r"V(\d+)(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact)
        if not match:
            continue
        if len(match.groups()) == 2:
            try:
                return float(f"{int(match.group(1))}.{match.group(2)}")
            except ValueError:
                continue
        try:
            return float(match.group(1))
        except ValueError:
            continue
    direct = re.search(r"(\d+(?:\.\d+)?)V", raw)
    if direct:
        try:
            return float(direct.group(1))
        except ValueError:
            return None
    return None


def _voltage_matches_rail_hint(voltage_v: Optional[float], rail_voltage_hint: Optional[float]) -> bool:
    if rail_voltage_hint is None or voltage_v is None:
        return True
    try:
        voltage = float(voltage_v)
        hint = float(rail_voltage_hint)
    except (TypeError, ValueError):
        return True
    if not math.isfinite(voltage) or not math.isfinite(hint):
        return True
    if hint <= 1.2:
        tolerance = 0.03
    elif hint <= 2.5:
        tolerance = 0.05
    else:
        tolerance = 0.10
    return abs(voltage - hint) <= tolerance


def _effective_rail_voltage_hint(rail_name: str, rail_payload: Dict[str, Any]) -> Optional[float]:
    selected_requirements = rail_payload.get("selected_requirements") or {}
    inferred_from_name = _infer_voltage_from_rail_name(rail_name)
    candidates = [
        selected_requirements.get("voltage"),
        (selected_requirements.get("voltage_selection") or {}).get("selected_value"),
        (selected_requirements.get("voltage_selection") or {}).get("explicit_value"),
        inferred_from_name,
    ]
    for candidate in candidates:
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(value) and value > 0):
            continue
        if inferred_from_name is not None and not _values_close(value, inferred_from_name, rel_tol=0.02, abs_tol=0.03):
            if candidate is not inferred_from_name:
                continue
        return value
    return None


def _name_tokens(text: Any) -> List[str]:
    raw = _canonical_rail_name(text).upper()
    tokens = [token for token in re.findall(r"[A-Z]+|\d+(?:\.\d+)?", raw) if token]
    return tokens


def _semantic_name_tokens(text: Any) -> List[str]:
    generic_tokens = {"V", "VDD", "VCC", "AVDD", "DVDD", "VIN", "VOUT", "REG", "OUT", "IO", "DDRIO"}
    semantic = []
    for token in _name_tokens(text):
        if token in generic_tokens:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", token):
            continue
        semantic.append(token)
    return semantic


def _supply_match_confidence(rail_name: str, source_supply_name: str, matched_supply_name: str) -> str:
    rail_tokens = set(_semantic_name_tokens(rail_name))
    supply_tokens = set(_semantic_name_tokens(source_supply_name or matched_supply_name))
    exact_match = _normalize_token(source_supply_name) == _normalize_token(matched_supply_name)
    if rail_tokens:
        if supply_tokens and supply_tokens.issubset(rail_tokens):
            return "high" if exact_match else "medium"
        return "low"
    if exact_match and supply_tokens:
        return "high"
    if exact_match:
        return "medium"
    return "low"


def _split_pointer(pointer: str) -> List[str]:
    if not pointer or pointer == "/":
        return []
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/") if part]


def _resolve_pointer(payload: Any, pointer: str) -> Any:
    current = payload
    for segment in _split_pointer(pointer):
        if isinstance(current, list):
            current = current[int(segment)]
        else:
            current = current[segment]
    return current


def _describe_node(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return {"type": "object", "size": len(value), "preview": list(value.keys())[:8]}
    if isinstance(value, list):
        return {"type": "array", "size": len(value), "preview": value[:3]}
    return {"type": type(value).__name__, "value": value}


def _child_pointer(parent: str, segment: str) -> str:
    escaped = segment.replace("~", "~0").replace("/", "~1")
    return f"{parent}/{escaped}" if parent else f"/{escaped}"


def _search_json(value: Any, query: str, pointer: str = "", matches: Optional[List[Dict[str, Any]]] = None, limit: int = 120) -> List[Dict[str, Any]]:
    matches = matches or []
    if len(matches) >= limit:
        return matches
    normalized_query = query.lower()
    descriptor = _describe_node(value)
    haystacks = [pointer.lower()]
    if descriptor["type"] not in {"object", "array"}:
        haystacks.append(str(descriptor.get("value")).lower())
    if any(normalized_query in item for item in haystacks if item):
        matches.append({"pointer": pointer or "/", **descriptor})
        if len(matches) >= limit:
            return matches
    if isinstance(value, dict):
        for key, child in value.items():
            _search_json(child, query, _child_pointer(pointer, str(key)), matches, limit)
            if len(matches) >= limit:
                break
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            _search_json(child, query, _child_pointer(pointer, str(idx)), matches, limit)
            if len(matches) >= limit:
                break
    return matches


def _read_raw_report(report_target: Path) -> Dict[str, Any]:
    return load_report(report_target).payload


@dataclass
class LoadedReport:
    report_path: Path
    report_dir: Path
    payload: Dict[str, Any]


def find_report_file(target: Path) -> Path:
    if target.is_file():
        return target

    direct = target / "numeric_eval_report.json"
    if direct.exists():
        return direct

    candidates = sorted(target.rglob("numeric_eval_report.json"))
    if not candidates:
        raise FileNotFoundError(f"No numeric_eval_report.json found under {target}")
    return candidates[0]


def list_report_files(target: Path) -> List[Path]:
    if target.is_file():
        return [target]
    direct = target / "numeric_eval_report.json"
    if direct.exists():
        return [direct]
    return sorted(target.rglob("numeric_eval_report.json"))


def _report_listing_entry(base_target: Path, path: Path) -> Dict[str, Any]:
    relative = path.relative_to(base_target) if base_target.is_dir() else Path(path.name)
    parts = list(relative.parts[:-1])
    group = parts[0] if parts else "default"
    family = parts[1] if len(parts) > 1 else path.parent.name
    board_name = parts[-1] if parts else path.parent.name
    label = " / ".join(parts) if parts else path.parent.name

    status = "-"
    runtime_seconds = 0.0
    timestamp = ""
    try:
        payload = load_report(path).payload
        summary = payload.get("summary") or {}
        runtime = payload.get("experiment_runtime") or {}
        status = str((payload.get("pdn_evaluation") or {}).get("status") or summary.get("status") or "-")
        runtime_seconds = _safe_float(runtime.get("duration_seconds"), 0.0)
        timestamp = str(payload.get("timestamp") or "")
    except Exception:
        pass

    return {
        "id": relative.as_posix(),
        "label": label,
        "path": path.as_posix(),
        "group": group,
        "family": family,
        "board": board_name,
        "status": status,
        "runtime_seconds": runtime_seconds,
        "timestamp": timestamp,
    }


def resolve_report_selection(base_target: Path, selection: str | None) -> Path:
    if not selection:
        return find_report_file(base_target)
    base_dir = base_target.parent if base_target.is_file() else base_target
    candidate = Path(selection)
    if candidate.is_absolute() and candidate.exists():
        return find_report_file(candidate)
    candidate = (base_dir / selection).resolve()
    if candidate.exists():
        return find_report_file(candidate)
    cwd_candidate = (Path.cwd() / selection).resolve()
    if cwd_candidate.exists():
        return find_report_file(cwd_candidate)
    raise FileNotFoundError(f"Unknown report selection: {selection}")


def load_report(target: Path) -> LoadedReport:
    report_file = find_report_file(target)
    stat_key = report_file.as_posix()
    mtime = report_file.stat().st_mtime_ns
    cached = _REPORT_CACHE.get(stat_key)
    if cached and cached[0] == mtime:
        return cached[1]
    payload = json.loads(report_file.read_text(encoding="utf-8-sig"))
    loaded = LoadedReport(report_path=report_file, report_dir=report_file.parent, payload=payload)
    _REPORT_CACHE[stat_key] = (mtime, loaded)
    return loaded


def _revisions_root(report_dir: Path) -> Path:
    return report_dir / "revisions" / "rails"


def _revision_signature(report_dir: Path) -> Tuple[Tuple[str, int], ...]:
    root = _revisions_root(report_dir)
    if not root.exists():
        return ()
    files = sorted(path for path in root.rglob("*.json") if path.is_file())
    return tuple((path.as_posix(), path.stat().st_mtime_ns) for path in files)


def _rail_revision_dir(report_dir: Path, rail_name: str) -> Path:
    return _revisions_root(report_dir) / _slug(rail_name)


def _revision_index_path(report_dir: Path, rail_name: str) -> Path:
    return _rail_revision_dir(report_dir, rail_name) / "index.json"


def _revision_payload_path(report_dir: Path, rail_name: str, revision_id: str) -> Path:
    return _rail_revision_dir(report_dir, rail_name) / f"{revision_id}.json"


def _read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_rail_revision_index(report_dir: Path, rail_name: str) -> Dict[str, Any]:
    index_path = _revision_index_path(report_dir, rail_name)
    default = {"rail": rail_name, "active_revision_id": "original", "revisions": []}
    payload = _read_json_file(index_path, default)
    if not isinstance(payload, dict):
        payload = default
    payload["rail"] = rail_name
    payload["active_revision_id"] = str(payload.get("active_revision_id") or "original")
    rows = payload.get("revisions")
    payload["revisions"] = rows if isinstance(rows, list) else []
    return payload


def _save_rail_revision_index(report_dir: Path, rail_name: str, payload: Dict[str, Any]) -> None:
    _write_json_file(_revision_index_path(report_dir, rail_name), payload)


def _load_revision_payload(report_dir: Path, rail_name: str, revision_id: str) -> Optional[Dict[str, Any]]:
    if not revision_id or revision_id == "original":
        return None
    payload = _read_json_file(_revision_payload_path(report_dir, rail_name, revision_id), None)
    return payload if isinstance(payload, dict) else None


def _active_revision_for_rail(report_dir: Path, rail_name: str) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    index_payload = _load_rail_revision_index(report_dir, rail_name)
    active_id = str(index_payload.get("active_revision_id") or "original")
    active_meta = {"revision_id": "original", "kind": "original", "active": active_id == "original"}
    for item in index_payload.get("revisions", []):
        if str(item.get("revision_id") or "") == active_id:
            active_meta = {
                "revision_id": active_id,
                "kind": str(item.get("kind") or "manual"),
                "active": True,
                "created_at": item.get("created_at"),
                "note": item.get("note"),
                "operator": item.get("operator"),
                "parent_revision_id": item.get("parent_revision_id"),
                "algorithm_version": item.get("algorithm_version"),
            }
            break
    return active_meta, _load_revision_payload(report_dir, rail_name, active_id)


def _new_revision_id() -> str:
    return dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]


def _normalize_override_patch(payload: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "voltage": float,
        "imax": float,
        "ripple": float,
        "z_target": float,
        "switching_freq_hz": float,
        "r_eq_ohm": float,
        "l_eq_h": float,
    }
    result: Dict[str, Any] = {}
    for key, caster in allowed.items():
        if key not in payload:
            continue
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            result[key] = caster(value)
        except Exception:
            continue
    result["source"] = "manual"
    return result


def _patch_hash(patch: Dict[str, Any]) -> str:
    blob = json.dumps(patch, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _artifact_url(report_dir: Path, value: Any) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, (dict, list, tuple, set)):
        return None
    raw_text = str(value).strip()
    if not raw_text:
        return None
    artifact_path = _path_from_payload(raw_text)
    if not artifact_path.is_absolute():
        candidates = [
            (Path.cwd() / artifact_path).resolve(),
            (report_dir / artifact_path).resolve(),
            (report_dir / artifact_path.name).resolve(),
        ]
        if artifact_path.parts and artifact_path.parts[0].lower() == "reports":
            candidates.extend(
                [
                    (report_dir.parent / artifact_path.name).resolve(),
                    (report_dir.parent / "/".join(artifact_path.parts[1:])).resolve(),
                    (report_dir / "/".join(artifact_path.parts[-2:])).resolve() if len(artifact_path.parts) >= 2 else report_dir.resolve(),
                ]
            )
        artifact_path = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    return f"/api/file?path={quote(artifact_path.as_posix(), safe='')}"


def _normalize_artifact_value(report_dir: Path, value: Any) -> Any:
    if isinstance(value, dict):
        return {str(sub_key): _normalize_artifact_value(report_dir, sub_value) for sub_key, sub_value in value.items()}
    if isinstance(value, list):
        return [_normalize_artifact_value(report_dir, item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_artifact_value(report_dir, item) for item in value]
    return _artifact_url(report_dir, value)


def _path_from_payload(value: Any) -> Path:
    text = str(value or "").strip()
    if not text:
        return Path()
    normalized = text.replace("\\", "/")
    return Path(normalized)


def _resolve_input_path_for_rerun(report_dir: Path, value: Any) -> Optional[Path]:
    text = str(value or "").strip()
    if not text:
        return None
    path = _path_from_payload(text)
    if path.is_absolute():
        return path if path.exists() else None
    candidates = [
        (Path.cwd() / path).resolve(),
        (report_dir / path).resolve(),
    ]
    return next((item for item in candidates if item.exists()), None)


def _load_numeric_eval_runner():
    module_name = "tools.run_numeric_real_board_eval"
    if module_name in sys.modules:
        return sys.modules[module_name]
    module_path = Path(__file__).resolve().parents[2] / "tools" / "run_numeric_real_board_eval.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if not spec or not spec.loader:
        raise ImportError(f"Unable to load numeric runner from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_state_for_rerun(loaded: LoadedReport):
    stat_key = loaded.report_path.as_posix()
    mtime = loaded.report_path.stat().st_mtime_ns
    cached = _RERUN_STATE_CACHE.get(stat_key)
    if cached and cached[0] == mtime:
        try:
            return copy.deepcopy(cached[1])
        except Exception:
            pass
    report = loaded.payload
    report_dir = loaded.report_dir
    state = None
    try:
        from plagent.backend.main import load_netlist_from_json
        from plagent.backend.models import GlobalState, PlacementResult

        netlist_candidates = sorted(report_dir.glob("*_real_numeric_netlist.json"))
        if netlist_candidates:
            netlist = load_netlist_from_json(str(netlist_candidates[0]))
            placements = {}
            for component in getattr(netlist, "components", []) or []:
                pos = getattr(component, "position", None)
                if isinstance(pos, (tuple, list)) and len(pos) >= 2:
                    placements[str(getattr(component, "ref", ""))] = (float(pos[0]), float(pos[1]))
            state = GlobalState(
                experiment_id=f"viewer_rerun_{report_dir.name}",
                session_id=f"viewer_rerun_{report_dir.name}",
                netlist=netlist,
                components=list(getattr(netlist, "components", []) or []),
                nets=list(getattr(netlist, "nets", []) or []),
            )
            state.placement = PlacementResult(iteration=0, placements=placements)
            path_a_rails = (((report.get("pdn_evaluation") or {}).get("path_a") or {}).get("rails") or {})
            derived_domains: Dict[str, Dict[str, Any]] = {}
            for rail_name, rail_payload in path_a_rails.items():
                if not isinstance(rail_payload, dict):
                    continue
                derived_domains[str(rail_name)] = {
                    "voltage": _safe_float(rail_payload.get("voltage"), 3.3),
                    "imax": _safe_float(rail_payload.get("imax"), 1.0),
                    "ripple": _safe_float(rail_payload.get("ripple"), 0.05),
                    "z_target": _safe_float(rail_payload.get("z_target"), 0.05),
                    "source": "report_cache",
                }
            if not derived_domains:
                summary_rows = ((report.get("summary") or {}).get("critical_rails") or [])
                for row in summary_rows:
                    if not isinstance(row, dict):
                        continue
                    rail_name = str(row.get("rail") or "").strip()
                    if rail_name:
                        derived_domains[rail_name] = {"source": "report_summary"}
            state.power_domains = derived_domains
            state.power_intent = {"power_domains": state.power_domains, "power_requirements": {"rails": copy.deepcopy(state.power_domains)}}
            state.pi_constraint_set = {
                "decap": [
                    {"rail": rail_name, "max_distance_mm": 6.0, "_source_level": "viewer_default"}
                    for rail_name in state.power_domains.keys()
                ]
            }
    except Exception:
        state = None

    if state is None:
        runner = _load_numeric_eval_runner()
        inputs = report.get("inputs") or {}
        board_dir = _resolve_input_path_for_rerun(report_dir, inputs.get("board_dir"))
        if not board_dir:
            raise FileNotFoundError("Missing board_dir for rerun")
        datastruct_json = _resolve_input_path_for_rerun(report_dir, inputs.get("datastruct_json"))
        ipc_xml = _resolve_input_path_for_rerun(report_dir, inputs.get("ipc_xml"))
        tmp_output = report_dir / "revisions" / ".cache_state"
        state, _, _, _ = runner._build_state_from_board(
            board_dir=board_dir,
            datastruct_json=datastruct_json,
            ipc_xml=ipc_xml,
            output_dir=tmp_output,
            datasheet_use_llm=False,
        )
    _RERUN_STATE_CACHE[stat_key] = (mtime, state)
    return copy.deepcopy(state)


def _is_bridge_component(ref: str, component_map: Dict[str, Any]) -> bool:
    comp = component_map.get(ref)
    attrs = (getattr(comp, "attributes", {}) or {}) if comp is not None else {}
    cls = str(attrs.get("class") or attrs.get("component_class") or "").lower()
    ref_upper = str(ref or "").upper()
    if cls in {"inductor", "inductance", "ferrite", "ferrite_bead", "resistance", "resistor"}:
        return True
    return ref_upper.startswith("R") or ref_upper.startswith("L") or ref_upper.startswith("FB")


def _build_rail_subgraph_state(state: Any, rail_name: str) -> Any:
    netlist = getattr(state, "netlist", None)
    metadata = (getattr(netlist, "metadata", {}) or {}) if netlist is not None else {}
    net_conn_raw = metadata.get("net_connections") or {}
    if not isinstance(net_conn_raw, dict):
        return state
    net_conn: Dict[str, List[str]] = {str(k): list(v or []) for k, v in net_conn_raw.items()}
    if rail_name not in net_conn:
        return state

    components = list(getattr(state, "components", []) or [])
    component_map: Dict[str, Any] = {str(getattr(comp, "ref", "")): comp for comp in components if getattr(comp, "ref", None)}
    pins = list(getattr(netlist, "pins", []) or []) if netlist is not None else []
    nets = list(getattr(state, "nets", []) or [])

    ref_to_nets: Dict[str, set[str]] = {}
    for pin in pins:
        ref = str(getattr(pin, "ref", "") or "")
        net_name = str(getattr(pin, "net", "") or "")
        if not ref or not net_name:
            continue
        ref_to_nets.setdefault(ref, set()).add(net_name)

    seed_refs: set[str] = set()
    for conn in net_conn.get(rail_name, []):
        if isinstance(conn, str) and "." in conn:
            seed_refs.add(conn.split(".", 1)[0])

    bridge_refs = {ref for ref in seed_refs if _is_bridge_component(ref, component_map)}
    expanded_nets: set[str] = {rail_name}
    for ref in bridge_refs:
        expanded_nets.update(ref_to_nets.get(ref, set()))

    relevant_refs: set[str] = set()
    for net_name in expanded_nets:
        for conn in net_conn.get(net_name, []):
            if isinstance(conn, str) and "." in conn:
                relevant_refs.add(conn.split(".", 1)[0])

    filtered_components = [comp for comp in components if str(getattr(comp, "ref", "")) in relevant_refs]
    if not filtered_components:
        return state

    filtered_nets = [net for net in nets if str(getattr(net, "name", "")) in expanded_nets]
    filtered_pins = [
        pin
        for pin in pins
        if str(getattr(pin, "ref", "")) in relevant_refs and str(getattr(pin, "net", "")) in expanded_nets
    ]
    filtered_net_conn: Dict[str, List[str]] = {}
    for net_name in expanded_nets:
        values = []
        for conn in net_conn.get(net_name, []):
            if not (isinstance(conn, str) and "." in conn):
                continue
            ref = conn.split(".", 1)[0]
            if ref in relevant_refs:
                values.append(conn)
        if values:
            filtered_net_conn[net_name] = values

    state.components = filtered_components
    state.nets = filtered_nets
    if getattr(state, "placement", None) is not None:
        placements = dict(getattr(state.placement, "placements", {}) or {})
        state.placement.placements = {k: v for k, v in placements.items() if k in relevant_refs}
    if netlist is not None:
        netlist.components = filtered_components
        netlist.nets = filtered_nets
        netlist.pins = filtered_pins
        netlist_metadata = dict(getattr(netlist, "metadata", {}) or {})
        netlist_metadata["net_connections"] = filtered_net_conn
        netlist.metadata = netlist_metadata
    return state


def _load_rail_subgraph_state_for_rerun(loaded: LoadedReport, rail_name: str):
    stat_key = loaded.report_path.as_posix()
    mtime = loaded.report_path.stat().st_mtime_ns
    runner = _load_numeric_eval_runner()
    algorithm_version = str(getattr(runner, "current_algorithm_version", lambda: "unknown")() or "unknown")
    cache_key = (stat_key, mtime, f"{rail_name}:{algorithm_version}")
    cached = _RERUN_RAIL_SUBGRAPH_CACHE.get(cache_key)
    if cached is not None:
        try:
            return copy.deepcopy(cached)
        except Exception:
            pass
    state = _load_state_for_rerun(loaded)
    rail_state = _build_rail_subgraph_state(state, rail_name)
    _RERUN_RAIL_SUBGRAPH_CACHE[cache_key] = rail_state
    return copy.deepcopy(rail_state)


def _restrict_state_for_single_rail(state: Any, rail_name: str, patch: Dict[str, Any]) -> Any:
    rails = state.power_domains or {}
    if rail_name not in rails:
        raise KeyError(f"Rail not found in state: {rail_name}")
    rail_domain = dict(rails.get(rail_name) or {})
    rail_domain.update(patch or {})
    state.power_domains = {rail_name: rail_domain}
    def _update_requirements_container(container: Any) -> Dict[str, Any]:
        raw = container if isinstance(container, dict) else {}
        has_rails_wrapper = isinstance(raw.get("rails"), dict)
        rails_map = dict(raw.get("rails") or {}) if has_rails_wrapper else dict(raw)
        current = rails_map.get(rail_name)
        requirement = current.copy() if isinstance(current, dict) else {}
        requirement.update(patch or {})
        updated_rails_map = {rail_name: requirement}
        if has_rails_wrapper:
            out = dict(raw)
            out["rails"] = updated_rails_map
            return out
        return updated_rails_map

    current_intent = state.power_intent if isinstance(state.power_intent, dict) else {}
    next_intent = dict(current_intent)
    next_intent["power_domains"] = {rail_name: rail_domain}
    next_intent["power_requirements"] = _update_requirements_container(current_intent.get("power_requirements"))
    state.power_intent = next_intent

    netlist = getattr(state, "netlist", None)
    metadata = dict(getattr(netlist, "metadata", {}) or {}) if netlist is not None else {}
    metadata["power_requirements"] = _update_requirements_container(metadata.get("power_requirements"))
    if netlist is not None:
        netlist.metadata = metadata
    decaps = (state.pi_constraint_set or {}).get("decap") or []
    state.pi_constraint_set = {
        "decap": [item for item in decaps if str(item.get("rail") or "") == rail_name]
    }
    if not state.pi_constraint_set["decap"]:
        state.pi_constraint_set["decap"] = [{"rail": rail_name, "max_distance_mm": 6.0, "_source_level": "manual_default"}]
    return state


def _curve_signature(points: Any) -> str:
    rows = []
    for item in (points or []):
        if not isinstance(item, dict):
            continue
        f_hz = _safe_float(item.get("f_hz"))
        z_ohm = _safe_float(item.get("z_ohm"))
        rows.append((round(f_hz, 3), round(z_ohm, 9)))
    blob = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _run_rail_abc_rerun(loaded: LoadedReport, rail_name: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    runner = _load_numeric_eval_runner()
    started_at = dt.datetime.utcnow()
    state = _load_rail_subgraph_state_for_rerun(loaded, rail_name)
    state = _restrict_state_for_single_rail(state, rail_name, patch)
    evaluated = runner._numeric_only_evaluate(
        state,
        top_k=1,
        rerun_mode="single_rail_fast",
        restrict_rails={rail_name},
        staged_mode="global_local_hybrid",
        segment_budget={"max_segments": 3, "max_steps_per_segment": 3, "max_candidates": 12},
        global_regression_guard={"enabled": True, "max_allowed_global_degrade": 0.05},
    )
    path_a = ((evaluated.get("path_a") or {}).get("rails") or {}).get(rail_name) or {}
    path_b = ((evaluated.get("path_b") or {}).get("rails") or {}).get(rail_name) or {}
    path_c = ((evaluated.get("path_c") or {}).get("rails") or {}).get(rail_name) or {}
    strategy_mode = str((path_a.get("staged_strategy") or {}).get("mode") or "global_local_hybrid")
    global_stage_summary = path_a.get("global_stage_summary") or {}
    segment_stage_summaries = path_a.get("segment_stage_summaries") or []
    regression_guard = path_a.get("global_regression_guard") or {}
    original_a = ((((loaded.payload.get("pdn_evaluation") or {}).get("path_a") or {}).get("rails") or {}).get(rail_name) or {})
    original_b = ((((loaded.payload.get("pdn_evaluation") or {}).get("path_b") or {}).get("rails") or {}).get(rail_name) or {})
    before_ratio = _safe_float(((original_a.get("curve_gap_summary") or {}).get("current_worst_ratio")))
    after_ratio = _safe_float(((path_a.get("curve_gap_summary") or {}).get("current_worst_ratio")))
    before_violations = _safe_int(len(original_b.get("violations") or []))
    after_violations = _safe_int(len(path_b.get("violations") or []))
    before_curve = list(original_a.get("actual_curve_current") or [])
    after_curve = list(path_a.get("actual_curve_current") or [])
    ratio_changed = abs(before_ratio - after_ratio) > 1e-9
    violations_changed = before_violations != after_violations
    curve_changed = _curve_signature(before_curve) != _curve_signature(after_curve)
    summary_delta = {
        "current_worst_ratio_before": before_ratio,
        "current_worst_ratio_after": after_ratio,
        "violations_before": before_violations,
        "violations_after": after_violations,
        "current_curve_points_before": len(before_curve),
        "current_curve_points_after": len(after_curve),
        "ratio_changed": ratio_changed,
        "violations_changed": violations_changed,
        "curve_changed": curve_changed,
        "changed": bool(ratio_changed or violations_changed or curve_changed),
    }
    finished_at = dt.datetime.utcnow()
    duration_ms = int(max((finished_at - started_at).total_seconds() * 1000.0, 0.0))
    return {
        "path_a_rail": path_a,
        "path_b_rail": path_b,
        "path_c_rail": path_c,
        "summary_delta": summary_delta,
        "duration_ms": duration_ms,
        "rerun_mode": "single_rail_fast",
        "strategy_mode": strategy_mode,
        "global_stage_delta": global_stage_summary,
        "segment_stage_deltas": segment_stage_summaries,
        "guard_applied": bool(regression_guard.get("guard_applied")),
    }


def _apply_active_revision_maps(
    loaded: LoadedReport,
    path_a_rails: Dict[str, Any],
    path_b_rails: Dict[str, Any],
    path_c_rails: Dict[str, Any],
    path_d_rails: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    revision_meta_by_rail: Dict[str, Dict[str, Any]] = {}
    override_patch_by_rail: Dict[str, Dict[str, Any]] = {}
    revision_rows: List[Dict[str, Any]] = []
    all_rails = sorted({*path_a_rails.keys(), *path_b_rails.keys(), *path_c_rails.keys(), *path_d_rails.keys()})
    for rail_name in all_rails:
        meta, payload = _active_revision_for_rail(loaded.report_dir, rail_name)
        revision_meta_by_rail[rail_name] = meta
        if meta.get("kind") == "manual":
            revision_rows.append({"rail": rail_name, **meta})
        if not payload:
            continue
        patch = payload.get("override_patch") or {}
        if isinstance(patch, dict) and patch:
            override_patch_by_rail[rail_name] = patch
        rerun = payload.get("rerun_result") or {}
        if isinstance(rerun.get("path_a_rail"), dict):
            path_a_rails[rail_name] = rerun.get("path_a_rail") or {}
        if isinstance(rerun.get("path_b_rail"), dict):
            path_b_rails[rail_name] = rerun.get("path_b_rail") or {}
        if isinstance(rerun.get("path_c_rail"), dict):
            path_c_rails[rail_name] = rerun.get("path_c_rail") or {}
    return path_a_rails, path_b_rails, path_c_rails, path_d_rails, revision_meta_by_rail, override_patch_by_rail, revision_rows


def _build_actions(risk: Dict[str, Any], rail_a: Dict[str, Any], rail_b: Dict[str, Any], rail_c: Dict[str, Any]) -> List[Dict[str, Any]]:
    combined: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for source in (
        _coerce_list(risk.get("recommended_actions")),
        _coerce_list(rail_a.get("recommended_actions")),
        _coerce_list(rail_c.get("datasheet_advisories")),
        _coerce_list(rail_c.get("device_library_advisories")),
    ):
        for item in source:
            if isinstance(item, dict):
                action_type = str(item.get("type") or item.get("action_type") or "review")
                summary = str(item.get("summary") or item.get("detail") or item.get("message") or item)
            else:
                action_type = "review"
                summary = str(item)
            key = (action_type, summary)
            if key in seen:
                continue
            seen.add(key)
            combined.append({"type": action_type, "summary": summary})
    if rail_b.get("violations"):
        combined.append(
            {
                "type": "distance",
                "summary": f"存在 {_safe_int(len(rail_b.get('violations') or []))} 条去耦距离违规",
            }
        )
    return combined


def _action_summary(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("summary") or item.get("detail") or item.get("message") or item.get("type") or "action")
    return str(item)


def _evidence_summary(item: Any) -> str:
    if isinstance(item, dict):
        pairs = []
        for key in ("type", "source", "label", "message", "detail", "reason", "ref", "net", "value"):
            value = item.get(key)
            if value is not None and str(value).strip():
                pairs.append(f"{key}:{value}")
            if len(pairs) >= 3:
                break
        return " | ".join(pairs) if pairs else str(item)[:160]
    return str(item)


def _slim_selection_trace(rows: List[Dict[str, Any]], limit: int = 160) -> List[Dict[str, Any]]:
    return [
        {
            "stage_label": item.get("stage_label") or item.get("stage"),
            "selected_cap": item.get("selected_cap"),
            "before_worst_ratio": item.get("before_worst_ratio"),
            "after_worst_ratio": item.get("after_worst_ratio"),
            "improvement": item.get("improvement"),
            "scope": item.get("scope") or "global",
        }
        for item in rows[:limit]
    ]


def _slim_segment(item: Dict[str, Any], source_path: str = "unknown") -> Dict[str, Any]:
    def _slim_curve(points: Any, limit: int = 180) -> List[Dict[str, float]]:
        rows = list(points or [])
        if len(rows) <= limit:
            selected = rows
        else:
            step = max(1, len(rows) // limit)
            selected = rows[::step]
            if rows and selected and rows[-1] is not selected[-1]:
                selected.append(rows[-1])
        return [
            {"f_hz": _safe_float(row.get("f_hz")), "z_ohm": _safe_float(row.get("z_ohm"))}
            for row in selected
            if isinstance(row, dict)
        ]

    curves = item.get("impedance_curves") or {}
    return {
        "segment_id": item.get("segment_id"),
        "segment_curve_source": source_path,
        "display_name": item.get("display_name") or item.get("label"),
        "kind": item.get("kind"),
        "z_target": item.get("z_target"),
        "priority_adjustment": item.get("priority_adjustment"),
        "load_refs": (item.get("load_refs") or [])[:6],
        "cap_refs": (item.get("cap_refs") or item.get("current_installed_caps") or [])[:6],
        "violations": (item.get("violations") or [])[:12],
        "actual_board_response": {
            "worst_ratio": ((item.get("actual_board_response") or {}).get("worst_ratio")),
        },
        "curves": {
            "target": _slim_curve(curves.get("target_curve") or item.get("target_curve")),
            "baseline": _slim_curve(curves.get("actual_curve_baseline") or item.get("actual_curve_baseline")),
            "current": _slim_curve(curves.get("actual_curve_current") or item.get("actual_curve_current")),
            "stage1": _slim_curve(curves.get("actual_curve_stage1") or item.get("actual_curve_stage1")),
            "stage2": _slim_curve(curves.get("actual_curve_stage2") or item.get("actual_curve_stage2")),
        },
    }


def _slim_violation(item: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("ref", "cap_ref", "load_ref", "from_ref", "to_ref", "distance_mm", "message", "reason", "segment_id")
    return {key: item.get(key) for key in keys if item.get(key) is not None}


def _region_tone(kind: str) -> str:
    normalized = str(kind or "").lower()
    if normalized in {"source_entry", "source"}:
        return "high"
    if normalized in {"load_cluster", "load"}:
        return "critical"
    if normalized in {"post_bead", "filter", "domain"}:
        return "medium"
    if normalized in {"return_loop", "loop"}:
        return "high"
    return "info"


def _region_role(kind: str) -> str:
    normalized = str(kind or "").lower()
    if normalized in {"source_entry", "source"}:
        return "Power Entry"
    if normalized in {"load_cluster", "load"}:
        return "Load Vicinity"
    if normalized in {"post_bead", "filter", "domain"}:
        return "Filtered Domain"
    if normalized in {"return_loop", "loop"}:
        return "Return Loop"
    return "Local Segment"


def _normalize_region_views(rail_name: str, rail_a: Dict[str, Any], rail_b: Dict[str, Any], rail_c: Dict[str, Any]) -> List[Dict[str, Any]]:
    region_map: Dict[str, Dict[str, Any]] = {}
    violations_by_segment: Dict[str, List[Dict[str, Any]]] = {}
    for violation in rail_b.get("violations") or []:
        segment_id = str(violation.get("segment_id") or "")
        if not segment_id:
            continue
        violations_by_segment.setdefault(segment_id, []).append(_slim_violation(violation))

    for item in rail_a.get("segments") or []:
        segment_id = str(item.get("segment_id") or f"{rail_name}:{item.get('kind') or 'segment'}")
        kind = str(item.get("kind") or "segment")
        refs = []
        for key in ("load_refs", "dominant_load_refs", "critical_caps", "current_installed_caps", "required_caps", "anchor_refs", "source_entry_refs", "bead_refs"):
            refs.extend(str(value) for value in (item.get(key) or []) if value)
        region_map[segment_id] = {
            "id": segment_id,
            "display_name": item.get("display_name") or item.get("label") or segment_id,
            "segment_ids": [segment_id],
            "kind": kind,
            "role": _region_role(kind),
            "tone": _region_tone(kind),
            "anchors": [str(value) for value in (item.get("anchor_refs") or []) if value],
            "source_entries": [str(value) for value in (item.get("source_entry_refs") or []) if value],
            "bead_refs": [str(value) for value in (item.get("bead_refs") or []) if value],
            "load_refs": [str(value) for value in (item.get("load_refs") or []) if value],
            "critical_caps": [str(value) for value in (item.get("critical_caps") or []) if value],
            "related_refs": sorted(set(refs)),
            "z_target": _safe_float(item.get("z_target")),
            "worst_ratio": _safe_float((item.get("actual_board_response") or {}).get("worst_ratio")),
            "peak_z": _safe_float((item.get("actual_board_response") or {}).get("peak_z")),
            "lost_band_count": _safe_int((item.get("actual_board_response") or {}).get("lost_band_count")),
            "severity": _safe_float(item.get("severity")),
            "action_tags": [str(value) for value in (item.get("action_tags") or []) if value],
            "dominant_missing_bands": item.get("dominant_missing_bands") or [],
            "worst_band": item.get("worst_band") or {},
            "violations": violations_by_segment.get(segment_id, []),
            "datasheet_notes": [str(value) for value in (rail_c.get("datasheet_advisories") or []) if value][:6],
        }
    return sorted(region_map.values(), key=lambda row: (-row["worst_ratio"], -row["lost_band_count"], row["display_name"]))


def _component_shape_payload(component: Dict[str, Any]) -> Dict[str, Any]:
    pins = []
    for pin in component.get("pins") or []:
        rel_x = _safe_float(pin.get("rel_x"))
        rel_y = _safe_float(pin.get("rel_y"))
        abs_x = _safe_float(component.get("x")) + _safe_float(pin.get("abs_dx"))
        abs_y = _safe_float(component.get("y")) + _safe_float(pin.get("abs_dy"))
        pins.append(
            {
                "pin": pin.get("pin"),
                "net_id": pin.get("net_id"),
                "shape": pin.get("shape"),
                "width": _safe_float(pin.get("width"), 0.02),
                "height": _safe_float(pin.get("height"), 0.02),
                "rel_x": rel_x,
                "rel_y": rel_y,
                "x": abs_x,
                "y": abs_y,
            }
        )
    return {
        "ref": component.get("ref"),
        "class": component.get("class"),
        "package": component.get("package"),
        "value": component.get("value"),
        "x": _safe_float(component.get("x")),
        "y": _safe_float(component.get("y")),
        "width": _safe_float(component.get("width"), 0.1),
        "height": _safe_float(component.get("height"), 0.1),
        "rotation": _safe_float(component.get("rotation")),
        "layer": component.get("layer"),
        "pins": pins,
    }


def _build_net_segments(net_entry: Dict[str, Any], component_index: Dict[str, Dict[str, Any]], allowed_refs: Optional[set[str]] = None, limit: int = 280) -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []
    for cluster in net_entry.get("clusters") or []:
        points = []
        for point in cluster:
            ref = str(point.get("ref") or "")
            if allowed_refs and ref not in allowed_refs:
                continue
            component = component_index.get(ref)
            if not component:
                continue
            pin = (component.get("pin_index") or {}).get(str(point.get("pin") or ""))
            if not pin:
                continue
            points.append(
                {
                    "ref": ref,
                    "pin": point.get("pin"),
                    "x": _safe_float(pin.get("x")),
                    "y": _safe_float(pin.get("y")),
                }
            )
        for start, end in zip(points, points[1:]):
            segments.append(
                {
                    "from_ref": start["ref"],
                    "from_pin": start["pin"],
                    "to_ref": end["ref"],
                    "to_pin": end["pin"],
                    "x1": start["x"],
                    "y1": start["y"],
                    "x2": end["x"],
                    "y2": end["y"],
                }
            )
            if len(segments) >= limit:
                return segments
    return segments


def _bounds_from_shapes(components: List[Dict[str, Any]]) -> Dict[str, float]:
    if not components:
        return {"min_x": 0.0, "min_y": 0.0, "max_x": 1.0, "max_y": 1.0}
    min_x = min(_safe_float(item.get("x")) - 0.5 * _safe_float(item.get("width"), 0.1) for item in components)
    min_y = min(_safe_float(item.get("y")) - 0.5 * _safe_float(item.get("height"), 0.1) for item in components)
    max_x = max(_safe_float(item.get("x")) + 0.5 * _safe_float(item.get("width"), 0.1) for item in components)
    max_y = max(_safe_float(item.get("y")) + 0.5 * _safe_float(item.get("height"), 0.1) for item in components)
    return {"min_x": min_x, "min_y": min_y, "max_x": max_x, "max_y": max_y}


def _shape_bounds(points: List[Tuple[float, float]]) -> Optional[Tuple[float, float, float, float]]:
    if not points:
        return None
    xs = [float(x) for x, _ in points]
    ys = [float(y) for _, y in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _component_bounds(component: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    shapes: List[Tuple[float, float]] = []
    for polygon in component.get("polygons") or []:
        shapes.extend((float(point.get("x")), float(point.get("y"))) for point in polygon if point)
    for polyline in component.get("polylines") or []:
        shapes.extend((float(point.get("x")), float(point.get("y"))) for point in polyline if point)
    for pin in component.get("pins") or []:
        shapes.append((float(pin.get("x")), float(pin.get("y"))))
    if shapes:
        return _shape_bounds(shapes)
    cx = _safe_float(component.get("x"))
    cy = _safe_float(component.get("y"))
    half_w = 0.5 * max(_safe_float(component.get("width"), 0.1), 0.01)
    half_h = 0.5 * max(_safe_float(component.get("height"), 0.1), 0.01)
    rotation = _safe_float(component.get("rotation"))
    corners = [
        _rotate_point(-half_w, -half_h, rotation),
        _rotate_point(half_w, -half_h, rotation),
        _rotate_point(half_w, half_h, rotation),
        _rotate_point(-half_w, half_h, rotation),
    ]
    return _shape_bounds([(cx + dx, cy + dy) for dx, dy in corners])


def _geometry_bounds(
    components: List[Dict[str, Any]],
    trace_paths: Optional[List[Dict[str, Any]]] = None,
    copper_regions: Optional[List[Dict[str, Any]]] = None,
    net_segments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, float]:
    bounds: List[Tuple[float, float, float, float]] = []
    for component in components:
        item_bounds = _component_bounds(component)
        if item_bounds:
            bounds.append(item_bounds)
    for trace in trace_paths or []:
        points = [(float(point.get("x")), float(point.get("y"))) for point in (trace.get("points") or []) if point]
        item_bounds = _shape_bounds(points)
        if item_bounds:
            bounds.append(item_bounds)
    for region in copper_regions or []:
        points = [(float(point.get("x")), float(point.get("y"))) for point in (region.get("points") or []) if point]
        item_bounds = _shape_bounds(points)
        if item_bounds:
            bounds.append(item_bounds)
    for segment in net_segments or []:
        item_bounds = _shape_bounds(
            [
                (_safe_float(segment.get("x1")), _safe_float(segment.get("y1"))),
                (_safe_float(segment.get("x2")), _safe_float(segment.get("y2"))),
            ]
        )
        if item_bounds:
            bounds.append(item_bounds)
    if not bounds:
        return _bounds_from_shapes(components)
    min_x = min(item[0] for item in bounds)
    min_y = min(item[1] for item in bounds)
    max_x = max(item[2] for item in bounds)
    max_y = max(item[3] for item in bounds)
    pad_x = max((max_x - min_x) * 0.06, 0.12)
    pad_y = max((max_y - min_y) * 0.06, 0.12)
    return {
        "min_x": min_x - pad_x,
        "min_y": min_y - pad_y,
        "max_x": max_x + pad_x,
        "max_y": max_y + pad_y,
    }


def _intersects_bounds(points: List[Dict[str, Any]], bounds: Dict[str, float]) -> bool:
    if not points:
        return False
    xs = [_safe_float(point.get("x")) for point in points]
    ys = [_safe_float(point.get("y")) for point in points]
    return not (
        max(xs) < bounds["min_x"]
        or min(xs) > bounds["max_x"]
        or max(ys) < bounds["min_y"]
        or min(ys) > bounds["max_y"]
    )


def _expand_bounds(bounds: Dict[str, float], margin: float) -> Dict[str, float]:
    return {
        "min_x": bounds["min_x"] - margin,
        "min_y": bounds["min_y"] - margin,
        "max_x": bounds["max_x"] + margin,
        "max_y": bounds["max_y"] + margin,
    }


def _build_geometry_bundle(refs: List[str], component_index: Dict[str, Dict[str, Any]], net_entry: Dict[str, Any]) -> Dict[str, Any]:
    ordered_refs = [ref for ref in refs if ref in component_index]
    components = [_component_shape_payload(component_index[ref]) for ref in ordered_refs]
    ref_set = set(ordered_refs)
    net_segments = _build_net_segments(net_entry, component_index, allowed_refs=ref_set)
    return {
        "component_refs": ordered_refs,
        "components": components,
        "bounds": _geometry_bounds(components, net_segments=net_segments),
        "net_segments": net_segments,
    }


def _build_ipc_geometry_bundle(refs: List[str], ipc_geometry: Dict[str, Any], rail_name: str) -> Dict[str, Any]:
    component_index = ipc_geometry.get("component_index") or {}
    ordered_refs = [ref for ref in refs if ref in component_index]
    components = []
    for ref in ordered_refs:
        item = component_index[ref]
        components.append(
            {
                "ref": item.get("ref"),
                "x": _safe_float(item.get("x")),
                "y": _safe_float(item.get("y")),
                "rotation": _safe_float(item.get("rotation")),
                "layer": item.get("layer"),
                "package": item.get("package"),
                "value": item.get("value"),
                "pins": item.get("pins") or [],
                "polygons": item.get("polygons") or [],
                "polylines": item.get("polylines") or [],
            }
        )
    ref_set = set(ordered_refs)
    net_key = _normalize_token(rail_name)
    component_bounds = _geometry_bounds(components)
    span_x = max(component_bounds["max_x"] - component_bounds["min_x"], 0.5)
    span_y = max(component_bounds["max_y"] - component_bounds["min_y"], 0.5)
    focus_bounds = _expand_bounds(component_bounds, max(span_x, span_y) * 1.5 + 4.0)
    traces = []
    for trace in ipc_geometry.get("traces") or []:
        if _normalize_token(trace.get("net")) != net_key:
            continue
        points = trace.get("points") or []
        if not points:
            continue
        if ref_set and not _intersects_bounds(points, focus_bounds):
            continue
        traces.append(
            {
                "layer": trace.get("layer"),
                "net": trace.get("net"),
                "width_mm": _safe_float(trace.get("width_mm"), 0.02),
                "points": [{"x": _safe_float(point.get("x")), "y": _safe_float(point.get("y"))} for point in points],
            }
        )
    copper_regions = []
    for region in ipc_geometry.get("conductors") or []:
        if _normalize_token(region.get("net")) != net_key:
            continue
        points = region.get("points") or []
        if len(points) < 2:
            continue
        if ref_set and not _intersects_bounds(points, focus_bounds):
            continue
        copper_regions.append(
            {
                "layer": region.get("layer"),
                "net": region.get("net"),
                "primitive": region.get("primitive"),
                "width_mm": _safe_float(region.get("width_mm"), 0.0),
                "points": [{"x": _safe_float(point.get("x")), "y": _safe_float(point.get("y"))} for point in points],
            }
        )
    bounds = _geometry_bounds(components, trace_paths=traces, copper_regions=copper_regions)
    return {
        "component_refs": ordered_refs,
        "components": components,
        "bounds": bounds,
        "trace_paths": traces[:260],
        "copper_regions": copper_regions[:220],
        "net_segments": [],
        "source": "ipc",
    }


def _build_ipc_board_bundle(ipc_geometry: Dict[str, Any]) -> Dict[str, Any]:
    components = []
    for item in (ipc_geometry.get("component_index") or {}).values():
        components.append(
            {
                "ref": item.get("ref"),
                "x": _safe_float(item.get("x")),
                "y": _safe_float(item.get("y")),
                "rotation": _safe_float(item.get("rotation")),
                "layer": item.get("layer"),
                "package": item.get("package"),
                "value": item.get("value"),
                "pins": item.get("pins") or [],
                "polygons": item.get("polygons") or [],
                "polylines": item.get("polylines") or [],
                "net_ids": item.get("net_ids") or [],
            }
        )
    traces = [
        {
            "layer": item.get("layer"),
            "net": item.get("net"),
            "width_mm": _safe_float(item.get("width_mm"), 0.02),
            "points": [{"x": _safe_float(point.get("x")), "y": _safe_float(point.get("y"))} for point in (item.get("points") or [])],
        }
        for item in (ipc_geometry.get("traces") or [])
        if len(item.get("points") or []) >= 2
    ]
    copper_regions = [
        {
            "layer": item.get("layer"),
            "net": item.get("net"),
            "primitive": item.get("primitive"),
            "width_mm": _safe_float(item.get("width_mm"), 0.0),
            "points": [{"x": _safe_float(point.get("x")), "y": _safe_float(point.get("y"))} for point in (item.get("points") or [])],
        }
        for item in (ipc_geometry.get("conductors") or [])
        if len(item.get("points") or []) >= 2
    ]
    return {
        "component_refs": [item.get("ref") for item in components if item.get("ref")],
        "components": components,
        "bounds": _geometry_bounds(components, trace_paths=traces, copper_regions=copper_regions),
        "trace_paths": traces,
        "copper_regions": copper_regions,
        "net_segments": [],
        "source": "ipc-board",
    }


def build_layout_geometry(loaded: LoadedReport) -> Dict[str, Any]:
    stat_key = loaded.report_path.as_posix()
    mtime = loaded.report_path.stat().st_mtime_ns
    cached = _LAYOUT_CACHE.get(stat_key)
    if cached and cached[0] == mtime:
        return cached[1]
    report = loaded.payload
    report_dir = loaded.report_dir
    datastruct = _load_datastruct(report, report_dir)
    ipc_geometry = _load_ipc_geometry(report, report_dir)
    if ipc_geometry:
        bundle = _build_ipc_board_bundle(ipc_geometry)
    else:
        components = [_component_shape_payload(item) for item in (datastruct.get("components") or [])]
        bundle = {
            "component_refs": [item.get("ref") for item in components if item.get("ref")],
            "components": components,
            "bounds": _geometry_bounds(components),
            "trace_paths": [],
            "copper_regions": [],
            "net_segments": [],
            "source": "datastruct-board",
        }
    payload = {
        "board_geometry": bundle,
        "board_meta": datastruct.get("board") or {},
        "datastruct_path": datastruct.get("path"),
        "component_count": len(bundle.get("components") or []),
        "pin_count": sum(len(item.get("pins") or []) for item in (bundle.get("components") or [])),
        "trace_count": len(bundle.get("trace_paths") or []),
        "copper_count": len(bundle.get("copper_regions") or []),
        "source": bundle.get("source"),
    }
    _LAYOUT_CACHE[stat_key] = (mtime, payload)
    return payload


def _parameter_order(parameter: str) -> int:
    order = {
        "voltage": 0,
        "current": 1,
        "ripple": 2,
        "target_impedance": 3,
        "switching_frequency": 4,
        "min_decap": 5,
        "max_decap_distance": 6,
    }
    return order.get(str(parameter), 99)


def _classification_rank(value: Any) -> int:
    priority = {"datasheet": 0, "derived": 1, "inferred_name": 2, "report": 3, "missing": 4}
    return priority.get(str(value or ""), 99)


def _confidence_rank(value: Any) -> int:
    priority = {"high": 0, "medium": 1, "low": 2, "none": 3}
    return priority.get(str(value or ""), 99)


def _values_close(lhs: Any, rhs: Any, *, rel_tol: float = 0.05, abs_tol: float = 1e-12) -> bool:
    try:
        left = float(lhs)
        right = float(rhs)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(left) and math.isfinite(right)):
        return False
    return math.isclose(left, right, rel_tol=rel_tol, abs_tol=abs_tol)


def _support_scope_text(source_count: int, component_count: int) -> str:
    if source_count <= 0 and component_count <= 0:
        return "-"
    parts = []
    if source_count > 0:
        parts.append(f"{source_count} source{'s' if source_count != 1 else ''}")
    if component_count > 0:
        parts.append(f"{component_count} component{'s' if component_count != 1 else ''}")
    return " / ".join(parts)


def _aggregate_parameter_evidence(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for row in rows:
        key = (
            row.get("parameter"),
            row.get("label"),
            row.get("value"),
            row.get("range_min"),
            row.get("range_max"),
            row.get("unit"),
            row.get("classification"),
            row.get("source_label"),
            row.get("note"),
            row.get("match_basis"),
            row.get("confidence"),
        )
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {
                **row,
                "source_objects": [],
                "components": [],
                "source_count": 0,
                "component_count": 0,
                "citations": [],
            }
            grouped[key] = bucket
        source_display = str(row.get("source_display") or "").strip()
        if source_display and source_display not in bucket["source_objects"]:
            bucket["source_objects"].append(source_display)
        component = str(row.get("component") or "").strip()
        if component and component not in bucket["components"]:
            bucket["components"].append(component)
        bucket["source_count"] = len(bucket["source_objects"])
        bucket["component_count"] = len(bucket["components"])
        bucket["citations"] = _unique_citations([*(bucket.get("citations") or []), *(row.get("citations") or [])])
    result = list(grouped.values())
    result.sort(
        key=lambda item: (
            _parameter_order(str(item.get("parameter") or "")),
            _classification_rank(item.get("classification")),
            str(item.get("source_label") or ""),
            str(item.get("value") or ""),
        )
    )
    return result


def _build_effective_parameter_summary(rail_name: str, rail_a: Dict[str, Any], facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    selected_requirements = rail_a.get("selected_requirements") or {}
    grouped = _aggregate_parameter_evidence(facts)
    grouped_by_parameter: Dict[str, List[Dict[str, Any]]] = {}
    inferred_voltage_hint = _infer_voltage_from_rail_name(rail_name)
    for item in grouped:
        grouped_by_parameter.setdefault(str(item.get("parameter") or ""), []).append(item)

    configs = [
        ("voltage", "Voltage", "V", _effective_rail_voltage_hint(rail_name, rail_a)),
        ("current", "Current", "A", selected_requirements.get("imax")),
        ("ripple", "Ripple", "V", selected_requirements.get("ripple")),
        (
            "target_impedance",
            "Target Impedance",
            "ohm",
            selected_requirements.get("z_target") if selected_requirements.get("z_target") is not None else rail_a.get("z_target"),
        ),
        (
            "switching_frequency",
            "Switching Frequency",
            "Hz",
            selected_requirements.get("switching_freq_hz")
            if selected_requirements.get("switching_freq_hz") is not None
            else (rail_a.get("focus_band") or {}).get("switching_freq_hz"),
        ),
    ]

    summaries: List[Dict[str, Any]] = []
    for parameter, label, unit, selected_value in configs:
        supports = [item for item in grouped_by_parameter.get(parameter, []) if str(item.get("classification") or "") != "missing"]
        reliable_supports = [item for item in supports if str(item.get("confidence") or "") in {"high", "medium"}]
        supports.sort(
            key=lambda item: (
                _classification_rank(item.get("classification")),
                _confidence_rank(item.get("confidence")),
                str(item.get("source_label") or ""),
                str(item.get("source_display") or ""),
            )
        )
        reliable_supports.sort(
            key=lambda item: (
                _classification_rank(item.get("classification")),
                _confidence_rank(item.get("confidence")),
                str(item.get("source_label") or ""),
                str(item.get("source_display") or ""),
            )
        )
        best_support = reliable_supports[0] if reliable_supports else (supports[0] if supports else None)
        value = None
        classification = "missing"
        source_label = "No traceable upstream source found"
        chosen_supports = reliable_supports

        if parameter == "voltage":
            value = _effective_rail_voltage_hint(rail_name, rail_a)
            if reliable_supports:
                best_voltage = reliable_supports[0].get("value")
                if value is None or _values_close(value, best_voltage, rel_tol=0.02, abs_tol=0.03):
                    value = best_voltage if best_voltage is not None else value
                    classification = reliable_supports[0].get("classification") or "datasheet"
                    source_label = reliable_supports[0].get("source_label") or "Structured datasheet/spec asset"
                elif inferred_voltage_hint is not None and _values_close(value, inferred_voltage_hint, rel_tol=0.02, abs_tol=0.03):
                    classification = "inferred_name"
                    source_label = "Rail-name inference"
                else:
                    value = best_voltage
                    classification = reliable_supports[0].get("classification") or "datasheet"
                    source_label = reliable_supports[0].get("source_label") or "Structured datasheet/spec asset"
            elif value is not None:
                classification = "inferred_name"
                source_label = "Rail-name inference"
        elif parameter == "current":
            if reliable_supports:
                total_current = 0.0
                for item in reliable_supports:
                    if item.get("value") is None:
                        continue
                    multiplicity = int(item.get("source_count") or 0) or len(item.get("source_objects") or []) or 1
                    total_current += float(item.get("value") or 0.0) * multiplicity
                value = total_current if total_current > 0 else None
                classification = "datasheet" if value is not None else "missing"
                source_label = "Aggregated rail current from datasheet/spec evidence" if value is not None else source_label
            elif selected_value is not None:
                value = selected_value
                classification = "report"
                source_label = "Evaluation-selected rail parameter"
        elif parameter == "ripple":
            ripple_values = [float(item.get("value")) for item in reliable_supports if item.get("value") is not None]
            if ripple_values:
                value = min(ripple_values)
                classification = "datasheet"
                source_label = "Conservative rail ripple from datasheet/spec evidence"
            elif selected_value is not None:
                value = selected_value
                classification = "report"
                source_label = "Evaluation-selected rail parameter"
        elif parameter == "target_impedance":
            current_summary = next((item for item in summaries if item.get("parameter") == "current"), None)
            ripple_summary = next((item for item in summaries if item.get("parameter") == "ripple"), None)
            if (
                current_summary
                and ripple_summary
                and current_summary.get("value") is not None
                and ripple_summary.get("value") is not None
                and str(current_summary.get("classification") or "") == "datasheet"
                and str(ripple_summary.get("classification") or "") == "datasheet"
            ):
                value = float(ripple_summary.get("value")) / max(float(current_summary.get("value")), 1e-9)
                classification = "derived"
                source_label = "Derived from aggregated rail current and ripple"
            else:
                target_values = [
                    float(item.get("value"))
                    for item in reliable_supports
                    if item.get("value") is not None and str(item.get("classification") or "") == "datasheet"
                ]
                if target_values:
                    value = min(target_values)
                    classification = "datasheet"
                    source_label = "Conservative datasheet/spec target impedance"
                elif selected_value is not None:
                    value = selected_value
                    classification = "report"
                    source_label = "Evaluation-selected rail parameter"
        else:
            value = selected_value
            if value is None and best_support is not None:
                value = best_support.get("value")
            classification = best_support.get("classification") if best_support is not None else ("report" if value is not None else "missing")
            source_label = (
                best_support.get("source_label")
                if best_support is not None
                else ("Evaluation-selected rail parameter" if value is not None else "No traceable upstream source found")
            )
        if parameter == "voltage" and best_support is None and value is not None:
            classification = "inferred_name"
            source_label = "Rail-name inference"
        supports_for_notes = chosen_supports if chosen_supports else supports
        citations = _unique_citations(item for support in supports_for_notes for item in (support.get("citations") or []))
        source_objects: List[str] = []
        seen_objects = set()
        for support in supports_for_notes:
            for source_object in support.get("source_objects") or []:
                if source_object in seen_objects:
                    continue
                seen_objects.add(source_object)
                source_objects.append(source_object)
        component_count = len(
            {
                str(item.get("component") or "").strip()
                for item in facts
                if str(item.get("parameter") or "") == parameter and str(item.get("component") or "").strip()
            }
        )
        source_count = len(source_objects)
        evidence_count = sum(
            int(item.get("source_count") or 0)
            or max(len(item.get("source_objects") or []), 1 if item.get("source_display") else 0)
            for item in supports_for_notes
        )
        distinct_value_count = len({(item.get("value"), item.get("range_min"), item.get("range_max")) for item in supports_for_notes})
        differs_from_best = (
            best_support is not None
            and value is not None
            and not _values_close(
                value,
                best_support.get("value"),
                rel_tol=0.02 if parameter == "voltage" else 0.05,
                abs_tol=0.03 if parameter == "voltage" else 1e-12,
            )
        )
        if parameter == "target_impedance" and classification == "derived":
            pass
        elif parameter == "target_impedance" and value is not None and differs_from_best:
            if selected_requirements.get("imax") is not None and selected_requirements.get("ripple") is not None:
                classification = "derived"
                source_label = "Derived from selected rail current and ripple"
            else:
                classification = "report"
                source_label = "Evaluation-selected rail parameter"
        elif parameter == "voltage" and best_support is not None and value is not None and differs_from_best:
            if inferred_voltage_hint is not None and _values_close(value, inferred_voltage_hint, rel_tol=0.02, abs_tol=0.03):
                classification = "inferred_name"
                source_label = "Rail-name inference"
            else:
                value = best_support.get("value")
        note_parts = []
        if best_support and best_support.get("note"):
            note_parts.append(str(best_support.get("note")))
        if evidence_count:
            note_parts.append(f"Supported by {evidence_count} evidence row{'s' if evidence_count != 1 else ''}")
        if distinct_value_count > 1:
            note_parts.append(f"{distinct_value_count} distinct candidate values observed")
        summaries.append(
            {
                "parameter": parameter,
                "label": label,
                "value": value,
                "range_min": None,
                "range_max": None,
                "unit": unit,
                "classification": classification,
                "source_label": source_label,
                "source_display": _support_scope_text(source_count, component_count),
                "source_objects": source_objects,
                "source_count": source_count,
                "component_count": component_count,
                "evidence_count": evidence_count,
                "distinct_value_count": distinct_value_count,
                "citations": citations,
                "note": ". ".join(part.strip().rstrip(".") for part in note_parts if str(part or "").strip()) or None,
                "match_basis": (
                    "aggregated datasheet current/ripple"
                    if parameter == "target_impedance" and classification == "derived"
                    else best_support.get("match_basis")
                    if best_support is not None
                    else ("selected_requirements" if value is not None else None)
                ),
                "confidence": (
                    "medium"
                    if parameter == "target_impedance" and classification == "derived"
                    else best_support.get("confidence")
                    if best_support is not None
                    else ("low" if classification == "report" and value is not None else "none")
                ),
            }
        )
    summaries.sort(key=lambda item: _parameter_order(str(item.get("parameter") or "")))
    return summaries


def _parameter_audit_rows(rail_name: str, summary_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_parameter: Dict[str, Dict[str, Any]] = {}
    for item in summary_rows:
        parameter = str(item.get("parameter") or "")
        if parameter not in {"voltage", "current", "ripple", "target_impedance"}:
            continue
        by_parameter[parameter] = item
    return {
        "rail": rail_name,
        "real_extract_count": sum(1 for item in by_parameter.values() if item.get("classification") == "datasheet"),
        "voltage": (by_parameter.get("voltage") or {}).get("classification") or "missing",
        "current": (by_parameter.get("current") or {}).get("classification") or "missing",
        "ripple": (by_parameter.get("ripple") or {}).get("classification") or "missing",
        "target_impedance": (by_parameter.get("target_impedance") or {}).get("classification") or "missing",
        "voltage_value": (by_parameter.get("voltage") or {}).get("value"),
        "current_value": (by_parameter.get("current") or {}).get("value"),
        "ripple_value": (by_parameter.get("ripple") or {}).get("value"),
        "target_impedance_value": (by_parameter.get("target_impedance") or {}).get("value"),
    }


def _build_parameter_audit(path_a_rails: Dict[str, Any]) -> Dict[str, Any]:
    rows = []
    counts = {
        "datasheet": 0,
        "derived": 0,
        "inferred_name": 0,
        "report": 0,
        "missing": 0,
    }
    for rail_name, rail_a in path_a_rails.items():
        provenance = _build_parameter_provenance(
            rail_name,
            rail_a or {},
            (rail_a or {}).get("rail_requirement_sources") or [],
            (rail_a or {}).get("datasheet_decap_requirements") or [],
        )
        summary_rows = list(provenance.get("summary") or [])
        if not summary_rows:
            summary_rows = list(provenance.get("facts") or [])
        row = _parameter_audit_rows(rail_name, summary_rows)
        rows.append(row)
        for key in ("voltage", "current", "ripple", "target_impedance"):
            counts[row[key]] = counts.get(row[key], 0) + 1
    rows.sort(key=lambda item: (-item["real_extract_count"], item["rail"]))
    return {"counts": counts, "rows": rows}


def _load_spec_requirements(part_numbers: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    ordered = []
    seen = set()
    for item in part_numbers:
        key = str(item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    missing = [item for item in ordered if item not in _SPEC_REQUIREMENT_CACHE]
    if missing:
        resolved, _ = load_spec_assets_for_parts(missing, spec_asset_root=DEFAULT_SPEC_ASSET_ROOT)
        for part_number in missing:
            asset = resolved.get(part_number)
            normalized_part = normalize_part_key(part_number)
            strong_match = False
            if asset and len(normalized_part) >= 4 and not normalized_part.isdigit():
                aliases = {normalize_part_key(asset.get("part_number") or "")}
                aliases.update(normalize_part_key(item) for item in (asset.get("aliases") or []))
                strong_match = normalized_part in aliases
            _SPEC_REQUIREMENT_CACHE[part_number] = compile_spec_asset_pi_requirements(asset) if strong_match else None
    return {
        part_number: requirement
        for part_number in ordered
        if (requirement := _SPEC_REQUIREMENT_CACHE.get(part_number))
    }


def _unique_citations(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for row in rows:
        path = _safe_path(row.get("source_path") or row.get("asset_path") or "")
        source_type = str(row.get("source_type") or "source")
        key = (path, source_type)
        if not path or key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "path": path,
                "source_type": source_type,
                "extractor_version": row.get("extractor_version"),
            }
        )
    return result


def _spec_asset_citations(requirements: Dict[str, Any]) -> List[Dict[str, Any]]:
    meta = requirements.get("spec_asset_meta") or {}
    return _unique_citations(meta.get("provenance") or [])


def _match_supply_rows(requirements: Dict[str, Any], supply_name: str, rail_name: str) -> List[Dict[str, Any]]:
    supplies = [item for item in (requirements.get("supplies") or []) if isinstance(item, dict)]
    if not supplies:
        return []
    if supply_name:
        supply_norm = _normalize_token(supply_name)
        matched = [
            item
            for item in supplies
            if _normalize_token(item.get("name")) == supply_norm
            or pi_req.pin_matches_rail(str(item.get("name") or ""), supply_name)
        ]
        if matched:
            return matched
    matched = [item for item in supplies if pi_req.pin_matches_rail(str(item.get("name") or ""), rail_name)]
    return matched


def _fallback_supply_rows_by_voltage_hint(requirements: Dict[str, Any], rail_voltage_hint: Optional[float]) -> List[Dict[str, Any]]:
    if rail_voltage_hint is None:
        return []
    supplies = [item for item in (requirements.get("supplies") or []) if isinstance(item, dict)]
    matched = []
    for item in supplies:
        voltage_v = pi_req.requirements_supply_voltage_v(item)
        if _voltage_matches_rail_hint(voltage_v, rail_voltage_hint):
            matched.append(item)
    return matched


def _narrow_supplies_by_voltage_hint(supplies: List[Dict[str, Any]], rail_voltage_hint: Optional[float]) -> List[Dict[str, Any]]:
    if rail_voltage_hint is None or len(supplies) <= 1:
        return supplies
    numeric_rows: List[Tuple[float, Dict[str, Any]]] = []
    for item in supplies:
        voltage_v = pi_req.requirements_supply_voltage_v(item)
        if voltage_v is None:
            continue
        try:
            numeric_rows.append((abs(float(voltage_v) - float(rail_voltage_hint)), item))
        except (TypeError, ValueError):
            continue
    if not numeric_rows:
        return supplies
    numeric_rows.sort(key=lambda row: row[0])
    best_delta = numeric_rows[0][0]
    return [item for delta, item in numeric_rows if math.isclose(delta, best_delta, abs_tol=1e-9)] or [numeric_rows[0][1]]


def _display_label(component: Any, part_number: Any, pin: Any = None) -> str:
    pieces = [str(component or "").strip(), str(part_number or "").strip(), str(pin or "").strip()]
    return " | ".join(piece for piece in pieces if piece)


def _build_parameter_fact(
    parameter: str,
    label: str,
    value: Any,
    unit: str,
    classification: str,
    source_label: str,
    *,
    component: Any = None,
    part_number: Any = None,
    pin_name: Any = None,
    note: Any = None,
    citations: Optional[List[Dict[str, Any]]] = None,
    derived_from: Optional[List[str]] = None,
    match_basis: Optional[str] = None,
    confidence: Optional[str] = None,
) -> Dict[str, Any]:
    numeric_value = None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        numeric_value = None
    range_min = None
    range_max = None
    note_text = note
    if isinstance(note, dict):
        note_text = note.get("text")
        try:
            range_min = float(note.get("range_min")) if note.get("range_min") is not None else None
        except (TypeError, ValueError):
            range_min = None
        try:
            range_max = float(note.get("range_max")) if note.get("range_max") is not None else None
        except (TypeError, ValueError):
            range_max = None
    if numeric_value is not None and range_min == 0.0 and range_max == 0.0:
        range_min = None
        range_max = None
    return {
        "parameter": parameter,
        "label": label,
        "value": numeric_value,
        "range_min": range_min,
        "range_max": range_max,
        "unit": unit,
        "classification": classification,
        "source_label": source_label,
        "component": component,
        "part_number": part_number,
        "pin_name": pin_name,
        "source_display": _display_label(component, part_number, pin_name),
        "note": str(note_text or "").strip() or None,
        "citations": citations or [],
        "derived_from": list(derived_from or []),
        "match_basis": str(match_basis or "").strip() or None,
        "confidence": str(confidence or "").strip() or None,
    }


def _parameter_sort_key(item: Dict[str, Any]) -> Tuple[int, str, str]:
    order = {
        "voltage": 0,
        "current": 1,
        "ripple": 2,
        "target_impedance": 3,
        "switching_frequency": 4,
        "min_decap": 5,
        "max_decap_distance": 6,
    }
    return (order.get(str(item.get("parameter")), 99), str(item.get("source_display") or ""), str(item.get("classification") or ""))


def _dedupe_parameter_facts(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen: set[Tuple[Any, ...]] = set()
    for row in rows:
        key = (
            row.get("parameter"),
            row.get("value"),
            row.get("range_min"),
            row.get("range_max"),
            row.get("unit"),
            row.get("classification"),
            row.get("component"),
            row.get("part_number"),
            row.get("pin_name"),
            row.get("note"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    result.sort(key=_parameter_sort_key)
    return result


def _build_parameter_provenance(rail_name: str, rail_a: Dict[str, Any], rail_sources: List[Dict[str, Any]], decap_requirements: List[Dict[str, Any]]) -> Dict[str, Any]:
    facts: List[Dict[str, Any]] = []
    source_documents: List[Dict[str, Any]] = []
    requirements_by_part = _load_spec_requirements(item.get("part_number") for item in rail_sources)
    selected_requirements = rail_a.get("selected_requirements") or {}
    selected_voltage = selected_requirements.get("voltage_selection") or {}
    rail_voltage_hint = _effective_rail_voltage_hint(rail_name, rail_a)

    for source in rail_sources:
        if str(source.get("source_type") or "").lower() != "supply":
            continue
        part_number = str(source.get("part_number") or "").strip()
        requirements = requirements_by_part.get(part_number) or {}
        citations = _spec_asset_citations(requirements)
        source_documents.extend(citations)
        source_supply_name = str(source.get("supply_name") or "")
        matched_supplies = _match_supply_rows(requirements, source_supply_name, rail_name)
        match_basis_prefix = "source supply pin match"
        confidence_base = "medium"
        if not matched_supplies:
            matched_supplies = _fallback_supply_rows_by_voltage_hint(requirements, rail_voltage_hint)
            if matched_supplies:
                match_basis_prefix = "fallback by rail voltage hint (name mismatch)"
                confidence_base = "low"
        if not matched_supplies:
            continue
        filtered_supplies = []
        for supply in matched_supplies:
            voltage_v = pi_req.requirements_supply_voltage_v(supply)
            if _voltage_matches_rail_hint(voltage_v, rail_voltage_hint):
                filtered_supplies.append(supply)
        chosen_supplies = filtered_supplies or []
        if not chosen_supplies and rail_voltage_hint is None and len(matched_supplies) == 1:
            chosen_supplies = matched_supplies
        if len(chosen_supplies) > 1:
            narrowed = _narrow_supplies_by_voltage_hint(chosen_supplies, rail_voltage_hint)
            if rail_voltage_hint is None:
                chosen_supplies = narrowed if len(narrowed) == 1 else []
            else:
                chosen_supplies = narrowed
        for supply in chosen_supplies:
            component = source.get("component")
            pin_name = supply.get("name") or source.get("supply_name")
            voltage_v = pi_req.requirements_supply_voltage_v(supply)
            current_a = pi_req.requirements_supply_current_a(supply)
            ripple_v = supply.get("ripple_v")
            target_impedance = supply.get("target_impedance_ohm")
            hints = requirements.get("impedance_hints") or {}
            if ripple_v is None:
                ripple_v = hints.get("ripple_v")
            if target_impedance is None:
                target_impedance = hints.get("target_impedance_ohm")
            confidence = _supply_match_confidence(rail_name, source_supply_name, str(pin_name or ""))
            if confidence_base == "low" and confidence != "none":
                confidence = "low"
            match_basis = match_basis_prefix
            if rail_voltage_hint is not None and voltage_v is not None:
                match_basis = f"{match_basis_prefix}; rail voltage hint {rail_voltage_hint:.4g}V"

            if voltage_v is not None or supply.get("voltage_min_v") is not None or supply.get("voltage_max_v") is not None:
                voltage_note = None
                if supply.get("voltage_min_v") is not None or supply.get("voltage_max_v") is not None:
                    note_text = "Structured datasheet/spec asset; range retained for evaluation."
                    if selected_voltage:
                        note_text = f"{selected_voltage.get('reason') or note_text} Strategy: {selected_voltage.get('strategy') or 'unknown'}."
                    voltage_note = {
                        "text": note_text,
                        "range_min": supply.get("voltage_min_v"),
                        "range_max": supply.get("voltage_max_v"),
                    }
                facts.append(
                    _build_parameter_fact(
                        "voltage",
                        "Voltage",
                        voltage_v,
                        "V",
                        "datasheet",
                        "Structured datasheet/spec asset",
                        component=component,
                        part_number=part_number,
                        pin_name=pin_name,
                        note=voltage_note,
                        citations=citations,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )
            if current_a is not None:
                facts.append(
                    _build_parameter_fact(
                        "current",
                        "Current",
                        current_a,
                        "A",
                        "datasheet",
                        "Structured datasheet/spec asset",
                        component=component,
                        part_number=part_number,
                        pin_name=pin_name,
                        citations=citations,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )
            if ripple_v is not None:
                facts.append(
                    _build_parameter_fact(
                        "ripple",
                        "Ripple",
                        ripple_v,
                        "V",
                        "datasheet",
                        "Structured datasheet/spec asset",
                        component=component,
                        part_number=part_number,
                        pin_name=pin_name,
                        citations=citations,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )
            if target_impedance is not None:
                facts.append(
                    _build_parameter_fact(
                        "target_impedance",
                        "Target Impedance",
                        target_impedance,
                        "ohm",
                        "datasheet",
                        "Structured datasheet/spec asset",
                        component=component,
                        part_number=part_number,
                        pin_name=pin_name,
                        citations=citations,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )
            elif current_a and ripple_v:
                facts.append(
                    _build_parameter_fact(
                        "target_impedance",
                        "Target Impedance",
                        float(ripple_v) / max(float(current_a), 1e-9),
                        "ohm",
                        "derived",
                        "Derived from datasheet current and ripple",
                        component=component,
                        part_number=part_number,
                        pin_name=pin_name,
                        citations=citations,
                        derived_from=["current", "ripple"],
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )
            switching_freq_hz = hints.get("switching_freq_hz")
            if switching_freq_hz is not None:
                operating_band = rail_a.get("focus_band") or {}
                note = None
                if operating_band:
                    note = (
                        f"Evaluation focus band {float(operating_band.get('f_start_hz', 0.0) or 0.0):.0f} Hz "
                        f"to {float(operating_band.get('f_end_hz', 0.0) or 0.0):.0f} Hz."
                    )
                facts.append(
                    _build_parameter_fact(
                        "switching_frequency",
                        "Switching Frequency",
                        switching_freq_hz,
                        "Hz",
                        "datasheet",
                        "Structured datasheet/spec asset",
                        component=component,
                        part_number=part_number,
                        pin_name=pin_name,
                        note=note,
                        citations=citations,
                        match_basis=match_basis,
                        confidence=confidence,
                    )
                )

    for item in decap_requirements:
        component = item.get("component")
        part_number = item.get("part_number")
        pin_name = item.get("target_pin")
        requirements = requirements_by_part.get(str(part_number or "").strip()) or {}
        citations = _spec_asset_citations(requirements)
        source_documents.extend(citations)
        if item.get("min_capacitance_f") is not None:
            facts.append(
                _build_parameter_fact(
                    "min_decap",
                    "Min Decap",
                    item.get("min_capacitance_f"),
                    "F",
                    "datasheet",
                    "Datasheet note in report",
                    component=component,
                    part_number=part_number,
                    pin_name=pin_name,
                    note=item.get("note"),
                    citations=citations,
                )
            )
        if item.get("distance_mm") is not None:
            facts.append(
                _build_parameter_fact(
                    "max_decap_distance",
                    "Max Decap Distance",
                    item.get("distance_mm"),
                    "mm",
                    "datasheet",
                    "Datasheet note in report",
                    component=component,
                    part_number=part_number,
                    pin_name=pin_name,
                    note=item.get("note"),
                    citations=citations,
                )
            )

    report_target = rail_a.get("z_target")
    if report_target is not None:
        facts.append(
            _build_parameter_fact(
                "target_impedance",
                "Target Impedance",
                report_target,
                "ohm",
                "report",
                "Evaluation report value (upstream source not attached)",
                match_basis="report payload",
                confidence="medium",
            )
        )

    parameters_present = {item.get("parameter") for item in facts}
    if "voltage" not in parameters_present:
        inferred_voltage = _infer_voltage_from_rail_name(rail_name)
        if inferred_voltage is not None:
            facts.append(
                _build_parameter_fact(
                    "voltage",
                    "Voltage",
                    inferred_voltage,
                    "V",
                    "inferred_name",
                    "Rail-name inference",
                    note=f"Inferred from rail name {rail_name}",
                    match_basis="rail-name inference",
                    confidence="medium",
                )
            )
            parameters_present.add("voltage")
    for parameter, label, unit in (
        ("voltage", "Voltage", "V"),
        ("current", "Current", "A"),
        ("ripple", "Ripple", "V"),
    ):
        if parameter not in parameters_present:
            facts.append(
                _build_parameter_fact(
                    parameter,
                    label,
                    None,
                    unit,
                    "missing",
                    "No traceable upstream source found",
                    confidence="none",
                )
            )

    facts = _dedupe_parameter_facts(facts)
    evidence = _aggregate_parameter_evidence(facts)
    summary = _build_effective_parameter_summary(rail_name, rail_a, facts)
    return {
        "facts": facts,
        "summary": summary,
        "evidence": evidence,
        "source_documents": _unique_citations(source_documents),
    }


def _load_datastruct(report: Dict[str, Any], report_dir: Path) -> Dict[str, Any]:
    inputs = report.get("inputs") or {}
    datastruct_value = inputs.get("datastruct_json")
    if not datastruct_value:
        return {}
    datastruct_path = _path_from_payload(datastruct_value)
    if not datastruct_path.is_absolute():
        candidates = [
            (Path.cwd() / datastruct_path).resolve(),
            (report_dir / datastruct_path).resolve(),
        ]
        datastruct_path = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    if not datastruct_path.exists():
        return {}
    raw = json.loads(datastruct_path.read_text(encoding="utf-8-sig"))
    size = raw.get("size") or {}
    components = []
    component_index: Dict[str, Dict[str, Any]] = {}
    for item in raw.get("ComponentInfo") or []:
        ref = str(item.get("componentID") or "")
        pos = item.get("position") or {}
        pins = item.get("pins") or []
        rotation = _safe_float(pos.get("rotation"))
        pin_payloads = []
        pin_index: Dict[str, Dict[str, Any]] = {}
        for pin in pins:
            pin_pos = (pin or {}).get("position") or {}
            rel_x = _safe_float(pin_pos.get("x"))
            rel_y = _safe_float(pin_pos.get("y"))
            abs_dx, abs_dy = _rotate_point(rel_x, rel_y, rotation)
            pad = (pin or {}).get("padSize") or {}
            payload = {
                "pin": str((pin or {}).get("pinNumber") or ""),
                "net_id": str((pin or {}).get("netID") or ""),
                "shape": pad.get("shape") or "rectangle",
                "width": _safe_float(pad.get("width"), 0.02),
                "height": _safe_float(pad.get("height"), 0.02),
                "rel_x": rel_x,
                "rel_y": rel_y,
                "abs_dx": abs_dx,
                "abs_dy": abs_dy,
                "x": _safe_float(pos.get("x")) + abs_dx,
                "y": _safe_float(pos.get("y")) + abs_dy,
            }
            pin_payloads.append(payload)
            pin_index[payload["pin"]] = payload
        comp = {
            "ref": ref,
            "class": item.get("class"),
            "package": item.get("package"),
            "value": item.get("value"),
            "x": _safe_float(pos.get("x")),
            "y": _safe_float(pos.get("y")),
            "rotation": rotation,
            "width": _safe_float((item.get("size") or {}).get("width"), 1.0),
            "height": _safe_float((item.get("size") or {}).get("height"), 1.0),
            "layer": (item.get("size") or {}).get("layer"),
            "net_ids": sorted({str((pin or {}).get("netID") or "") for pin in pins if (pin or {}).get("netID")}),
            "pin_count": len(pins),
            "pins": pin_payloads,
            "pin_index": pin_index,
        }
        components.append(comp)
        component_index[ref] = comp

    nets = []
    net_index: Dict[str, Dict[str, Any]] = {}
    for item in raw.get("NetInfo") or []:
        net_id = str(item.get("netID") or "")
        connections = item.get("connections") or []
        refs = []
        edges = []
        for cluster in connections:
            prev_ref = None
            for pin in cluster:
                ref = str((pin or {}).get("componentID") or "")
                if ref:
                    refs.append(ref)
                if prev_ref and ref and prev_ref != ref:
                    edges.append({"from": prev_ref, "to": ref})
                prev_ref = ref
        payload = {
            "net_id": net_id,
            "net_class": item.get("netClass"),
            "refs": sorted(set(refs)),
            "edges": edges[:400],
            "clusters": [
                [{"ref": str((pin or {}).get("componentID") or ""), "pin": str((pin or {}).get("pin") or "")} for pin in cluster if (pin or {}).get("componentID")]
                for cluster in connections[:240]
            ],
        }
        nets.append(payload)
        net_index[_normalize_token(net_id)] = payload

    return {
        "path": datastruct_path.as_posix(),
        "board": {
            "width": _safe_float(size.get("width"), 100.0),
            "height": _safe_float(size.get("height"), 100.0),
        },
        "components": components,
        "component_index": component_index,
        "nets": nets,
        "net_index": net_index,
    }


def _resolve_input_path(report: Dict[str, Any], report_dir: Path, key: str) -> Optional[Path]:
    value = ((report.get("inputs") or {}).get(key))
    if not value:
        return None
    raw_path = _path_from_payload(value)
    if raw_path.is_absolute():
        return raw_path if raw_path.exists() else None
    candidates = [
        (Path.cwd() / raw_path).resolve(),
        (report_dir / raw_path).resolve(),
    ]
    return next((candidate for candidate in candidates if candidate.exists()), None)


def _transform_ipc_points(points: Iterable[Tuple[float, float]], x: float, y: float, rotation: float, mirror: bool) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for px, py in points:
        qx, qy = (-px, py) if mirror else (px, py)
        dx, dy = _rotate_point(float(qx), float(qy), rotation)
        out.append((x + dx, y + dy))
    return out


def _load_ipc_geometry(report: Dict[str, Any], report_dir: Path) -> Dict[str, Any]:
    ipc_path = _resolve_input_path(report, report_dir, "ipc_xml")
    if not ipc_path:
        return {}
    stat_key = ipc_path.as_posix()
    mtime = ipc_path.stat().st_mtime_ns
    cached = _IPC_CACHE.get(stat_key)
    if cached and cached[0] == mtime:
        return cached[1]

    parser = _load_ipc_parser()
    traces, components, _, package_shapes, comp_net_map = parser.parse_ipc2581(
        xml_path=ipc_path,
        layers=["ALL"],
        allow_nets=[],
        allow_net_regex=None,
        default_width=0.2,
        conductor_only=True,
        include_no_net=False,
    )
    conductors = parser.parse_ipc2581_conductors(
        xml_path=ipc_path,
        layers=["ALL"],
        allow_nets=[],
        allow_net_regex=None,
        default_width=0.2,
        conductor_only=True,
        include_no_net=False,
    )

    component_index: Dict[str, Dict[str, Any]] = {}
    for comp in components:
        shape = package_shapes.get(comp.package)
        polygons = []
        polylines = []
        pins = []
        if shape:
            polygons = [
                [{"x": x, "y": y} for x, y in _transform_ipc_points(poly, comp.x, comp.y, float(comp.rotation), bool(comp.mirror))]
                for poly in (shape.polygons or [])
                if len(poly) >= 2
            ]
            polylines = [
                [{"x": x, "y": y} for x, y in _transform_ipc_points(line, comp.x, comp.y, float(comp.rotation), bool(comp.mirror))]
                for line in (shape.polylines or [])
                if len(line) >= 2
            ]
            for pin_name, px, py in (shape.pins or []):
                tx, ty = _transform_ipc_points([(px, py)], comp.x, comp.y, float(comp.rotation), bool(comp.mirror))[0]
                pins.append({"pin": pin_name, "x": tx, "y": ty})
        component_index[str(comp.ref)] = {
            "ref": str(comp.ref),
            "x": float(comp.x),
            "y": float(comp.y),
            "rotation": float(comp.rotation),
            "layer": str(comp.layer),
            "package": str(comp.package),
            "part": str(comp.part),
            "value": str(comp.value),
            "mirror": bool(comp.mirror),
            "polygons": polygons,
            "polylines": polylines,
            "pins": pins,
            "net_ids": [str(net) for net in (comp_net_map.get(comp.ref) or [])],
        }

    payload = {
        "path": ipc_path.as_posix(),
        "component_index": component_index,
        "traces": [
            {
                "layer": str(trace.layer),
                "net": str(trace.net),
                "width_mm": float(trace.width_mm),
                "points": [{"x": float(x), "y": float(y)} for x, y in trace.points],
            }
            for trace in traces
            if len(trace.points) >= 2
        ],
        "conductors": [
            {
                "layer": str(item.layer),
                "net": str(item.net),
                "primitive": str(item.primitive),
                "width_mm": float(item.width_mm),
                "points": [{"x": float(x), "y": float(y)} for x, y in item.points],
            }
            for item in conductors
            if len(item.points) >= 2
        ],
        "component_net_map": {str(key): [str(net) for net in value] for key, value in comp_net_map.items()},
    }
    _IPC_CACHE[stat_key] = (mtime, payload)
    return payload


def _normalize_board_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _report_board_candidates(report: Dict[str, Any], report_dir: Path) -> List[str]:
    tokens: List[str] = []

    def _add(value: Any) -> None:
        token = _normalize_board_token(value)
        if token and token not in tokens:
            tokens.append(token)

    board = report.get("board")
    if isinstance(board, dict):
        for key in ("name", "board_name", "id"):
            _add(board.get(key))
    else:
        _add(board)
    summary = report.get("summary") or {}
    _add(summary.get("board"))
    _add(summary.get("board_name"))
    inputs = report.get("inputs") or {}
    board_dir_input = inputs.get("board_dir")
    if board_dir_input:
        _add(_path_from_payload(board_dir_input).name)
    for key in ("ipc_xml", "netlist_json", "datastruct_json"):
        raw = str(inputs.get(key) or "")
        if not raw:
            continue
        raw_path = _path_from_payload(raw)
        _add(raw_path.stem)
        for part in raw_path.parts:
            _add(part)

    title = report_dir.name
    _add(title)
    _add(title.split("_")[0])
    return tokens


def _collect_benchmark_files(root: Path) -> List[Path]:
    if not root.exists():
        return []
    selected: Dict[str, Path] = {}
    patterns = ("*benchmark_report.json", "benchmark_report.json", "*abc_recall*.json", "*layout_pathc_recall*.json", "*archived_testboard_recall*.json")
    for pattern in patterns:
        for path in root.rglob(pattern):
            if path.is_file():
                selected[path.as_posix()] = path
    return sorted(selected.values())


def _safe_div(num: float, den: float) -> Optional[float]:
    if not den:
        return None
    return num / den


def _extract_hi3519_board_cases(payload: Dict[str, Any], source_file: Path) -> List[Tuple[str, Dict[str, Any]]]:
    out: List[Tuple[str, Dict[str, Any]]] = []
    boards = payload.get("boards") or {}
    for board_name, board_payload in boards.items():
        details = board_payload.get("details") or []
        expected = len(details)
        matched = _safe_int(((board_payload.get("abc_any_recall") or {}).get("hits")), 0)
        label_rows = []
        for item in details:
            path_a = item.get("path_a") or {}
            path_b = item.get("path_b") or {}
            path_c = item.get("path_c") or {}
            label_rows.append(
                {
                    "id": f"{item.get('component')}-{_slug(str(item.get('rail')))}",
                    "description": item.get("group") or "",
                    "component": item.get("component") or "",
                    "rail": item.get("rail") or "",
                    "group": item.get("group") or "",
                    "matched": bool(item.get("abc_any_hit")),
                    "match_count": int(bool(item.get("abc_any_hit"))),
                    "examples": {
                        "path_a": (path_a.get("risk_classification") or [])[:3],
                        "path_b": (path_b.get("violations") or [])[:3],
                        "path_c": (path_c.get("risk_points") or [])[:3],
                    },
                }
            )
        missing = [row for row in label_rows if not row.get("matched")]
        out.append(
            (
                _normalize_board_token(board_name),
                {
                    "id": f"{_slug(board_name)}::{source_file.stem}",
                    "board": board_name,
                    "case": source_file.stem,
                    "case_type": "abc_recall",
                    "source_file": source_file.as_posix(),
                    "metrics": {
                        "expected_labels": expected,
                        "matched_labels": matched,
                        "label_precision": None,
                        "label_recall": _safe_float((board_payload.get("abc_any_recall") or {}).get("recall"), 0.0) if expected else None,
                        "proxy_precision": None,
                        "proxy_recall": _safe_div(matched, expected),
                        "layout_risk_point_count": None,
                    },
                    "path_recall": {
                        "path_a": (board_payload.get("path_a_recall") or {}).get("recall"),
                        "path_b": (board_payload.get("path_b_recall") or {}).get("recall"),
                        "path_c": (board_payload.get("path_c_recall") or {}).get("recall"),
                        "abc_any": (board_payload.get("abc_any_recall") or {}).get("recall"),
                    },
                    "labels": label_rows,
                    "missing_labels": missing,
                    "status": "ok" if expected and matched >= expected else "warning",
                },
            )
        )
    return out


def _extract_t1_case_rows(payload: Dict[str, Any], source_file: Path) -> List[Tuple[str, Dict[str, Any]]]:
    out: List[Tuple[str, Dict[str, Any]]] = []
    for case_name, case_payload in (payload.get("cases") or {}).items():
        metrics = case_payload.get("metrics") or {}
        expected = _safe_int(metrics.get("expected_labels"), 0)
        matched = _safe_int(metrics.get("matched_labels"), 0)
        layout_count = _safe_int(case_payload.get("layout_risk_point_count"), 0)
        labels = []
        for item in (case_payload.get("label_matches") or []):
            labels.append(
                {
                    "id": item.get("id") or "",
                    "description": item.get("description") or "",
                    "component": ((item.get("examples") or [{}])[0].get("component") if item.get("examples") else "") or "",
                    "rail": ((item.get("examples") or [{}])[0].get("rail") if item.get("examples") else "") or "",
                    "group": "",
                    "matched": bool(item.get("matched")),
                    "match_count": _safe_int(item.get("match_count"), 0),
                    "examples": {"matches": (item.get("examples") or [])[:4]},
                }
            )
        board_name = case_name.split("_")[0]
        out.append(
            (
                _normalize_board_token(board_name),
                {
                    "id": f"{_slug(board_name)}::{case_name}",
                    "board": board_name,
                    "case": case_name,
                    "case_type": "label_mapped",
                    "source_file": source_file.as_posix(),
                    "metrics": {
                        "expected_labels": expected,
                        "matched_labels": matched,
                        "label_precision": _safe_float(metrics.get("precision")) if metrics.get("precision") is not None else None,
                        "label_recall": _safe_float(metrics.get("recall")) if metrics.get("recall") is not None else None,
                        "proxy_precision": _safe_div(matched, layout_count),
                        "proxy_recall": _safe_div(matched, expected),
                        "layout_risk_point_count": layout_count if layout_count else None,
                    },
                    "path_recall": {},
                    "labels": labels,
                    "missing_labels": [row for row in labels if not row.get("matched")],
                    "status": "ok" if expected and matched >= expected else "warning",
                },
            )
        )
    return out


def _extract_pathc_rows(payload: Dict[str, Any], source_file: Path) -> List[Tuple[str, Dict[str, Any]]]:
    out: List[Tuple[str, Dict[str, Any]]] = []
    for board_name, board_payload in payload.items():
        if not isinstance(board_payload, dict):
            continue
        match_map = board_payload.get("matches") or {}
        expected = len(match_map)
        matched = sum(1 for rows in match_map.values() if rows)
        layout_count = _safe_int(board_payload.get("layout_risk_point_count"), 0)
        labels = []
        for component, rows in match_map.items():
            first = (rows or [{}])[0]
            labels.append(
                {
                    "id": f"{component}:{_slug(str(first.get('rail') or ''))}",
                    "description": f"path_c match for {component}",
                    "component": component,
                    "rail": first.get("rail") or "",
                    "group": "path_c",
                    "matched": bool(rows),
                    "match_count": len(rows or []),
                    "examples": {"matches": (rows or [])[:4]},
                }
            )
        out.append(
            (
                _normalize_board_token(board_name),
                {
                    "id": f"{_slug(board_name)}::{source_file.stem}",
                    "board": board_name,
                    "case": source_file.stem,
                    "case_type": "path_c_recall",
                    "source_file": source_file.as_posix(),
                    "metrics": {
                        "expected_labels": expected,
                        "matched_labels": matched,
                        "label_precision": None,
                        "label_recall": _safe_div(matched, expected),
                        "proxy_precision": _safe_div(matched, layout_count),
                        "proxy_recall": _safe_div(matched, expected),
                        "layout_risk_point_count": layout_count if layout_count else None,
                    },
                    "path_recall": {"path_c": _safe_div(matched, expected)},
                    "labels": labels,
                    "missing_labels": [row for row in labels if not row.get("matched")],
                    "status": "ok" if expected and matched >= expected else "warning",
                },
            )
        )
    return out


def _extract_archived_recall_rows(payload: Dict[str, Any], source_file: Path) -> List[Tuple[str, Dict[str, Any]]]:
    out: List[Tuple[str, Dict[str, Any]]] = []
    for case_payload in (payload.get("cases") or []):
        if not isinstance(case_payload, dict):
            continue
        if str(case_payload.get("status") or "").lower() != "ok":
            continue
        board_name = str(case_payload.get("board") or "unknown")
        case_name = str(case_payload.get("case") or source_file.stem)
        recall = case_payload.get("recall") or {}
        details = recall.get("details") or []
        labels: List[Dict[str, Any]] = []
        for item in details:
            labels.append(
                {
                    "id": item.get("id") or "",
                    "description": item.get("description") or "",
                    "component": item.get("component") or "",
                    "rail": item.get("rail") or "",
                    "normalized_rail": item.get("normalized_rail") or item.get("rail") or "",
                    "original_rail": item.get("original_rail") or "",
                    "group": item.get("group") or "",
                    "matched": bool(item.get("abc_any_hit")),
                    "match_count": int(bool(item.get("abc_any_hit"))),
                    "unresolved_semantic_label": bool(item.get("unresolved_semantic_label")),
                    "normalization_reason": item.get("normalization_reason") or "",
                    "normalization_confidence": item.get("normalization_confidence") or "",
                    "rlc_component": bool(item.get("rlc_component")),
                    "rlc_power_scope": bool(item.get("rlc_power_scope")),
                    "examples": {
                        "path_a": ((item.get("path_a") or {}).get("risk_classification") or [])[:3],
                        "path_b": ((item.get("path_b") or {}).get("violations") or [])[:3],
                        "path_c": ((item.get("path_c") or {}).get("risk_points") or [])[:3],
                    },
                }
            )
        review = recall.get("review") or {}
        missing = [row for row in labels if (not row.get("matched")) and (not row.get("unresolved_semantic_label"))]
        unresolved = [row for row in labels if row.get("unresolved_semantic_label")]
        metrics = {
            "expected_labels": _safe_int(((recall.get("abc_any_recall") or {}).get("total"))),
            "matched_labels": _safe_int(((recall.get("abc_any_recall") or {}).get("hits"))),
            "label_precision": None,
            "label_recall": _safe_float((recall.get("abc_any_recall") or {}).get("recall")) if (recall.get("abc_any_recall") or {}).get("total") else None,
            "proxy_precision": None,
            "proxy_recall": _safe_float((recall.get("abc_any_recall") or {}).get("recall")) if (recall.get("abc_any_recall") or {}).get("total") else None,
            "layout_risk_point_count": None,
            "rlc_all_hits": _safe_int(((recall.get("rlc_all_recall") or {}).get("hits"))),
            "rlc_all_total": _safe_int(((recall.get("rlc_all_recall") or {}).get("total"))),
            "rlc_all_recall": _safe_float((recall.get("rlc_all_recall") or {}).get("recall")) if (recall.get("rlc_all_recall") or {}).get("total") else None,
            "rlc_power_hits": _safe_int(((recall.get("rlc_power_recall") or {}).get("hits"))),
            "rlc_power_total": _safe_int(((recall.get("rlc_power_recall") or {}).get("total"))),
            "rlc_power_recall": _safe_float((recall.get("rlc_power_recall") or {}).get("recall")) if (recall.get("rlc_power_recall") or {}).get("total") else None,
            "rlc_power_strict_hits": _safe_int(((recall.get("rlc_power_strict_recall") or {}).get("hits"))),
            "rlc_power_strict_total": _safe_int(((recall.get("rlc_power_strict_recall") or {}).get("total"))),
            "rlc_power_strict_recall": _safe_float((recall.get("rlc_power_strict_recall") or {}).get("recall")) if (recall.get("rlc_power_strict_recall") or {}).get("total") else None,
            "rlc_power_medium_hits": _safe_int(((recall.get("rlc_power_medium_recall") or {}).get("hits"))),
            "rlc_power_medium_total": _safe_int(((recall.get("rlc_power_medium_recall") or {}).get("total"))),
            "rlc_power_medium_recall": _safe_float((recall.get("rlc_power_medium_recall") or {}).get("recall")) if (recall.get("rlc_power_medium_recall") or {}).get("total") else None,
            "unresolved_labels": len(unresolved),
        }
        out.append(
            (
                _normalize_board_token(board_name),
                {
                    "id": f"{_slug(board_name)}::{case_name}::{source_file.stem}",
                    "board": board_name,
                    "case": case_name,
                    "case_type": "archived_recall",
                    "source_file": source_file.as_posix(),
                    "metrics": metrics,
                    "path_recall": {
                        "path_a": (recall.get("path_a_recall") or {}).get("recall"),
                        "path_b": (recall.get("path_b_recall") or {}).get("recall"),
                        "path_c": (recall.get("path_c_recall") or {}).get("recall"),
                        "abc_any": (recall.get("abc_any_recall") or {}).get("recall"),
                        "rlc_all": (recall.get("rlc_all_recall") or {}).get("recall"),
                        "rlc_power": (recall.get("rlc_power_recall") or {}).get("recall"),
                        "rlc_power_strict": (recall.get("rlc_power_strict_recall") or {}).get("recall"),
                        "rlc_power_medium": (recall.get("rlc_power_medium_recall") or {}).get("recall"),
                    },
                    "labels": labels,
                    "missing_labels": missing,
                    "unresolved_labels": unresolved,
                    "review_counts": review.get("counts") if isinstance(review.get("counts"), dict) else {"matched": len(labels) - len(missing) - len(unresolved), "missed": len(missing), "unresolved": len(unresolved)},
                    "status": "ok" if not missing else "warning",
                },
            )
        )
    return out


def _scan_benchmark_cases(root: Path) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
    files = _collect_benchmark_files(root)
    stamp = tuple((path.as_posix(), path.stat().st_mtime_ns) for path in files)
    cached = _BENCHMARK_CACHE.get(root.as_posix())
    if cached and cached[0] == stamp:
        return cached[1], cached[2]
    by_board: Dict[str, List[Dict[str, Any]]] = {}
    source_files: List[str] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        rows: List[Tuple[str, Dict[str, Any]]] = []
        if isinstance(payload, dict) and "boards" in payload:
            rows = _extract_hi3519_board_cases(payload, path)
        elif isinstance(payload, dict) and "cases" in payload and "overall" in payload:
            rows = _extract_t1_case_rows(payload, path)
        elif isinstance(payload, dict) and "cases" in payload and "aggregate" in payload:
            rows = _extract_archived_recall_rows(payload, path)
        elif isinstance(payload, dict) and all(isinstance(v, dict) for v in payload.values()):
            rows = _extract_pathc_rows(payload, path)
        if not rows:
            continue
        source_files.append(path.as_posix())
        for board_token, case_payload in rows:
            by_board.setdefault(board_token, []).append(case_payload)
    for board_token, cases in by_board.items():
        cases.sort(key=lambda item: (str(item.get("case_type") or ""), str(item.get("case") or ""), str(item.get("source_file") or "")))
    _BENCHMARK_CACHE[root.as_posix()] = (stamp, by_board, source_files)
    return by_board, source_files


def _normalize_benchmark_payload(report: Dict[str, Any], report_dir: Path) -> Dict[str, Any]:
    board_tokens = _report_board_candidates(report, report_dir)
    by_board, source_files = _scan_benchmark_cases(BENCHMARK_ROOT)
    matched_cases: List[Dict[str, Any]] = []
    for token in board_tokens:
        matched_cases.extend(by_board.get(token, []))
    unique_cases: Dict[str, Dict[str, Any]] = {}
    for case in matched_cases:
        unique_cases[str(case.get("id") or uuid.uuid4().hex)] = case
    cases = list(unique_cases.values())
    if not cases:
        return {
            "has_data": False,
            "join_key": {"candidates": board_tokens, "matched": []},
            "summary": {
                "case_count": 0,
                "label_expected": None,
                "label_matched": None,
                "label_precision": None,
                "label_recall": None,
                "proxy_precision": None,
                "proxy_recall": None,
                "rlc_all_recall": None,
                "rlc_power_recall": None,
                "rlc_power_strict_recall": None,
                "rlc_power_medium_recall": None,
                "rlc_all_labels": None,
                "rlc_power_labels": None,
                "rlc_power_strict_labels": None,
                "rlc_power_medium_labels": None,
                "unresolved_labels": None,
                "target_threshold": 0.75,
                "target_passed": None,
            },
            "source_files": source_files,
            "cases": [],
            "last_generated_at": None,
        }

    expected_sum = 0.0
    matched_sum = 0.0
    predicted_sum = 0.0
    layout_sum = 0.0
    proxy_matched_sum = 0.0
    rlc_all_hits = 0.0
    rlc_all_total = 0.0
    rlc_power_hits = 0.0
    rlc_power_total = 0.0
    rlc_power_strict_hits = 0.0
    rlc_power_strict_total = 0.0
    rlc_power_medium_hits = 0.0
    rlc_power_medium_total = 0.0
    unresolved_total = 0.0
    for case in cases:
        metrics = case.get("metrics") or {}
        expected = _safe_float(metrics.get("expected_labels"), 0.0)
        matched = _safe_float(metrics.get("matched_labels"), 0.0)
        precision = metrics.get("label_precision")
        layout_count = _safe_float(metrics.get("layout_risk_point_count"), 0.0)
        rlc_all_hits += _safe_float(metrics.get("rlc_all_hits"), 0.0)
        rlc_all_total += _safe_float(metrics.get("rlc_all_total"), 0.0)
        rlc_power_hits += _safe_float(metrics.get("rlc_power_hits"), 0.0)
        rlc_power_total += _safe_float(metrics.get("rlc_power_total"), 0.0)
        rlc_power_strict_hits += _safe_float(metrics.get("rlc_power_strict_hits"), 0.0)
        rlc_power_strict_total += _safe_float(metrics.get("rlc_power_strict_total"), 0.0)
        rlc_power_medium_hits += _safe_float(metrics.get("rlc_power_medium_hits"), 0.0)
        rlc_power_medium_total += _safe_float(metrics.get("rlc_power_medium_total"), 0.0)
        unresolved_total += _safe_float(metrics.get("unresolved_labels"), 0.0)
        expected_sum += expected
        matched_sum += matched
        if precision is not None and _safe_float(precision, 0.0) > 0:
            predicted_sum += matched / _safe_float(precision, 1.0)
        if layout_count > 0:
            layout_sum += layout_count
            proxy_matched_sum += matched
    label_precision = _safe_div(matched_sum, predicted_sum) if predicted_sum > 0 else None
    label_recall = _safe_div(matched_sum, expected_sum) if expected_sum > 0 else None
    proxy_precision = _safe_div(proxy_matched_sum, layout_sum) if layout_sum > 0 else None
    proxy_recall = _safe_div(proxy_matched_sum, expected_sum) if expected_sum > 0 else None
    rlc_all_recall = _safe_div(rlc_all_hits, rlc_all_total) if rlc_all_total > 0 else None
    rlc_power_recall = _safe_div(rlc_power_hits, rlc_power_total) if rlc_power_total > 0 else None
    rlc_power_strict_recall = _safe_div(rlc_power_strict_hits, rlc_power_strict_total) if rlc_power_strict_total > 0 else None
    rlc_power_medium_recall = _safe_div(rlc_power_medium_hits, rlc_power_medium_total) if rlc_power_medium_total > 0 else None

    return {
        "has_data": True,
        "join_key": {"candidates": board_tokens, "matched": sorted({_normalize_board_token(case.get('board')) for case in cases})},
        "summary": {
            "case_count": len(cases),
            "label_expected": int(expected_sum) if expected_sum > 0 else None,
            "label_matched": int(matched_sum) if matched_sum > 0 else None,
            "label_precision": label_precision,
            "label_recall": label_recall,
            "proxy_precision": proxy_precision,
            "proxy_recall": proxy_recall,
            "rlc_all_recall": rlc_all_recall,
            "rlc_power_recall": rlc_power_recall,
            "rlc_power_strict_recall": rlc_power_strict_recall,
            "rlc_power_medium_recall": rlc_power_medium_recall,
            "rlc_all_labels": int(rlc_all_total) if rlc_all_total > 0 else None,
            "rlc_power_labels": int(rlc_power_total) if rlc_power_total > 0 else None,
            "rlc_power_strict_labels": int(rlc_power_strict_total) if rlc_power_strict_total > 0 else None,
            "rlc_power_medium_labels": int(rlc_power_medium_total) if rlc_power_medium_total > 0 else None,
            "unresolved_labels": int(unresolved_total) if unresolved_total > 0 else None,
            "target_threshold": 0.75,
            "target_passed": bool(rlc_power_recall is not None and rlc_power_recall >= 0.75),
        },
        "source_files": source_files,
        "cases": cases,
        "last_generated_at": dt.datetime.utcnow().isoformat() + "Z",
    }


def normalize_report(loaded: LoadedReport) -> Dict[str, Any]:
    stat_key = loaded.report_path.as_posix()
    mtime = loaded.report_path.stat().st_mtime_ns
    benchmark_signature = tuple((path.as_posix(), path.stat().st_mtime_ns) for path in _collect_benchmark_files(BENCHMARK_ROOT))
    revision_signature = _revision_signature(loaded.report_dir)
    cached = _SUMMARY_CACHE.get(stat_key)
    if cached and cached[0] == mtime and cached[1] == benchmark_signature and cached[2] == revision_signature:
        return cached[3]
    report = loaded.payload
    report_dir = loaded.report_dir
    pdn = report.get("pdn_evaluation") or {}
    summary = report.get("summary") or {}
    artifacts = report.get("artifacts") or {}
    knowledge = report.get("knowledge_fusion") or pdn.get("knowledge_fusion") or {}
    board = report.get("board") or {}
    runtime = report.get("experiment_runtime") or {}
    local_description_support = report.get("local_description_support") or {}
    ipc_metrics = report.get("ipc_routing_metrics") or {}
    limitations = report.get("limitations") or []
    decision_fusion = pdn.get("decision_fusion") or {}
    cross_validation = decision_fusion.get("cross_validation") or {}
    datastruct = _load_datastruct(report, report_dir)
    benchmark = _normalize_benchmark_payload(report, report_dir)

    path_a_rails = _normalize_rail_keyed_map((pdn.get("path_a") or {}).get("rails") or {})
    path_b_rails = _normalize_rail_keyed_map((pdn.get("path_b") or {}).get("rails") or {})
    path_c_rails = _normalize_rail_keyed_map((pdn.get("path_c") or {}).get("rails") or {})
    path_d_rails = _normalize_rail_keyed_map((pdn.get("path_d") or {}).get("rails") or {})
    path_a_rails, path_b_rails, path_c_rails, path_d_rails, revision_meta_by_rail, override_patch_by_rail, revision_rows = _apply_active_revision_maps(
        loaded,
        dict(path_a_rails),
        dict(path_b_rails),
        dict(path_c_rails),
        dict(path_d_rails),
    )
    path_a_rails = _normalize_rail_keyed_map(path_a_rails)
    path_b_rails = _normalize_rail_keyed_map(path_b_rails)
    path_c_rails = _normalize_rail_keyed_map(path_c_rails)
    path_d_rails = _normalize_rail_keyed_map(path_d_rails)
    revision_meta_by_rail = _normalize_rail_keyed_map(revision_meta_by_rail)
    override_patch_by_rail = _normalize_rail_keyed_map(override_patch_by_rail)
    parameter_audit = _build_parameter_audit(path_a_rails)
    path_a_segment_curve_missing_rails = 0
    for rail_payload in path_a_rails.values():
        segments = rail_payload.get("segments") or []
        if not segments:
            continue
        has_empty = False
        for segment in segments:
            curves = segment.get("impedance_curves") or {}
            current = curves.get("actual_curve_current") or segment.get("actual_curve_current") or []
            if not current:
                has_empty = True
                break
        if has_empty:
            path_a_segment_curve_missing_rails += 1

    rail_names = sorted({*path_a_rails.keys(), *path_b_rails.keys(), *path_c_rails.keys(), *path_d_rails.keys()})
    top_risks = pdn.get("top_risks") or []
    slim_top_risks = []
    for item in top_risks:
        if not item.get("rail"):
            continue
        slim_top_risks.append(
            {
                "rail": _canonical_rail_name(item.get("rail")),
                "risk_level": item.get("risk_level"),
                "final_score": _safe_float(item.get("final_score")),
                "operating_band_adjust": _safe_float(item.get("operating_band_adjust")),
                "review_required": bool(item.get("review_required")),
                "evidence": [_evidence_summary(evidence) for evidence in _coerce_list(item.get("evidence"))[:8]],
                "recommended_actions": [_action_summary(action) for action in _coerce_list(item.get("recommended_actions"))[:8]],
            }
        )
    risk_lookup = {_canonical_rail_name(item.get("rail")): item for item in slim_top_risks if item.get("rail")}
    top_passive = report.get("top_passive_findings") or []

    rails: List[Dict[str, Any]] = []
    for rail in rail_names:
        rail_a = path_a_rails.get(rail) or {}
        rail_b = path_b_rails.get(rail) or {}
        rail_c = path_c_rails.get(rail) or {}
        rail_d = path_d_rails.get(rail) or {}
        violations = (rail_b.get("violations") or [])[:50]
        gap = rail_a.get("curve_gap_summary") or {}
        risk = risk_lookup.get(rail, {})
        passive_findings = [item for item in top_passive if _canonical_rail_name(item.get("rail")) == rail]
        actions = _build_actions(risk, rail_a, rail_b, rail_c)
        rails.append(
            {
                "id": _slug(rail),
                "rail": _canonical_rail_name(rail),
                "display_name": _canonical_rail_name(rail_a.get("display_name") or rail),
                "kind": rail_a.get("kind") or rail_b.get("kind") or rail_c.get("kind") or "rail",
                "risk_level": str(risk.get("risk_level") or "info"),
                "final_score": _safe_float(risk.get("final_score")),
                "review_required": bool(risk.get("review_required")),
                "current_worst_ratio": _safe_float(gap.get("current_worst_ratio")),
                "operating_band_adjust": _safe_float(risk.get("operating_band_adjust")),
                "focus_band_summary": (rail_a.get("actual_board_response") or {}).get("focus_band_summary") or rail_a.get("focus_band_summary") or {},
                "harmonic_focus_summary": (rail_a.get("actual_board_response") or {}).get("harmonic_focus_summary") or rail_a.get("harmonic_focus_summary") or {},
                "stage1_worst_ratio": _safe_float(gap.get("stage1_worst_ratio")),
                "stage2_worst_ratio": _safe_float(gap.get("stage2_worst_ratio")),
                "baseline_worst_ratio": _safe_float(gap.get("baseline_worst_ratio")),
                "peak_z": _safe_float((rail_a.get("actual_board_response") or {}).get("peak_z")),
                "lost_band_count": _safe_int((rail_a.get("actual_board_response") or {}).get("lost_band_count")),
                "severity": rail_a.get("severity") or rail_b.get("severity") or risk.get("risk_level"),
                "required_caps_count": len(rail_a.get("required_caps") or []),
                "redundant_caps_count": len(rail_a.get("redundant_caps_selected") or []),
                "violations_count": len(rail_b.get("violations") or []),
                "datasheet_findings_count": len(rail_c.get("datasheet_findings") or []),
                "device_library_findings_count": len(rail_c.get("device_library_findings") or []),
                "action_count": len(actions),
                "critical_caps": rail_a.get("critical_caps") or [],
                "load_refs": rail_a.get("load_refs") or [],
                "dominant_load_refs": rail_a.get("dominant_load_refs") or [],
                "dominant_missing_bands": rail_a.get("dominant_missing_bands") or [],
                "passive_findings": passive_findings,
                "actions": actions,
                "evidence": [_evidence_summary(evidence) for evidence in _coerce_list(risk.get("evidence"))[:8]],
                "images": {
                    "curve": _artifact_url(report_dir, (artifacts.get("curve_svgs") or {}).get(rail)),
                    "layout": _artifact_url(report_dir, (artifacts.get("rail_layout_svgs") or {}).get(rail)),
                },
                "staged_strategy": rail_a.get("staged_strategy") or {},
                "global_stage_summary": rail_a.get("global_stage_summary") or {},
                "segment_stage_summaries": (rail_a.get("segment_stage_summaries") or [])[:30],
                "global_regression_guard": rail_a.get("global_regression_guard") or {},
                "revision_meta": revision_meta_by_rail.get(rail, {"revision_id": "original", "kind": "original", "active": True}),
            }
        )

    rails.sort(key=lambda item: (-item["final_score"], -item["current_worst_ratio"], item["rail"]))

    operating_band_rails = sum(1 for item in rails if item.get("focus_band_summary"))
    harmonic_violation_rails = sum(
        1 for item in rails if _safe_int((item.get("harmonic_focus_summary") or {}).get("violating_harmonic_count")) > 0
    )
    switching_freq_backed_rails = sum(
        1
        for item in rails
        if _safe_float((item.get("focus_band_summary") or {}).get("switching_freq_hz"))
        or _safe_float((item.get("harmonic_focus_summary") or {}).get("switching_freq_hz"))
    )

    overview_cards = [
        {"label": "PDN 状态", "value": str(pdn.get("status") or summary.get("status") or "-"), "tone": str(pdn.get("status") or "info")},
        {"label": "风险 Rail", "value": len(top_risks), "tone": "critical" if top_risks else "ok"},
        {"label": "总 Rail 数", "value": len(rail_names), "tone": "neutral"},
        {"label": "关键被动问题", "value": len(top_passive), "tone": "warning" if top_passive else "ok"},
        {"label": "真实参数提取", "value": parameter_audit["counts"].get("datasheet", 0), "tone": "ok" if parameter_audit["counts"].get("datasheet", 0) else "medium"},
        {"label": "Work-Band Rails", "value": operating_band_rails, "tone": "ok" if operating_band_rails else "neutral"},
        {"label": "Switch-Freq Rails", "value": switching_freq_backed_rails, "tone": "ok" if switching_freq_backed_rails else "neutral"},
        {"label": "Harmonic Violations", "value": harmonic_violation_rails, "tone": "critical" if harmonic_violation_rails else "ok"},
        {"label": "器件库覆盖", "value": f"{_safe_int(knowledge.get('db_backed_components'))}/{_safe_int(knowledge.get('component_count'))}", "tone": "neutral"},
        {"label": "RAG 覆盖", "value": _safe_int(knowledge.get("rag_backed_components")), "tone": "neutral"},
    ]

    board_gallery_keys = [
        ("placement_image", "Board Placement"),
        ("coverage_svg", "Coverage"),
        ("ipc_global_svg", "IPC Global"),
        ("ipc_power_focus_svg", "IPC Power Focus"),
        ("ipc_hs_focus_svg", "IPC High-Speed Focus"),
        ("pdn_summary_svg", "PDN Summary"),
        ("pdn_decap_distance_svg", "Decap Distance"),
        ("pdn_impedance_loss_svg", "Impedance Loss"),
    ]
    board_gallery = [
        {"key": key, "label": label, "url": _artifact_url(report_dir, artifacts.get(key))}
        for key, label in board_gallery_keys
        if artifacts.get(key)
    ]

    for rail in rails:
        related_refs = set()
        related_refs.update(rail["critical_caps"])
        related_refs.update(rail["load_refs"])
        related_refs.update(rail["dominant_load_refs"])
        for violation in rail.get("violations") or []:
            for key in ("cap_ref", "load_ref", "from_ref", "to_ref", "ref"):
                value = violation.get(key)
                if value:
                    related_refs.add(str(value))
        related_nets = []
        net_entry = (datastruct.get("net_index") or {}).get(_normalize_token(rail["rail"]))
        if net_entry:
            related_nets.append(net_entry)
            related_refs.update(net_entry.get("refs") or [])
        rail["layout_focus"] = {
            "related_refs": sorted(ref for ref in related_refs if ref in (datastruct.get("component_index") or {})),
            "related_net_ids": [item.get("net_id") for item in related_nets],
        }

    decision_fusion_summary = {
        "risk_pool_size": decision_fusion.get("risk_pool_size"),
        "weights": decision_fusion.get("weights"),
        "cross_validation": cross_validation,
    }

    summary_payload = dict(summary or {})
    summary_payload["manual_revision_count"] = len(revision_rows)
    summary_payload["active_manual_rails_count"] = len({row.get("rail") for row in revision_rows})
    normalized = {
        "meta": {
            "report_path": loaded.report_path.as_posix(),
            "report_dir": loaded.report_dir.as_posix(),
            "title": report_dir.name,
            "timestamp": report.get("timestamp"),
        },
        "summary": summary_payload,
        "board": board,
        "runtime": runtime,
        "ipc_metrics": ipc_metrics,
        "local_description_support": local_description_support,
        "limitations": limitations,
        "datastruct": {
            "path": datastruct.get("path"),
            "board": datastruct.get("board") or {},
            "component_count": len(datastruct.get("components") or []),
            "pin_count": sum(_safe_int(item.get("pin_count")) for item in (datastruct.get("components") or [])),
            "net_count": len(datastruct.get("nets") or []),
        },
        "overview_cards": overview_cards,
        "knowledge_fusion": knowledge,
        "parameter_audit": parameter_audit,
        "decision_fusion": decision_fusion_summary,
        "cross_validation": cross_validation,
        "benchmark": benchmark,
        "operating_band_summary": {
            "work_band_rails": operating_band_rails,
            "switching_freq_rails": switching_freq_backed_rails,
            "harmonic_violation_rails": harmonic_violation_rails,
        },
        "diagnostics": {
            "path_a_segment_curve_missing_rails": path_a_segment_curve_missing_rails,
        },
        "revision_status": {
            "rows": revision_rows[:200],
            "override_rails": sorted(override_patch_by_rail.keys()),
        },
        "top_passive_findings": top_passive,
        "top_risks": slim_top_risks,
        "rails": rails,
        "board_gallery": board_gallery,
        "artifacts": {
            key: _normalize_artifact_value(report_dir, value)
            for key, value in artifacts.items()
        },
        "raw_keys": sorted(report.keys()),
    }
    _SUMMARY_CACHE[stat_key] = (mtime, benchmark_signature, revision_signature, normalized)
    return normalized


def normalize_rail_detail(loaded: LoadedReport, rail_name: str) -> Dict[str, Any]:
    rail_name = _canonical_rail_name(rail_name)
    stat_key = loaded.report_path.as_posix()
    mtime = loaded.report_path.stat().st_mtime_ns
    revision_signature = _revision_signature(loaded.report_dir)
    cache_key = (stat_key, rail_name)
    cached = _RAIL_DETAIL_CACHE.get(cache_key)
    if cached and cached[0] == mtime and cached[1] == revision_signature:
        return cached[2]
    report = loaded.payload
    report_dir = loaded.report_dir
    datastruct = _load_datastruct(report, report_dir)
    ipc_geometry = _load_ipc_geometry(report, report_dir)
    pdn = report.get("pdn_evaluation") or {}
    artifacts = report.get("artifacts") or {}
    top_passive = report.get("top_passive_findings") or []
    top_risks = pdn.get("top_risks") or []
    risk = next((item for item in top_risks if _canonical_rail_name(item.get("rail")) == rail_name), {})
    path_a_rails = _normalize_rail_keyed_map(((pdn.get("path_a") or {}).get("rails") or {}))
    path_b_rails = _normalize_rail_keyed_map(((pdn.get("path_b") or {}).get("rails") or {}))
    path_c_rails = _normalize_rail_keyed_map(((pdn.get("path_c") or {}).get("rails") or {}))
    path_d_rails = _normalize_rail_keyed_map(((pdn.get("path_d") or {}).get("rails") or {}))
    path_a_rails, path_b_rails, path_c_rails, path_d_rails, revision_meta_by_rail, override_patch_by_rail, _ = _apply_active_revision_maps(
        loaded,
        path_a_rails,
        path_b_rails,
        path_c_rails,
        path_d_rails,
    )
    path_a_rails = _normalize_rail_keyed_map(path_a_rails)
    path_b_rails = _normalize_rail_keyed_map(path_b_rails)
    path_c_rails = _normalize_rail_keyed_map(path_c_rails)
    path_d_rails = _normalize_rail_keyed_map(path_d_rails)
    revision_meta_by_rail = _normalize_rail_keyed_map(revision_meta_by_rail)
    override_patch_by_rail = _normalize_rail_keyed_map(override_patch_by_rail)
    rail_a = path_a_rails.get(rail_name) or {}
    rail_b = path_b_rails.get(rail_name) or {}
    rail_c = path_c_rails.get(rail_name) or {}
    rail_d = path_d_rails.get(rail_name) or {}
    net_entry = (datastruct.get("net_index") or {}).get(_normalize_token(rail_name)) or {}
    impedance_curves = rail_a.get("impedance_curves") or {}
    parameter_provenance = _build_parameter_provenance(
        rail_name,
        rail_a,
        rail_a.get("rail_requirement_sources") or [],
        rail_a.get("datasheet_decap_requirements") or [],
    )
    region_views = _normalize_region_views(rail_name, rail_a, rail_b, rail_c)
    related_refs = sorted({ref for region in region_views for ref in (region.get("related_refs") or []) if ref in (datastruct.get("component_index") or {})})
    layout_geometry = (
        _build_ipc_geometry_bundle(related_refs, ipc_geometry, rail_name)
        if ipc_geometry
        else _build_geometry_bundle(related_refs, datastruct.get("component_index") or {}, net_entry)
    ) if (net_entry or ipc_geometry) else {"component_refs": related_refs, "components": [], "bounds": {}, "net_segments": []}
    for region in region_views:
        region["geometry"] = (
            _build_ipc_geometry_bundle(region.get("related_refs") or [], ipc_geometry, rail_name)
            if ipc_geometry
            else _build_geometry_bundle(region.get("related_refs") or [], datastruct.get("component_index") or {}, net_entry)
        ) if (net_entry or ipc_geometry) else {"component_refs": region.get("related_refs") or [], "components": [], "bounds": {}, "net_segments": []}

    def _slim_curve(points: Any, limit: int = 220) -> List[Dict[str, Any]]:
        rows = list(points or [])
        if len(rows) <= limit:
            selected = rows
        else:
            step = max(1, len(rows) // limit)
            selected = rows[::step]
            if rows[-1] is not selected[-1]:
                selected.append(rows[-1])
        return [{"f_hz": _safe_float(item.get("f_hz")), "z_ohm": _safe_float(item.get("z_ohm"))} for item in selected]

    detail = {
        "rail": rail_name,
        "selection_trace": _slim_selection_trace(rail_a.get("selection_trace") or []),
        "focus_band": rail_a.get("focus_band") or {},
        "focus_band_summary": (rail_a.get("actual_board_response") or {}).get("focus_band_summary") or rail_a.get("focus_band_summary") or {},
        "harmonic_focus_summary": (rail_a.get("actual_board_response") or {}).get("harmonic_focus_summary") or rail_a.get("harmonic_focus_summary") or {},
        "operating_band_adjust": _safe_float(risk.get("operating_band_adjust") or rail_a.get("operating_band_priority_adjustment")),
        "segments": {
            "path_a": [_slim_segment(item, "path_a") for item in (rail_a.get("segments") or [])[:60]],
            "path_b": [_slim_segment(item, "path_b") for item in (rail_b.get("segments") or [])[:60]],
            "path_c": [_slim_segment(item, "path_c") for item in (rail_c.get("segments") or [])[:60]],
            "path_d": [_slim_segment(item, "path_d") for item in (rail_d.get("segments") or [])[:60]],
        },
        "loop_esl_breakdown": {
            "current": (rail_a.get("loop_esl_breakdown_current") or [])[:20],
            "stage1": (rail_a.get("loop_esl_breakdown_stage1") or [])[:20],
            "stage2": (rail_a.get("loop_esl_breakdown_stage2") or [])[:20],
        },
        "violations": [_slim_violation(item) for item in (rail_b.get("violations") or [])[:80]],
        "passive_findings": [item for item in top_passive if _canonical_rail_name(item.get("rail")) == rail_name],
        "actions": _build_actions(risk, rail_a, rail_b, rail_c),
        "evidence": [_evidence_summary(evidence) for evidence in _coerce_list(risk.get("evidence"))[:12]],
        "layout_focus": {
            "related_net_ids": [net_entry.get("net_id")] if net_entry.get("net_id") else [],
            "related_nets": [net_entry] if net_entry else [],
            "geometry": layout_geometry,
        },
        "curves": {
            "target": _slim_curve(impedance_curves.get("target_curve") or rail_a.get("target_curve")),
            "baseline": _slim_curve(impedance_curves.get("actual_curve_baseline") or rail_a.get("actual_curve_baseline")),
            "current": _slim_curve(impedance_curves.get("actual_curve_current") or rail_a.get("actual_curve_current")),
            "stage1": _slim_curve(impedance_curves.get("actual_curve_stage1") or rail_a.get("actual_curve_stage1")),
            "stage2": _slim_curve(impedance_curves.get("actual_curve_stage2") or rail_a.get("actual_curve_stage2")),
        },
        "images": {
            "curve": _artifact_url(report_dir, (artifacts.get("curve_svgs") or {}).get(rail_name)),
            "layout": _artifact_url(report_dir, (artifacts.get("rail_layout_svgs") or {}).get(rail_name)),
        },
        "electrical_parameters": parameter_provenance["facts"],
        "electrical_parameter_summary": parameter_provenance.get("summary") or [],
        "electrical_parameter_evidence": parameter_provenance.get("evidence") or parameter_provenance["facts"],
        "source_documents": parameter_provenance["source_documents"],
        "region_views": region_views,
        "staged_strategy": rail_a.get("staged_strategy") or {},
        "global_stage_summary": rail_a.get("global_stage_summary") or {},
        "segment_stage_summaries": (rail_a.get("segment_stage_summaries") or [])[:80],
        "global_regression_guard": rail_a.get("global_regression_guard") or {},
        "revision_meta": revision_meta_by_rail.get(rail_name, {"revision_id": "original", "kind": "original", "active": True}),
        "override_parameters": override_patch_by_rail.get(rail_name, {}),
        "rerun_trace": ((_active_revision_for_rail(report_dir, rail_name)[1] or {}).get("rerun_trace") or []),
    }
    _RAIL_DETAIL_CACHE[cache_key] = (mtime, revision_signature, detail)
    return detail


class ReportViewerHandler(SimpleHTTPRequestHandler):
    server_version = "PlagentReportViewer/1.0"

    def __init__(self, *args: Any, static_dir: Path, report_target: Path, **kwargs: Any) -> None:
        self.static_dir = static_dir
        self.report_target = report_target
        super().__init__(*args, directory=str(static_dir), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/reports":
            self._send_reports()
            return
        if parsed.path == "/api/report":
            self._send_json_report()
            return
        if parsed.path == "/api/health":
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/file":
            self._send_file(parsed.query)
            return
        if parsed.path == "/api/rail-detail":
            self._send_rail_detail(parsed.query)
            return
        if parsed.path == "/api/layout-geometry":
            self._send_layout_geometry(parsed.query)
            return
        if parsed.path == "/api/raw":
            self._send_raw_node(parsed.query)
            return
        if parsed.path == "/api/raw-search":
            self._send_raw_search(parsed.query)
            return
        if parsed.path == "/api/rail-overrides":
            self._send_rail_overrides(parsed.query)
            return
        if parsed.path in {"/", ""}:
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/rail-overrides":
            self._post_rail_overrides(parsed.query)
            return
        if parsed.path == "/api/rail-rerun":
            self._post_rail_rerun(parsed.query)
            return
        if parsed.path == "/api/rail-revision-activate":
            self._post_rail_revision_activate(parsed.query)
            return
        if parsed.path == "/api/rail-revision-rollback":
            self._post_rail_revision_rollback(parsed.query)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")

    def _send_json_report(self) -> None:
        try:
            selection = parse_qs(urlparse(self.path).query).get("report", [""])[0]
            payload = normalize_report(load_report(resolve_report_selection(self.report_target, selection)))
        except Exception as exc:  # pragma: no cover
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"))
            return
        self._send_json(payload)

    def _send_json(self, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, query: str) -> None:
        path_value = parse_qs(query).get("path", [""])[0]
        if not path_value:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing path query")
            return
        file_path = _path_from_payload(path_value)
        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Artifact not found")
            return
        mime_type, _ = mimetypes.guess_type(str(file_path))
        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_rail_detail(self, query: str) -> None:
        rail_name = parse_qs(query).get("rail", [""])[0]
        selection = parse_qs(query).get("report", [""])[0]
        self._send_json(normalize_rail_detail(load_report(resolve_report_selection(self.report_target, selection)), rail_name))

    def _send_layout_geometry(self, query: str) -> None:
        selection = parse_qs(query).get("report", [""])[0]
        self._send_json(build_layout_geometry(load_report(resolve_report_selection(self.report_target, selection))))

    def _send_raw_node(self, query: str) -> None:
        pointer = parse_qs(query).get("path", [""])[0]
        selection = parse_qs(query).get("report", [""])[0]
        payload = _read_raw_report(resolve_report_selection(self.report_target, selection))
        node = _resolve_pointer(payload, pointer) if pointer else payload
        response = {"pointer": pointer or "/", **_describe_node(node)}
        if isinstance(node, dict):
            response["children"] = [
                {"key": key, "pointer": _child_pointer(pointer, str(key)), **_describe_node(value)}
                for key, value in node.items()
            ]
        elif isinstance(node, list):
            response["children"] = [
                {"key": str(idx), "pointer": _child_pointer(pointer, str(idx)), **_describe_node(value)}
                for idx, value in enumerate(node[:200])
            ]
            response["truncated"] = len(node) > 200
        self._send_json(response)

    def _send_raw_search(self, query: str) -> None:
        term = parse_qs(query).get("q", [""])[0].strip()
        selection = parse_qs(query).get("report", [""])[0]
        payload = _read_raw_report(resolve_report_selection(self.report_target, selection))
        if not term:
            self._send_json({"matches": []})
            return
        self._send_json({"matches": _search_json(payload, term)})

    def _send_reports(self) -> None:
        reports = [_report_listing_entry(self.report_target, path) for path in list_report_files(self.report_target)]
        reports.sort(key=lambda item: (item.get("group") != "evaluation", item.get("group"), item.get("family"), item.get("board"), item.get("id")))
        self._send_json({"reports": reports})

    def _read_json_body(self) -> Dict[str, Any]:
        raw_length = int(self.headers.get("Content-Length") or 0)
        if raw_length <= 0:
            return {}
        raw = self.rfile.read(raw_length)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _send_rail_overrides(self, query: str) -> None:
        params = parse_qs(query)
        rail_name = params.get("rail", [""])[0]
        selection = params.get("report", [""])[0]
        if not rail_name:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing rail")
            return
        loaded = load_report(resolve_report_selection(self.report_target, selection))
        detail = normalize_rail_detail(loaded, rail_name)
        index_payload = _load_rail_revision_index(loaded.report_dir, rail_name)
        active_meta, active_payload = _active_revision_for_rail(loaded.report_dir, rail_name)
        missing_parameters = [
            {
                "parameter": item.get("parameter"),
                "label": item.get("label"),
                "unit": item.get("unit"),
            }
            for item in (detail.get("electrical_parameters") or [])
            if str(item.get("classification") or "") == "missing"
        ]
        effective_parameters = {}
        for item in (detail.get("electrical_parameter_summary") or []):
            parameter = str(item.get("parameter") or "").strip()
            if not parameter:
                continue
            effective_parameters[parameter] = item.get("value")
        self._send_json(
            {
                "rail": rail_name,
                "active_revision": active_meta,
                "active_override_patch": (active_payload or {}).get("override_patch") or {},
                "effective_parameters": effective_parameters,
                "revisions": index_payload.get("revisions") or [],
                "missing_parameters": missing_parameters,
                "rerun_trace": (active_payload or {}).get("rerun_trace") or [],
            }
        )

    def _post_rail_overrides(self, query: str) -> None:
        params = parse_qs(query)
        selection = params.get("report", [""])[0]
        payload = self._read_json_body()
        rail_name = str(payload.get("rail") or "")
        if not rail_name:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing rail")
            return
        loaded = load_report(resolve_report_selection(self.report_target, selection))
        index_payload = _load_rail_revision_index(loaded.report_dir, rail_name)
        active_meta, active_payload = _active_revision_for_rail(loaded.report_dir, rail_name)
        base_patch = (active_payload or {}).get("override_patch") if active_meta.get("kind") == "manual" else {}
        incoming_patch = _normalize_override_patch(payload.get("override_patch") if isinstance(payload.get("override_patch"), dict) else {})
        merged_patch = {**(base_patch or {}), **incoming_patch}
        revision_id = _new_revision_id()
        note = str(payload.get("note") or "").strip() or "manual override"
        operator = str(payload.get("operator") or "frontend_user")
        created_at = dt.datetime.utcnow().isoformat() + "Z"
        meta = {
            "revision_id": revision_id,
            "kind": "manual",
            "created_at": created_at,
            "note": note,
            "operator": operator,
            "parent_revision_id": active_meta.get("revision_id") or "original",
            "algorithm_version": "viewer-local-rerun",
            "patch_hash": _patch_hash(merged_patch),
        }
        revision_payload = {
            "revision_id": revision_id,
            "rail": rail_name,
            "kind": "manual",
            "created_at": created_at,
            "parent_revision_id": meta["parent_revision_id"],
            "note": note,
            "operator": operator,
            "override_patch": merged_patch,
            "rerun_result": None,
            "rerun_trace": [],
        }
        _write_json_file(_revision_payload_path(loaded.report_dir, rail_name, revision_id), revision_payload)
        rows = [item for item in (index_payload.get("revisions") or []) if str(item.get("revision_id") or "") != revision_id]
        rows.append(meta)
        index_payload["revisions"] = rows
        index_payload["active_revision_id"] = revision_id
        _save_rail_revision_index(loaded.report_dir, rail_name, index_payload)
        self._send_json({"ok": True, "rail": rail_name, "active_revision_id": revision_id, "revision": meta})

    def _post_rail_rerun(self, query: str) -> None:
        params = parse_qs(query)
        selection = params.get("report", [""])[0]
        payload = self._read_json_body()
        rail_name = str(payload.get("rail") or "")
        if not rail_name:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing rail")
            return
        revision_id = str(payload.get("revision_id") or "")
        loaded = load_report(resolve_report_selection(self.report_target, selection))
        index_payload = _load_rail_revision_index(loaded.report_dir, rail_name)
        if not revision_id:
            revision_id = str(index_payload.get("active_revision_id") or "original")
        if revision_id == "original":
            self.send_error(HTTPStatus.BAD_REQUEST, "Original revision cannot be rerun directly")
            return
        revision_payload = _load_revision_payload(loaded.report_dir, rail_name, revision_id)
        if not revision_payload:
            self.send_error(HTTPStatus.NOT_FOUND, "Revision not found")
            return
        patch = _normalize_override_patch(revision_payload.get("override_patch") if isinstance(revision_payload.get("override_patch"), dict) else {})
        rerun_result = _run_rail_abc_rerun(loaded, rail_name, patch)
        trace = list(revision_payload.get("rerun_trace") or [])
        trace.append(
            {
                "rerun_at": dt.datetime.utcnow().isoformat() + "Z",
                "algorithm_version": "viewer-local-rerun",
                "patch_hash": _patch_hash(patch),
                "duration_ms": _safe_int(rerun_result.get("duration_ms")),
                "strategy_mode": str(rerun_result.get("strategy_mode") or "global_local_hybrid"),
                "guard_applied": bool(rerun_result.get("guard_applied")),
                "summary_delta": rerun_result.get("summary_delta") or {},
            }
        )
        revision_payload["rerun_result"] = rerun_result
        revision_payload["rerun_trace"] = trace[-32:]
        _write_json_file(_revision_payload_path(loaded.report_dir, rail_name, revision_id), revision_payload)
        index_payload["active_revision_id"] = revision_id
        _save_rail_revision_index(loaded.report_dir, rail_name, index_payload)
        self._send_json(
            {
                "ok": True,
                "rail": rail_name,
                "revision_id": revision_id,
                "changed": bool((rerun_result.get("summary_delta") or {}).get("changed")),
                "rerun_result": rerun_result,
                "rerun_trace": revision_payload["rerun_trace"],
            }
        )

    def _post_rail_revision_activate(self, query: str) -> None:
        params = parse_qs(query)
        selection = params.get("report", [""])[0]
        payload = self._read_json_body()
        rail_name = str(payload.get("rail") or "")
        revision_id = str(payload.get("revision_id") or "")
        if not rail_name or not revision_id:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing rail or revision_id")
            return
        loaded = load_report(resolve_report_selection(self.report_target, selection))
        index_payload = _load_rail_revision_index(loaded.report_dir, rail_name)
        known_ids = {"original", *[str(item.get("revision_id") or "") for item in (index_payload.get("revisions") or [])]}
        if revision_id not in known_ids:
            self.send_error(HTTPStatus.NOT_FOUND, "Revision not found")
            return
        index_payload["active_revision_id"] = revision_id
        _save_rail_revision_index(loaded.report_dir, rail_name, index_payload)
        self._send_json({"ok": True, "rail": rail_name, "active_revision_id": revision_id})

    def _post_rail_revision_rollback(self, query: str) -> None:
        params = parse_qs(query)
        selection = params.get("report", [""])[0]
        payload = self._read_json_body()
        rail_name = str(payload.get("rail") or "")
        if not rail_name:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing rail")
            return
        loaded = load_report(resolve_report_selection(self.report_target, selection))
        index_payload = _load_rail_revision_index(loaded.report_dir, rail_name)
        index_payload["active_revision_id"] = "original"
        _save_rail_revision_index(loaded.report_dir, rail_name, index_payload)
        self._send_json({"ok": True, "rail": rail_name, "active_revision_id": "original"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def create_server(host: str, port: int, report_target: Path) -> ThreadingHTTPServer:
    def handler(*args: Any, **kwargs: Any) -> ReportViewerHandler:
        return ReportViewerHandler(*args, static_dir=FRONTEND_STATIC_DIR, report_target=report_target, **kwargs)

    return ThreadingHTTPServer((host, port), handler)


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch the interactive evaluation report viewer.")
    parser.add_argument("target", nargs="?", default=str(DEFAULT_REPORT_ROOT), help="Report directory or numeric_eval_report.json file.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host.")
    parser.add_argument("--port", type=int, default=8765, help="Bind port.")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    report_target = Path(args.target)
    report_file = find_report_file(report_target)
    server = create_server(args.host, args.port, report_target)
    print(f"Serving report viewer for {report_file}")
    print(f"Open http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
