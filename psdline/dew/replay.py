"""哈希链重放：事后回答"当时凭什么报了风险"。

重放只依赖入链事实，不接任何现场设备：

1. ``verify`` 先校验哈希链完整性（任何篡改/删改都会断链）；
2. 按链上拓扑重建门区、按 ``dew.policy`` 恢复当时生效的策略版本；
3. 按链序把 ``dew.sample``（原始值 + 质量证据）喂回与实时链路**相同**的融合与
   判定状态机，一拍三个量凑齐即产出该拍结论，逐拍重建每个门区的结论轨迹；
4. 与链上的 ``dew.verdict.changed`` 状态变迁逐条比对，任何不一致都报出来；
5. ``explain`` 给出某次风险判定的完整证据链：策略版本、三个量的读数与质量、
   瞬时/平滑余量、触发原因、随后的摘门与擦拭工单。
"""

from __future__ import annotations

import time

from .assess import RISK, DARK, RiskAssessor
from .events import EventChain
from .policy import DewPolicy, DewPolicySchedule
from .sensors import SensorMonitor
from .topology import KINDS

ACTION_KINDS = {
    "dew.hold.set",
    "dew.hold.clear",
    "dew.workorder.created",
    "dew.workorder.dispatched",
    "dew.workorder.done",
    "dew.workorder.failed",
    "dew.workorder.cancelled",
}


class _ZoneRebuild:
    """重放期的单门区运行态。"""

    def __init__(self, zone_id: str, station_id: str, layout: dict[str, tuple[str, ...]]):
        self.zone_id = zone_id
        self.station_id = station_id
        self.monitor = SensorMonitor(zone_id, layout)
        self.assessor = RiskAssessor(zone_id)
        # (ts, kind) -> FusedSample，凑齐三个量即判定并清空。
        self.pending: dict[tuple[float, str], object] = {}
        self.wipe_active = False


