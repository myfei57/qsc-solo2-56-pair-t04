"""凝露防护链路协调器。

把四件事串成固定链路，每个控制周期跑一遍：

    环境采集（轮询 + 健康/融合）
      → 风险判定（纯状态机，产出带证据的结论）
      → 门控联动（RISK/DARK 摘门、恢复放开，维护本系统自己的凝露占用锁）
      → 清扫调度（擦拭/核查工单的生成、派单、重试、取消）

摘门**不借用**联锁故障锁：凝露不是门体故障，借用故障锁会让现场分不清"设备坏了"
和"玻璃起雾"。这里维护独立的凝露占用标志（``DewHoldRegistry``），
``door/batch.py`` 在既有 locked 检查之外再查凝露占用，两条原因分别上报。

全部事实（原始采样、通道健康、判定证据、摘放门、工单流转、策略版本）按确定
顺序写进同一条哈希链，顺序即事后重放的输入顺序。
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from ..eventbus import Event, EventBus
from .assess import DARK, OK, RISK, Assessment, RiskAssessor
from .cleaning import (
    CANCELLED,
    DISPATCHED,
    DONE,
    FAILED,
    INSPECT,
    OPEN,
    WIPE,
    CleaningPlanner,
    Dispatcher,
    WorkOrder,
)
from .events import ChainEvent, EventChain
from .policy import DewPolicy, DewPolicySchedule
from .sensors import BAD, FusedSample, GOOD, SUSPECT, SensorMonitor
from .topology import ZoneTopology


class DewHoldRegistry:
    """凝露占用锁：按门区设置/清除，独立于联锁故障锁。"""

    def __init__(self) -> None:
        self._holds: dict[str, dict] = {}

    def set(self, zone_id: str, state: str, reasons: tuple[str, ...], ts: float) -> dict:
        self._holds[zone_id] = {
            "zone_id": zone_id,
            "state": state,
            "reasons": list(reasons),
            "since": ts,
        }
        return dict(self._holds[zone_id])

    def clear(self, zone_id: str) -> None:
        self._holds.pop(zone_id, None)

    def is_held(self, zone_id: str) -> bool:
        return zone_id in self._holds

    def held_zones(self) -> list[str]:
        return sorted(self._holds)

    def describe(self, zone_id: str) -> Optional[dict]:
        item = self._holds.get(zone_id)
        return dict(item) if item else None

    def all(self) -> dict:
        return {key: dict(value) for key, value in self._holds.items()}


class ZoneChain:
    """单个门区的完整运行态。"""

    def __init__(self, topology: ZoneTopology) -> None:
        self.topology = topology
        self.monitor = SensorMonitor(topology.zone_id, topology.build_channels())
        self.assessor = RiskAssessor(topology.zone_id)
        self.assessment: Optional[Assessment] = None


def null_dispatcher(order: WorkOrder, now: float) -> tuple[bool, str]:
    """默认派单网关：未接清扫调度时受理（控制台/演示用）。"""

    return True, "accepted"


class DewSystem:
    """凝露防护子系统。"""

    def __init__(
        self,
        chain: EventChain,
        schedule: DewPolicySchedule,
        bus: Optional[EventBus] = None,
        dispatcher: Optional[Dispatcher] = None,
        clock: Optional[Callable[[], float]] = None,
        readers: Optional[dict[str, Callable[[], Optional[float]]]] = None,
    ) -> None:
        self.chain = chain
        self.schedule = schedule
        self.bus = bus or EventBus()
        self._dispatcher = dispatcher or null_dispatcher
        self._clock_fn = clock or time.time
        self._external_readers = readers or {}
        self.zones: dict[str, ZoneChain] = {}
        self.holds = DewHoldRegistry()
        self.planner = CleaningPlanner()
        self._policy_version: Optional[str] = None
        self._last_verdict: dict[str, str] = {}

    # ---- 装配 ---------------------------------------------------------

    def add_zone(self, topology: ZoneTopology) -> None:
        if topology.zone_id in self.zones:
            raise ValueError(f"门区已登记: {topology.zone_id}")
        self.zones[topology.zone_id] = ZoneChain(topology)
        self.chain.append(
            "dew.topology",
            self._now(),
            zone_id=topology.zone_id,
            station_id=topology.station_id,
            channels=topology.channel_names(),
        )

    def chain_zone_ids(self) -> set[str]:
        """返回哈希链上已经登记过的门区（重启恢复用）。"""

        return {
            str(event.payload.get("zone_id"))
            for event in self.chain.events("dew.topology")
        }

    def restore_from_chain(self) -> dict:
        """从哈希链重建运行态：门区、判定器状态、占用锁与工单。

        链是事实唯一来源。重建过程只消费事件、不产生新事件，因此重启不会
        改写历史；恢复后从链尾继续追加。
        """

        from .topology import KINDS

        # 1) 拓扑（不重新发 topology 事件）。
        for event in self.chain.events("dew.topology"):
            payload = event.payload
            zone_id = str(payload["zone_id"])
            if zone_id in self.zones:
                continue
            layout: dict[str, tuple[str, ...]] = {kind: () for kind in KINDS}
            for name in payload.get("channels", []):
                kind = name.split(":")[1].split("#")[0]
                if kind in layout:
                    layout[kind] = (*layout[kind], name)
            topology = ZoneTopology(
                zone_id=zone_id,
                station_id=str(payload.get("station_id", "")),
                channels=layout,
                readers=dict(self._external_readers),
            )
            self.zones[zone_id] = ZoneChain(topology)

        # 2) 按链序重建：策略、工单、占用锁、每拍判定。
        current_policy: DewPolicy | None = None
        orders: dict[str, WorkOrder] = {}
        # zone_id -> ts -> {kind: FusedSample}
        pending: dict[str, dict[float, dict]] = {}
        for event in self.chain.events():
            kind = event.kind
            if kind == "dew.policy":
                current_policy = DewPolicy.from_dict(event.payload["params"])
                self._policy_version = current_policy.version
            elif kind == "dew.sample":
                self._restore_sample(event, current_policy, pending, orders)
            elif kind == "dew.hold.set":
                self.holds.set(
                    str(event.payload["zone_id"]),
                    str(event.payload.get("state", "RISK")),
                    tuple(event.payload.get("reasons", ())),
                    event.ts,
                )
            elif kind == "dew.hold.clear":
                self.holds.clear(str(event.payload["zone_id"]))
            elif kind and kind.startswith("dew.workorder."):
                self._restore_workorder(kind, event, orders)
        for order in orders.values():
            self.planner.restore(order)
        for zone_id, zone in self.zones.items():
            if zone.assessment is not None:
                self._last_verdict[zone_id] = zone.assessment.state
        return {
            "zones": len(self.zones),
            "holds": self.holds.held_zones(),
            "orders": len(orders),
            "policy_version": self._policy_version,
        }

    def _restore_sample(self, event, current_policy, pending, orders) -> None:
        payload = event.payload
        zone = self.zones.get(str(payload["zone_id"]))
        if zone is None:
            return
        policy = current_policy or self.policy_at(event.ts)
        fused = zone.monitor.replay_step(str(payload["kind"]), payload)
        ts = round(event.ts, 6)
        batch = pending.setdefault(zone.topology.zone_id, {}).setdefault(ts, {})
        batch[str(payload["kind"])] = fused
        if {"air_c", "rh_pct", "glass_c"} <= set(batch):
            wipe_active = any(
                order.zone_id == zone.topology.zone_id
                and order.kind == WIPE
                and order.state in (OPEN, DISPATCHED, FAILED)
                for order in orders.values()
            )
            verdict = zone.assessor.process(
                policy, batch["air_c"], batch["rh_pct"], batch["glass_c"], ts, wipe_active
            )
            zone.assessment = verdict
            pending[zone.topology.zone_id].pop(ts, None)

    def _restore_workorder(self, kind: str, event, orders: dict) -> None:
        payload = event.payload
        order_id = str(payload.get("order_id", ""))
        if not order_id:
            return
        order = orders.get(order_id)
        if kind == "dew.workorder.created":
            orders[order_id] = WorkOrder(
                id=order_id,
                zone_id=str(payload["zone_id"]),
                kind=str(payload.get("work_kind", "WIPE")),
                reason=str(payload.get("reason", "")),
                created_ts=event.ts,
                state=str(payload.get("state", "open")),
            )
            return
        if order is None:
            return
        if kind == "dew.workorder.dispatched":
            order.state = "dispatched" if payload.get("accepted") else "failed"
            order.attempts = int(payload.get("attempt", order.attempts))
            order.dispatched_ts = event.ts
            order.note = str(payload.get("note", ""))
        elif kind == "dew.workorder.done":
            order.state = "done"
            order.completed_ts = event.ts
        elif kind == "dew.workorder.failed":
            order.state = "failed"
        elif kind == "dew.workorder.cancelled":
            order.state = "cancelled"


    def zone_ids(self) -> list[str]:
        return sorted(self.zones)

    def policy_at(self, ts: float) -> DewPolicy:
        return self.schedule.for_date(*_month_day(ts))

    def _now(self) -> float:
        return float(self._clock_fn())

    @staticmethod
    def _zone_wired(zone: ZoneChain) -> bool:
        """门区是否至少有一路通道接了真实驱动（非未接线占位）。"""

        for states in zone.monitor._states.values():
            for state in states:
                reader = state.channel.read
                check = getattr(reader, "wired", None)
                if callable(check):
                    if check():
                        return True
                elif reader is not None:
                    return True
        return False

    def _publish(self, action: str, **payload: object) -> None:
        self.bus.publish(Event.of("dew", "coordinator", action, **payload))

    # ---- 主链路 -------------------------------------------------------

    def tick(self) -> dict:
        """推进一个采样/判定/联动周期。"""

        now = self._now()
        self._note_policy(now)
        sampled: dict[str, dict[str, FusedSample]] = {}
        verdicts: dict[str, Assessment] = {}
        for zone_id in sorted(self.zones):
            zone = self.zones[zone_id]
            # 门区至少有一个已接真实驱动的通道才参与本轮；未接线的预留门区
            # （全部是 unconnected 占位）不产生采样与判定，避免被误判成断线。
            if not self._zone_wired(zone):
                continue
            policy = self.policy_at(now)
            fused = zone.monitor.poll(now, policy.stale_after_s)
            sampled[zone_id] = fused
            self._record_samples(zone_id, now, fused)
            wipe_active = self.planner.wipe_in_progress(zone_id)
            verdict = zone.assessor.process(
                policy, fused["air_c"], fused["rh_pct"], fused["glass_c"], now, wipe_active
            )
            zone.assessment = verdict
            verdicts[zone_id] = verdict
            self._apply_verdict(zone, verdict, now)
        dispatched = self._dispatch(now)
        return {
            "ts": now,
            "policy_version": self.policy_at(now).version,
            "held": self.holds.held_zones(),
            "dispatched": [
                {"order_id": order.id, "zone_id": order.zone_id, "accepted": accepted}
                for order, accepted, _note in dispatched
            ],
        }

    def _note_policy(self, now: float) -> None:
        version = self.policy_at(now).version
        if version != self._policy_version:
            policy = self.policy_at(now)
            self.chain.append(
                "dew.policy",
                now,
                version=version,
                label=policy.label,
                params=policy.as_dict(),
            )
            self._policy_version = version

    def _record_samples(self, zone_id: str, now: float, fused: dict[str, FusedSample]) -> None:
        for sample_kind, sample in fused.items():
            self.chain.append(
                "dew.sample",
                now,
                zone_id=zone_id,
                kind=sample_kind,
                value=sample.value,
                quality=sample.quality,
                reasons=list(sample.reasons),
                channels=[dict(item) for item in sample.channels],
            )
            if sample.quality != GOOD:
                self._publish(
                    "sample_untrusted",
                    zone_id=zone_id,
                    kind=sample_kind,
                    quality=sample.quality,
                    reasons=list(sample.reasons),
                )
    def _apply_verdict(self, zone: ZoneChain, verdict: Assessment, now: float) -> None:
        zone_id = zone.topology.zone_id
        held = self.holds.describe(zone_id)
        # 边沿的"上一拍状态"以判定器自身轨迹为准；占用锁只用来决定是否补
        # hold.set/clear 事件。冷启动 DARK 不该被记成 OK→DARK。
        previous_state = getattr(self, "_last_verdict", {}).get(zone_id, DARK)

        if verdict.state in (RISK, DARK):
            if previous_state != verdict.state:
                self.holds.set(zone_id, verdict.state, verdict.reasons, now)
                self.chain.append(
                    "dew.hold.set",
                    now,
                    zone_id=zone_id,
                    state=verdict.state,
                    reasons=list(verdict.reasons),
                )
                self._publish(
                    "door_held",
                    zone_id=zone_id,
                    station_id=zone.topology.station_id,
                    state=verdict.state,
                    reasons=list(verdict.reasons),
                )
            created = self.planner.ensure_for_state(
                zone_id, verdict.state, verdict.reasons, now,
                cooldown_s=self.policy_at(now).rewipe_cooldown_s,
            )
            if created is not None:
                self._record_order_event("dew.workorder.created", now, created)
            # 复擦不自动派：擦拭完成后风险仍在时，由班组通过
            # ``report_order_failed`` 明确上报"擦完仍起雾"，才进入重试。
            # 这避免工单在无人确认时自我繁殖。
        else:  # OK
            if held is not None:
                self.holds.clear(zone_id)
                self.chain.append("dew.hold.clear", now, zone_id=zone_id)
                self._publish("door_released", zone_id=zone_id, station_id=zone.topology.station_id)
            for kind in (WIPE, INSPECT):
                for order in self.planner.cancel_active(zone_id, kind, now):
                    self._record_order_event("dew.workorder.cancelled", now, order)

        # 判定证据每个周期入链（状态变化时单独打一条醒目的边）。
        if previous_state != verdict.state:
            self.chain.append(
                "dew.verdict.changed",
                now,
                zone_id=zone_id,
                previous=previous_state,
                current=verdict.state,
                evidence=verdict.as_dict(),
            )
        self._last_verdict[zone_id] = verdict.state

    def _dispatch(self, now: float) -> list[tuple[WorkOrder, bool, str]]:
        results = self.planner.dispatch_due(now, self._dispatcher)
        for order, accepted, note in results:
            self.chain.append(
                "dew.workorder.dispatched",
                now,
                order_id=order.id,
                zone_id=order.zone_id,
                work_kind=order.kind,
                attempt=order.attempts,
                accepted=accepted,
                note=note,
            )
        return results

    # ---- 外部交互 -----------------------------------------------------

    def report_order_done(self, order_id: str, note: str = "") -> Optional[WorkOrder]:
        now = self._now()
        order = self.planner.report_done(order_id, now, note)
        if order is not None:
            self._record_order_event("dew.workorder.done", now, order)
        return order

    def report_order_failed(self, order_id: str, note: str = "") -> Optional[WorkOrder]:
        """班组上报在途工单失败；工单进入 failed，由派单重试周期自动重试。"""

        now = self._now()
        order = self.planner.report_failed(order_id, now, note)
        if order is not None:
            self._record_order_event("dew.workorder.failed", now, order)
        return order

    def eligible_zones(self, zone_ids: list[str]) -> set[str]:
        """批量开门准入：未被凝露占用的门区才允许进入批量。"""

        return {zone_id for zone_id in zone_ids if not self.holds.is_held(zone_id)}

    def status(self) -> dict:
        return {
            "ts": self._now(),
            "policy_version": self._policy_version,
            "holds": self.holds.all(),
            "zones": {
                zone_id: {
                    "state": zone.assessment.state if zone.assessment else DARK,
                    "assessment": zone.assessment.as_dict() if zone.assessment else None,
                }
                for zone_id, zone in self.zones.items()
            },
            "work_orders": [order.as_dict() for order in self.planner.list_orders()],
            "chain_tip": self.chain.head().seq,
        }

    def _record_order_event(self, kind: str, now: float, order: WorkOrder) -> None:
        self.chain.append(
            kind,
            now,
            order_id=order.id,
            zone_id=order.zone_id,
            work_kind=order.kind,
            state=order.state,
            reason=order.reason,
            attempts=order.attempts,
        )
        action = {
            "dew.workorder.created": "workorder_created",
            "dew.workorder.dispatched": "workorder_dispatched",
            "dew.workorder.done": "workorder_done",
            "dew.workorder.failed": "workorder_failed",
            "dew.workorder.cancelled": "workorder_cancelled",
        }.get(kind, "workorder_event")
        self._publish(action, order_id=order.id, zone_id=order.zone_id, work_kind=order.kind)


def _month_day(ts: float) -> tuple[int, int]:
    return time.gmtime(ts).tm_mon, time.gmtime(ts).tm_mday
