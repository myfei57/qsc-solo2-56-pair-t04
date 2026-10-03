"""凝露链重放。

重放回答两个问题：

1. **重启恢复**：链上采样逐条喂回全新的传感器监控与风险引擎，恢复每个采集
   节拍当时的窗口、趋势和风险等级；HOLD 事件折叠出摘除表；工单是当前态文档
   （``wipe/*.json``，原子写），重启直接读。
2. **事后复核**：用链上冻结的画像（不是当前画像）和判据代码重新计算每个
   FACT，与当时落盘的 FACT 逐字段比对。任何不一致（历史被改、判据换版导致
   结论漂移）都进 ``fact_mismatches``；配合哈希链的篡改校验，"当时凭什么报了
   风险"可以被任何人、在任何时间重新验证。

重放是确定性的：同样的链 + 同样的判据版本，必然得到同样的风险轨迹。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..store import JsonStore
from .chain import (
    CHAIN_FACT,
    CHAIN_HOLD,
    CHAIN_SAMPLE,
    ChainJournal,
    ChainVerifyError,
    Entry,
    verify_entries,
)
from .config import CondensationConfig, CondensationProfile, profile_from_dict
from .engine import DoorEnvEngine
from .hold import HOLD_RISK, Hold, HoldRegistry
from .sensors import (
    HEALTH_STALE,
    DoorEnvInput,
    Reading,
    SensorMonitor,
    _SensorState,
)

# 复算 FACT 时必须与历史一致的字段（测量量、判据输出、版本、状态迁移）。
COMPARE_FIELDS = (
    "level",
    "input_status",
    "margin_c",
    "air_t",
    "rh",
    "surface_t",
    "dew_point_c",
    "slope_c_per_min",
    "worsening",
    "dwell_required_s",
    "profile_version",
    "criterion_version",
    "engine_version",
    "changed",
    "previous_level",
)


@dataclass
class ReplayResult:
    """一次重放的结果。"""

    entries: list[Entry] = field(default_factory=list)
    errors: list[ChainVerifyError] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    fact_mismatches: list[dict] = field(default_factory=list)
    sample_count: int = 0
    fact_count: int = 0
    tick_count: int = 0
    levels: list[dict] = field(default_factory=list)
    last_inputs: dict[str, DoorEnvInput] = field(default_factory=dict)
    monitor: SensorMonitor | None = None
    engines: dict[str, DoorEnvEngine] = field(default_factory=dict)
    holds: HoldRegistry | None = None

    @property
    def ok(self) -> bool:
        return not self.errors and not self.fact_mismatches

    def as_dict(self) -> dict:
        return {
            "entries": len(self.entries),
            "chain_ok": not self.errors,
            "errors": [item.as_dict() for item in self.errors],
            "sample_count": self.sample_count,
            "fact_count": self.fact_count,
            "fact_mismatches": self.fact_mismatches,
            "levels": self.levels,
            "ok": self.ok,
        }


def _profile_of_recorded(recorded: dict, config: CondensationConfig) -> CondensationProfile:
    """取判定当时冻结的画像；链上画像损坏时回落按时间选画像并标记不了篡改
    （哈希校验会先抓到内容被改）。"""

    profile_payload = recorded.get("profile")
    if isinstance(profile_payload, dict) and profile_payload:
        try:
            return profile_from_dict(profile_payload)
        except (TypeError, ValueError):
            pass
    return config.profile(float(recorded.get("ts", 0.0)))


def _restore_monitor_states(monitor: SensorMonitor, states: dict) -> None:
    """从链上 SAMPLE 冻结的传感器状态快照恢复监控内部态。"""

    for sensor_id, snapshot in states.items():
        state = _SensorState(sensor_id, str(snapshot.get("kind", "")))
        state.last_ts = snapshot.get("last_ts")
        state.last_value = snapshot.get("last_value")
        state.stuck_count = int(snapshot.get("stuck_count", 0))
        state.suspect = bool(snapshot.get("suspect", False))
        state.suspect_reason = str(snapshot.get("suspect_reason", ""))
        state.good_streak = int(snapshot.get("good_streak", 0))
        state.last_status = str(snapshot.get("last_status", HEALTH_STALE))
        monitor._states[sensor_id] = state


def _input_from_payload(psd_id: str, ts: float, payload: dict) -> DoorEnvInput:
    """把链上冻结的解析输入还原为引擎可吃的 DoorEnvInput。"""

    from .sensors import INPUT_OK, INPUT_UNKNOWN, SensorHealth

    health = [
        SensorHealth(
            sensor_id=str(item.get("sensor_id", "")),
            kind=str(item.get("kind", "")),
            status=str(item.get("status", "")),
            usable=bool(item.get("usable", False)),
            value=item.get("value"),
            detail=str(item.get("detail", "")),
        )
        for item in payload.get("sensor_health", [])
    ]
    return DoorEnvInput(
        psd_id=psd_id,
        ts=ts,
        usable=bool(payload.get("usable", False)),
        status=str(payload.get("status", INPUT_UNKNOWN)),
        air_t=payload.get("air_t"),
        rh=payload.get("rh"),
        surface_t=payload.get("surface_t"),
        dew_point_c=payload.get("dew_point_c"),
        margin_c=payload.get("margin_c"),
        reasons=list(payload.get("reasons", [])),
        health=health,
        drift_ids=list(payload.get("drift_ids", [])),
    )


def replay_chain(
    chain: ChainJournal,
    config: CondensationConfig,
    bindings: dict[str, dict[str, list[str]]] | None = None,
) -> ReplayResult:
    """全链重放：哈希校验 → 采样重建 → 判据复算比对 → 摘除表折叠。"""

    entries = chain.read_all()
    result = ReplayResult(entries=entries, errors=verify_entries(entries))
    monitor = SensorMonitor(config.profile)
    engines: dict[str, DoorEnvEngine] = {}

    for entry in entries:
        if entry.kind == CHAIN_SAMPLE:
            result.sample_count += 1
            result.tick_count += 1
            payload = entry.payload.get("input", {})
            states = entry.payload.get("sensor_states", {})
            if states:
                _restore_monitor_states(monitor, states)
            env_input = _input_from_payload(entry.psd_id, entry.ts, payload)
            result.last_inputs[entry.psd_id] = env_input
            continue

        if entry.kind == CHAIN_FACT:
            result.fact_count += 1
            recorded = entry.payload.get("fact", {})
            env_input = result.last_inputs.get(entry.psd_id)
            if env_input is None or env_input.ts != entry.ts:
                result.fact_mismatches.append({
                    "seq": entry.seq,
                    "psd_id": entry.psd_id,
                    "reason": "找不到与该判定同节拍的原始采样",
                })
                continue
            profile = _profile_of_recorded(recorded, config)
            engine = engines.setdefault(entry.psd_id, DoorEnvEngine(entry.psd_id, config.window_s))
            recomputed = engine.evaluate(env_input, profile).as_dict()
            mismatch = _diff_fact(recorded, recomputed, entry.seq, entry.psd_id)
            if mismatch:
                result.fact_mismatches.append(mismatch)
            result.facts.append(recorded)
            result.levels.append({
                "seq": entry.seq,
                "ts": entry.ts,
                "psd_id": entry.psd_id,
                "level": recorded.get("level"),
                "margin_c": recorded.get("margin_c"),
                "profile_version": recorded.get("profile_version"),
                "reasons": recorded.get("reasons", []),
                "changed": recorded.get("changed"),
            })

    result.monitor = monitor
    result.engines = engines
    result.holds = fold_holds(entries)
    return result


def fold_holds(entries: list[Entry]) -> HoldRegistry:
    """按 HOLD 事件折叠出最终摘除表。"""

    holds = HoldRegistry()
    for entry in entries:
        if entry.kind != CHAIN_HOLD:
            continue
        action = entry.payload.get("action", "")
        if action in ("hold_risk", "hold_sensor"):
            payload = entry.payload.get("hold", {})
            holds.restore(Hold(
                psd_id=str(payload.get("psd_id", entry.psd_id)),
                reason=str(payload.get("reason", HOLD_RISK)),
                since=float(payload.get("since", entry.ts)),
                incident_id=str(payload.get("incident_id", "")),
                profile_version=str(payload.get("profile_version", "")),
                margin_c=payload.get("margin_c"),
                level=str(payload.get("level", "")),
                detail=str(payload.get("detail", "")),
                updated=float(payload.get("updated", 0.0)),
            ))
        elif action in ("release", "force_release"):
            holds.release(entry.psd_id)
    return holds


def _diff_fact(recorded: dict, recomputed: dict, seq: int, psd_id: str) -> dict | None:
    """逐字段比对历史判定与复算判定；浮点字段按 1e-3 容差。"""

    differences = {}
    for field_name in COMPARE_FIELDS:
        old = recorded.get(field_name)
        new = recomputed.get(field_name)
        if isinstance(old, (int, float)) and old is not None and new is not None:
            if abs(float(old) - float(new)) > 1e-3:
                differences[field_name] = {"recorded": old, "recomputed": new}
        elif old != new:
            differences[field_name] = {"recorded": old, "recomputed": new}
    if not differences:
        return None
    return {"seq": seq, "psd_id": psd_id, "differences": differences}


def explain(chain: ChainJournal, seq: int) -> dict:
    """取某条判定（按链序号）当时凭什么报的：采样 + 判定 + 前因。"""

    target: Entry | None = None
    for entry in chain.read_all():
        if entry.seq == seq:
            target = entry
            break
    if target is None:
        return {"found": False, "seq": seq}
    context = [
        {
            "seq": item.seq,
            "ts": item.ts,
            "kind": item.kind,
            "psd_id": item.psd_id,
            "payload": item.payload,
        }
        for item in chain.read_all()
        if item.psd_id == target.psd_id and item.seq <= seq
    ][-20:]
    return {
        "found": True,
        "seq": target.seq,
        "entry": {"ts": target.ts, "kind": target.kind, "payload": target.payload},
        "recent_context": context,
        "chain_hash": target.hash,
        "prev_hash": target.prev_hash,
    }