class Replayer:
    """从哈希链重建凝露链路。"""

    def __init__(self, chain: EventChain, schedule: DewPolicySchedule | None = None) -> None:
        self.chain = chain
        # 策略以链上记录为准；schedule 只在策略事件缺失时兜底。
        self.schedule = schedule or DewPolicySchedule()
        self._policies: dict[str, DewPolicy] = {}
        self._zones: dict[str, _ZoneRebuild] = {}
        self.timeline: list[dict] = []
        self._ran = False

    def run(self) -> dict:
        integrity = self.chain.verify()
        mismatch: list[dict] = []
        expected_edges = [
            {"zone_id": event.payload["zone_id"],
             "previous": event.payload["previous"],
             "current": event.payload["current"],
             "ts": event.ts,
             "seq": event.seq}
            for event in self.chain.events("dew.verdict.changed")
        ]
        actual_edges: list[dict] = []
        current_policy: DewPolicy | None = None

        for event in self.chain.events():
            kind = event.kind
            if kind == "_genesis":
                continue
            if kind == "dew.policy":
                current_policy = DewPolicy.from_dict(event.payload["params"])
                self._policies[current_policy.version] = current_policy
            elif kind == "dew.topology":
                self._build_zone(event.payload)
            elif kind == "dew.sample":
                edges = self._fold_sample(event, current_policy)
                actual_edges.extend(edges)
            elif kind == "dew.workorder.created" and event.payload.get("work_kind") == "WIPE":
                zone = self._zones.get(str(event.payload["zone_id"]))
                if zone is not None and event.payload.get("state") == "open":
                    zone.wipe_active = True
            elif kind in ("dew.workorder.done", "dew.workorder.cancelled") and event.payload.get(
                "work_kind"
            ) == "WIPE":
                zone = self._zones.get(str(event.payload["zone_id"]))
                if zone is not None:
                    zone.wipe_active = False

        for actual, expected in zip(actual_edges, expected_edges):
            if (
                actual["zone_id"] != expected["zone_id"]
                or actual["previous"] != expected["previous"]
                or actual["current"] != expected["current"]
            ):
                mismatch.append({"rebuilt": actual, "recorded": expected})
        if len(actual_edges) != len(expected_edges):
            mismatch.append(
                {
                    "reason": "edge_count_differs",
                    "rebuilt_count": len(actual_edges),
                    "recorded_count": len(expected_edges),
                }
            )
        self._ran = True
        return {
            "integrity": integrity,
            "ok": integrity["ok"] and not mismatch,
            "zones": sorted(self._zones),
            "policy_versions": sorted(self._policies),
            "samples_replayed": len(self.chain.events("dew.sample")),
            "edges_recorded": len(expected_edges),
            "edges_rebuilt": len(actual_edges),
            "mismatches": mismatch,
        }

    def _build_zone(self, payload: dict) -> None:
        zone_id = str(payload["zone_id"])
        layout: dict[str, tuple[str, ...]] = {kind: () for kind in KINDS}
        for name in payload.get("channels", []):
            kind = name.split(":")[1].split("#")[0]
            if kind in layout:
                layout[kind] = (*layout[kind], name)
        self._zones[zone_id] = _ZoneRebuild(
            zone_id, str(payload.get("station_id", "")), layout
        )

    def _fold_sample(self, event, current_policy: DewPolicy | None) -> list[dict]:
        payload = event.payload
        zone = self._zones.get(str(payload["zone_id"]))
        if zone is None:
            return []
        policy = current_policy or self.schedule.for_date(*_gm_month_day(event.ts))
        fused = zone.monitor.replay_step(str(payload["kind"]), payload)
        zone.pending[(round(event.ts, 6), str(payload["kind"]))] = fused
        kinds_ready = {key[1] for key in zone.pending if key[0] == round(event.ts, 6)}
        if {"air_c", "rh_pct", "glass_c"} != kinds_ready:
            return []
        ts = round(event.ts, 6)
        bucket = {
            key[1]: value
            for key, value in zone.pending.items()
            if key[0] == ts
        }
        for key in list(zone.pending):
            if key[0] == ts:
                del zone.pending[key]
        previous = zone.assessor.state
        verdict = zone.assessor.process(
            policy,
            bucket["air_c"],
            bucket["rh_pct"],
            bucket["glass_c"],
            ts,
            wipe_in_progress=zone.wipe_active,
        )
        self.timeline.append(
            {
                "ts": ts,
                "zone_id": zone.zone_id,
                "state": verdict.state,
                "margin_c": verdict.margin_c,
                "ewma_margin_c": verdict.ewma_margin_c,
                "policy_version": verdict.policy_version,
                "reasons": list(verdict.reasons),
                "confidence": verdict.confidence,
                "evidence": verdict.as_dict(),
            }
        )
        if verdict.state != previous:
            return [
                {
                    "zone_id": zone.zone_id,
                    "previous": previous,
                    "current": verdict.state,
                    "ts": ts,
                }
            ]
        return []

    def explain(self, zone_id: str, around_ts: float | None = None) -> dict:
        """给出某次（或最近一次）摘门判定的完整证据。"""

        result = self.run()
        trail = [item for item in self.timeline if item["zone_id"] == zone_id]
        if not trail:
            return {"ok": False, "zone_id": zone_id, "reason": "链上没有该门区的判定记录"}
        target_index = self._pick_index(trail, around_ts)
        point = trail[target_index]
        related = [
            {"seq": event.seq, "ts": event.ts, "kind": event.kind, "payload": event.payload}
            for event in self.chain.events()
            if event.payload.get("zone_id") == zone_id and event.kind in ACTION_KINDS
        ]
        policy = self._policies.get(point["policy_version"])
        return {
            "ok": result["ok"],
            "zone_id": zone_id,
            "at_ts": point["ts"],
            "state": point["state"],
            "why": list(point["reasons"]),
            "confidence": point["confidence"],
            "margin_c": point["margin_c"],
            "ewma_margin_c": point["ewma_margin_c"],
            "policy": policy.as_dict() if policy else None,
            "inputs": point["evidence"]["inputs"],
            "trail_around": [
                {
                    key: item[key]
                    for key in ("ts", "state", "margin_c", "ewma_margin_c", "reasons")
                }
                for item in trail[max(0, target_index - 4) : target_index + 3]
            ],
            "actions_after": [item for item in related if item["ts"] >= point["ts"]],
        }

    @staticmethod
    def _pick_index(trail: list[dict], around_ts: float | None) -> int:
        if around_ts is None:
            # 默认解释第一次凝露风险（RISK），而不是冷启动的数据黑暗期。
            risk = [
                index
                for index, item in enumerate(trail)
                if item["state"] == RISK
            ]
            return risk[0] if risk else len(trail) - 1
        return min(range(len(trail)), key=lambda index: abs(trail[index]["ts"] - around_ts))


def _gm_month_day(ts: float) -> tuple[int, int]:
    st = time.gmtime(ts)
    return st.tm_mon, st.tm_mday
