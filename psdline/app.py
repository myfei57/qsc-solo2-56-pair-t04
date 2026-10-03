"""平台装配与控制循环。

``Platform`` 把命名空间、站台、列车、站台门、信号、联锁、门控、门区映射、间隙
探测、配额与审计按真实调用链装到一起，并暴露四件事：到发编排（到站、发车、
门区联动、批量开门、清扫）、控制循环、状态快照与手动检修动作。

控制链固定为「信号 → 停稳对位 → 开门 → 间隙探测 → 关门 → 发车 → 审计」，
任何一步错位都会被对应组件拒绝，并把拒绝原因落进审计。
"""

from __future__ import annotations

from .audit import AuditLog, build_timeline
from .clock import Clock, SystemClock
from .condensation import EnvController
from .condensation.replay import replay_chain
from .config import PlatformConfig, default_config, with_storage_root
from .door import (
    DoorController,
    active_group,
    batch_open,
    close_and_audit,
    depart,
    depart_policy,
    door_report,
    door_state,
    enqueue,
    open_pressure,
    open_doors,
    open_group,
    process_queue,
    queued_total,
)
from .errors import RailError
from .eventbus import Event, EventBus
from .gap import GapRegistry, gap_summary
from .ids import IdFactory
from .interlock import (
    FaultRegistry,
    InterlockArmer,
    interlock_status,
    next_sequence,
    releasable,
    restore_lockouts,
    verdict_of,
)
from .ns import Namespace, NamespaceRegistry
from .platform import MappingRegistry, adjacent, group_keys, physical_groups, resolve
from .psd import LockoutRegistry, PSD, PSDRegistry, sweep_station
from .quota import QuotaRegistry
from .sig import SignalController, aspect_of, depart_checked, refresh_position, signal_summary
from .station import (
    DoorZone,
    Layout,
    LayoutRegistry,
    Schedule,
    ScheduleRegistry,
    Station,
    StationRegistry,
    nearest_zone,
    spread_m,
)
from .store import CommandLog, JsonStore, JsonlJournal, StoreMetrics
from .telemetry import TrendBuffer
from .train import (
    AlignRegistry,
    DoorStateRegistry,
    Dwell,
    DwellRegistry,
    PositionTracker,
    Route,
    RouteRegistry,
    Train,
    TrainRegistry,
    train_state,
)

ARRIVAL = "arrival"
DEPART = "depart"


class Platform:
    """站台门区顺序与联锁控制平台。"""

    def __init__(
        self,
        config: PlatformConfig | None = None,
        clock: Clock | None = None,
        unit: str | None = None,
    ) -> None:
        self.config = (config or default_config()).validate()
        self.clock = clock or SystemClock()
        self.bus = EventBus()
        self.unit = unit or self.config.platform.unit
        self.ids = IdFactory("op")

        store = JsonStore(self.config.storage_root)
        self.store = store
        self.metrics = StoreMetrics(store.root)
        self.audit = AuditLog(
            self.clock,
            self.bus,
            JsonlJournal(store.root, "audit", segment_limit=self.config.audit.journal_segment),
        )
        self.namespace = Namespace(
            self.config.namespace.line, list(self.config.namespace.stations)
        )
        self.namespaces = NamespaceRegistry(self.unit)
        self.trend = TrendBuffer(period_s=self.config.signal.refresh_period_s)

        self.stations = StationRegistry(store)
        self.layouts = LayoutRegistry(store)
        self.schedules = ScheduleRegistry(store)
        self.trains = TrainRegistry(store)
        self.align = AlignRegistry(store, tolerance_m=self.config.station.stop_tolerance_m)
        self.train_doors = DoorStateRegistry(store)
        self.dwells = DwellRegistry(store, max_seconds=self.config.train.dwell_max_s)
        self.positions = PositionTracker(
            store,
            max_position_m=self.config.train.max_position_m,
            stale_after_s=self.config.signal.stale_after_s,
        )
        self.routes = RouteRegistry(store)

        self.gates = PSDRegistry(store)
        self.lockouts = LockoutRegistry(self.gates)
        self.signals = SignalController(store)
        self.faults = FaultRegistry(store)
        self.armers = InterlockArmer(store)
        self.mappings = MappingRegistry(store)
        self.gaps = GapRegistry(store)
        self.quotas = QuotaRegistry(store)
        self.commands = CommandLog(store)
        self.doors = DoorController(
            store, self.align, self.commands, self.gates, self.lockouts
        )
        self.doors.set_batch_limit(self.config.door.batch_limit)

        self.env = EnvController(
            config=self.config.condensation,
            clock=self.clock,
            store=store,
            audit=self.audit,
        )
        self._env_restored: dict | None = None

        self._cycles = 0
        self._operations = 0
        self._event_counts: dict[str, int] = {}
        self.bus.subscribe("*", self._on_event)
        self._register_namespaces()
        self._seed(store)
        self._restore()

    def _on_event(self, event: Event) -> None:
        """统计总线事件，供状态接口判断动作有没有真的流过组件。"""

        self._event_counts[event.topic] = self._event_counts.get(event.topic, 0) + 1

    # ---- 装配 ---------------------------------------------------------

    def _register_namespaces(self) -> None:
        """登记现场设备点位（控制台与审计共用同一套名字）。"""

        for area, device, kind in (
            ("line", "signal", "signal-controller"),
            ("line", "sequence", "sequence-source"),
            ("station", "platform", "platform-screen"),
            ("station", "gap", "gap-detector"),
            ("station", "door", "platform-door"),
            ("station", "interlock", "door-interlock"),
            ("station", "quota", "quota-counter"),
            ("station", "audit", "audit-sink"),
        ):
            self.namespaces.register(area, device, kind)

    def _seed(self, store: JsonStore) -> None:
        """按线路命名空间布置站台、布局、映射与站台门。"""

        seed_doc = "platform/seed"
        if store.exists(seed_doc):
            return
        groups_per_station = self.config.station.groups_per_station
        doors_per_group = self.config.station.doors_per_group
        span = self.config.station.group_span_m
        for station_index, station_id in enumerate(self.namespace.station_ids(), start=1):
            groups = [f"G{index:02d}" for index in range(1, groups_per_station + 1)]
            self.stations.upsert(
                Station(
                    id=station_id,
                    name=f"站台 {station_index}",
                    line=self.namespace.line_id(),
                    door_groups=groups,
                )
            )
            base = (station_index - 1) * span
            zones = [
                DoorZone(id=group, distance_m=int(base + (index - 1) * span / groups_per_station))
                for index, group in enumerate(groups, start=1)
            ]
            self.layouts.save(Layout(station_id=station_id, zones=zones))
            self.mappings.renumber(station_id, {group: group for group in groups})
            self.schedules.save(
                Schedule(station_id=station_id, headways=[120, 150, 180, 240])
            )
            for group in groups:
                for door_index in range(1, doors_per_group + 1):
                    self.gates.upsert(
                        PSD(
                            id=f"{station_id}-{group}-{door_index:02d}",
                            station=station_id,
                            group=group,
                        )
                    )
        for name, limit in (
            (ARRIVAL, self.config.quota.arrival_limit),
            (DEPART, self.config.quota.depart_limit),
        ):
            self.quotas.set_limit(name, limit)
        store.write(
            seed_doc,
            {"line": self.namespace.line_id(), "stations": self.namespace.station_ids()},
        )

    def _restore(self) -> None:
        """从落盘状态恢复操作计数与锁定状态。"""

        state = self.store.read("platform/state")
        if isinstance(state, dict):
            self._operations = int(state.get("operations", 0))
            self._cycles = int(state.get("cycles", 0))
        restore_lockouts(self.faults, self.gates, self.lockouts, not self.gaps.blocked_ids())
        if self.env.chain.count() and self.config.condensation.verify_on_start:
            # 凝露链与现场当前态必须能对上；链被改过或判据重放不一致，拒绝启动。
            self._env_restored = self.env.restore_from_chain()
        else:
            self._env_restored = None

    def _persist(self) -> None:
        """落盘平台计数，供重启后对照。"""

        self.store.write(
            "platform/state",
            {
                "operations": self._operations,
                "cycles": self._cycles,
                "unit": self.unit,
                "line": self.namespace.line_id(),
            },
        )

    def _record_operation(self, op_id: str, operation: str, report: dict) -> dict:
        """把编排步骤逐条写事件，并记一条汇总审计。"""

        train_id = str(report.get("train_id", ""))
        station_id = str(report.get("station_id", ""))
        reserved = {
            "train_id",
            "station_id",
            "op_id",
            "severity",
            "message",
            "component",
            "action",
            "detail",
        }
        for step in report.get("steps", []):
            detail = {
                key: value
                for key, value in dict(step.get("detail", {})).items()
                if not isinstance(value, (dict, list)) and key not in reserved
            }
            self.bus.publish(
                Event.of(
                    "door",
                    str(step.get("component", "door")),
                    str(step.get("action", "step")),
                    op_id=op_id,
                    train_id=train_id,
                    severity="notice",
                    **detail,
                )
            )
        for violation in report.get("violations", []):
            self.bus.publish(
                Event.of(
                    "audit",
                    "interlock",
                    "interlock_rejected",
                    op_id=op_id,
                    train_id=train_id,
                    station_id=station_id,
                    severity="alarm",
                    message=f"{operation} 被拒绝：{violation}",
                    violation=violation,
                )
            )
        entry = self.audit.record(
            f"{operation}_complete",
            train_id,
            station_id,
            op_id=op_id,
            ok=bool(report.get("ok")),
            violations=list(report.get("violations", [])),
        )
        return {
            "op_id": op_id,
            "operation": operation,
            "ok": bool(report.get("ok")),
            "violations": list(report.get("violations", [])),
            "audit_id": entry.id,
        }

    # ---- 生命周期 -----------------------------------------------------

    def start(self) -> dict:
        """启动平台：核对命名空间、站台、布局与映射是否一致。"""

        missing: dict[str, list[str]] = {}
        for station_id in self.namespace.station_ids():
            if self.stations.get(station_id) is None:
                missing.setdefault("station", []).append(station_id)
            if self.layouts.get(station_id) is None:
                missing.setdefault("layout", []).append(station_id)
            if self.mappings.current(station_id) is None:
                missing.setdefault("mapping", []).append(station_id)
        return {
            "unit": self.unit,
            "line": self.namespace.label(),
            "missing": missing,
            "stations": len(self.namespace.station_ids()),
            "gates": len(self.gates.list()),
            "operations": self._operations,
            "ready": not missing,
        }

    def stop(self) -> dict:
        """停运平台：整站清扫后收起全部灯位。"""

        op_id = self.ids.labeled("stop")
        steps: list[dict] = []
        closed = 0
        for station_id in self.namespace.station_ids():
            report = self.sweep(station_id)
            closed += report["closed"]
            steps.append(
                {
                    "component": "psd",
                    "action": "sweep",
                    "detail": {"station_id": station_id, "closed": report["closed"]},
                }
            )
        for train in self.trains.list():
            self.signals.clear(train.id, ARRIVAL)
            self.signals.clear(train.id, DEPART)
        report = {"ok": True, "train_id": "", "closed": closed, "steps": steps, "violations": []}
        summary = self._record_operation(op_id, "stop", report)
        self._operations += 1
        self._persist()
        return {**report, **summary}

    # ---- 到发编排 -----------------------------------------------------

    def arrive(self, train_id: str, station_id: str, position_m: int) -> dict:
        """到站到开门：信号到站 → 停稳对位 → 对位落盘 → 开门。"""

        op_id = self.ids.labeled("arrive")
        steps: list[dict] = []
        violations: list[str] = []

        def reject(reason: str) -> dict:
            violations.append(reason)
            self.bus.publish(
                Event.of(
                    "audit",
                    "interlock",
                    "arrive_rejected",
                    op_id=op_id,
                    train_id=train_id,
                    station_id=station_id,
                    severity="alarm",
                    message=reason,
                )
            )
            return {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": violations,
            }

        if not self.quotas.consume(ARRIVAL):
            return reject("到站配额已用完")
        try:
            self.namespace.require(station_id)
        except RailError as error:
            return reject(error.message)
        if self.stations.get(station_id) is None:
            return reject(f"站台未登记: {station_id}")

        self.signals.raise_signal(train_id, ARRIVAL, self.clock.now())
        steps.append(
            {"component": "sig", "action": "arrival_signal", "detail": {"train_id": train_id}}
        )
        if self.trains.get(train_id) is None:
            self.trains.upsert(Train(id=train_id, line=self.namespace.line_id()))
        self.trains.assign_station(train_id, station_id)

        stop_position = self._stop_position(station_id)
        try:
            alignment = self.align.align(train_id, position_m, stop_position, self.clock.now())
        except RailError as error:
            return reject(error.message)
        steps.append(
            {
                "component": "train",
                "action": "align",
                "detail": {"train_id": train_id, "position_m": alignment.position_m},
            }
        )
        try:
            self.doors_open(train_id, station_id)
        except RailError as error:
            return reject(error.message)
        entry = self.audit.record(ARRIVAL, train_id, station_id, position_m=position_m)
        report = {
            "ok": True,
            "train_id": train_id,
            "station_id": station_id,
            "position_m": alignment.position_m,
            "steps": steps,
            "violations": [],
        }
        summary = self._record_operation(op_id, "arrive", report)
        self._operations += 1
        self._persist()
        return {**report, **summary, "arrival_audit": entry.id}

    def doors_open(self, train_id: str, station_id: str) -> dict:
        """开门并确认车门：对位必须先落盘。"""

        report = open_doors(self.doors, train_id, self.clock.now())
        group = self._primary_group(station_id)
        deferred_by_condensation: list[str] = []
        if group:
            try:
                open_group(self.doors, self.mappings, station_id, group, self.env.holds)
            except RailError as error:
                if error.component != "condensation":
                    raise
                # 整组联动开门撞上凝露摘除：跳过该组并落审计，到站编排不回滚，
                # 被摘门的擦拭闭环在凝露链里单独走。
                deferred_by_condensation.append(group)
                self.audit.record(
                    "condensation_open_deferred",
                    train_id,
                    station_id,
                    group=group,
                    reason=str(error.message),
                )
        self.train_doors.confirm_open(train_id, self.clock.now())
        self.bus.publish(
            Event.of(
                "door",
                "door",
                "opened",
                train_id=train_id,
                station_id=station_id,
                severity="notice",
                group=group,
            )
        )
        return {**report, "group": group,
                "deferred_by_condensation": deferred_by_condensation}

    def depart(self, train_id: str, station_id: str) -> dict:
        """关门到发车：关门指令 → 间隙探测 → 关门落盘 → 发车信号。"""

        op_id = self.ids.labeled("depart")
        steps: list[dict] = []
        if not self.quotas.consume(DEPART):
            report = {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": ["发车配额已用完"],
            }
            summary = self._record_operation(op_id, "depart", report)
            return {**report, **summary}

        gate = self._primary_gate(station_id)
        if gate is not None and not self.lockouts.is_locked(gate.id):
            self.lockouts.set(gate.id, True)
            steps.append(
                {"component": "interlock", "action": "lock", "detail": {"psd_id": gate.id}}
            )
        try:
            closed = close_and_audit(
                self.doors, self.audit, train_id, station_id, DEPART, self.clock.now()
            )
        except RailError as error:
            report = {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": [error.message],
            }
            summary = self._record_operation(op_id, "depart", report)
            return {**report, **summary}
        steps.append(
            {"component": "door", "action": "close_durable", "detail": {"train_id": train_id}}
        )
        gap_state = self.gaps.state(train_id)
        steps.append(
            {
                "component": "gap",
                "action": "check",
                "detail": {"train_id": train_id, "state": gap_state},
            }
        )
        verdict = depart_checked(self.signals, self.store, self.gaps, train_id, self.clock.now())
        if not verdict["ok"]:
            report = {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": list(verdict["violations"]),
            }
            summary = self._record_operation(op_id, "depart", report)
            return {**report, **summary}
        steps.append(
            {"component": "sig", "action": "depart_signal", "detail": {"train_id": train_id}}
        )
        departure = depart(self.doors, train_id, self.clock.now())
        if gate is not None:
            self.lockouts.clear(gate.id)
        report = {
            "ok": True,
            "train_id": train_id,
            "station_id": station_id,
            "close_durable": bool(closed["close_durable"]),
            "aspect": verdict["aspect"],
            "cleared_commands": departure["cleared_commands"],
            "steps": steps,
            "violations": [],
        }
        summary = self._record_operation(op_id, "depart", report)
        self._operations += 1
        self._persist()
        return {**report, **summary}

    def check_gap(self, train_id: str, clear: bool = True) -> dict:
        """确认间隙无异常，或登记一次可疑探测结果。"""

        if clear:
            result = self.gaps.check(train_id, self.clock.now())
        else:
            result = self.gaps.probe(train_id, False, self.clock.now())
        self.audit.record("gap_probe", train_id, "", clear=clear)
        return {"ok": True, "train_id": train_id, "clear": result.clear, "violations": []}

    def renumber(self, station_id: str, groups: dict[str, str]) -> dict:
        """按最新布局重排门区映射，并按新映射联动一次。"""

        mapping = self.mappings.renumber(station_id, groups)
        first_key = sorted(groups)[0]
        try:
            linked = open_group(self.doors, self.mappings, station_id, first_key, self.env.holds)
        except RailError as error:
            if error.component != "condensation":
                raise
            self.audit.record(
                "condensation_open_deferred", "", station_id,
                group=first_key, reason=str(error.message),
            )
            linked = {"group": "", "held": True, "reason": error.message}
        self.audit.record("mapping_renumber", "", station_id, revision=mapping.revision)
        return {
            "ok": True,
            "station_id": station_id,
            "revision": mapping.revision,
            "mapping": mapping.as_dict(),
            "active_group": linked["group"],
            "violations": [],
        }

    def enqueue(self, train_id: str, command_id: str, kind: str, station_id: str = "") -> dict:
        """把一条门控指令排进队列。"""

        command = enqueue(self.doors, train_id, command_id, kind, station_id)
        self.audit.record(
            "door_enqueue", train_id, station_id, command_id=command.id, command_kind=kind
        )
        return {"ok": True, "train_id": train_id, "command": command.as_dict(), "violations": []}

    def process_queue(self, train_id: str) -> dict:
        """返回列车队列里待执行的门控指令，并带上发车自检结果。"""

        policy = depart_policy(self.doors, train_id)
        ids = process_queue(self.doors, train_id)
        self.audit.record("door_process", train_id, "", queued=len(ids))
        return {
            "ok": True,
            "train_id": train_id,
            "ids": ids,
            "ready": policy["allowed"],
            "violations": list(policy["failures"]),
        }

    def clear_fault(self, psd_id: str) -> dict:
        """清除门区故障并按条件解锁。"""

        gate = self.gates.require(psd_id)
        allowed, reason = releasable(self.gates, psd_id, True)
        if not allowed:
            return {"ok": False, "psd_id": psd_id, "locked": True, "violations": [reason]}
        self.faults.clear_fault(self.lockouts, psd_id, self.clock.now())
        self.audit.record("interlock_clear", "", gate.station, psd_id=psd_id)
        return {"ok": True, "psd_id": psd_id, "locked": False, "violations": []}

    def set_fault(self, psd_id: str) -> dict:
        """置位门区故障并锁定。"""

        gate = self.gates.require(psd_id)
        self.faults.set_fault(self.lockouts, psd_id, self.clock.now())
        self.audit.record("interlock_fault", "", gate.station, psd_id=psd_id)
        return {"ok": True, "psd_id": psd_id, "locked": True, "violations": []}

    def arm(self, train_id: str, armed: bool) -> dict:
        """启用或退出一列车的联锁。"""

        state = self.armers.arm(train_id, armed)
        self.audit.record("interlock_arm", train_id, "", armed=state)
        return {"ok": True, "train_id": train_id, "armed": state, "violations": []}

    def refresh_position(self, train_id: str, position_m: int) -> dict:
        """按信号刷新列车位置。"""

        outcome = refresh_position(self.positions, train_id, position_m, self.clock.now())
        if outcome["ok"]:
            self.trend.append("train_position_m", self.clock.now(), position_m)
        else:
            self.audit.record("position_rejected", train_id, "", reason=outcome["reason"])
        return {**outcome, "violations": [] if outcome["ok"] else [outcome["reason"]]}

    def sequence_verdict(self, previous: int, step: int) -> dict:
        """推进门控事件序号并给出回绕判定。"""

        current = next_sequence(previous, step)
        verdict = verdict_of(previous, current)
        self.audit.record(
            "sequence_verdict",
            "",
            "",
            previous=verdict.previous,
            current=verdict.current,
            advanced=verdict.advanced,
        )
        return {"ok": verdict.advanced, "verdict": verdict.as_dict(), "violations": [] if verdict.advanced else [verdict.reason]}

    def batch_open(self, station_id: str) -> dict:
        """按布局里程顺序批量开门，凝露摘除的门逐樘剔除并留痕。"""

        layout = self.layouts.get(station_id)
        zones = list(layout.zones) if layout else []
        outcome = batch_open(self.doors, station_id, zones, self.env.holds)
        nearest = nearest_zone(zones)
        self.audit.record(
            "batch_open",
            "",
            station_id,
            opened=len(outcome["opened"]),
            skipped=len(outcome["skipped"]),
            condensation_held=len(outcome["held_gates"]),
        )
        return {
            "ok": not outcome["skipped"],
            "station_id": station_id,
            "order": outcome["order"],
            "opened": outcome["opened"],
            "deferred": outcome["deferred"],
            "skipped": outcome["skipped"],
            "held": outcome["held"],
            "held_gates": outcome["held_gates"],
            "nearest": nearest.id if nearest else "",
            "violations": [],
        }

    def sweep(self, station_id: str) -> dict:
        """整站清扫。"""

        return sweep_station(self.gates, station_id)

    def register_dwell(self, train_id: str, seconds: float, station_id: str = "") -> dict:
        """登记停站时长。"""

        dwell = self.dwells.set(Dwell(train_id=train_id, seconds=seconds, station_id=station_id))
        return {"ok": True, "dwell": dwell.as_dict(), "violations": []}

    def assign_route(self, train_id: str, origin: str, destination: str, distance_m: int) -> dict:
        """登记运行交路。"""

        route = self.routes.assign(
            Route(train_id=train_id, origin=origin, destination=destination, distance_m=distance_m)
        )
        return {"ok": True, "route": route.__dict__, "violations": []}

    # ---- 凝露风险链 ---------------------------------------------------

    def attach_env_adapter(self, adapter) -> dict:
        """挂上环境采集适配器（真实现场实现 EnvAdapter 协议）。"""

        self.env.set_adapter(adapter)
        return {"ok": True, "adapter": type(adapter).__name__,
                "bound_doors": len(self.env.bound_ids())}

    def set_cleaning_dispatcher(self, dispatcher) -> dict:
        """配置外部清扫/检修派单通道（短信、班组终端、工单系统）。

        没配置时工单走"现场人工"模式：建单即视为已通知，班组从控制台/工单
        看板取单；配置后每个 OPEN 工单按周期重派，失败留单下周期再试。
        """

        self.env.wipes.set_dispatcher(dispatcher)
        self.audit.record("condensation_dispatcher_set", "", "",
                         dispatcher=type(dispatcher).__name__)
        return {"ok": True, "dispatcher": type(dispatcher).__name__}

    def bind_env_sensors(
        self,
        psd_id: str,
        air_t_sensor: str,
        rh_sensor: str,
        surface_sensors: str | list[str],
    ) -> dict:
        """把一樘门绑到门区空气温湿度与门体表面传感器。"""

        gate = self.gates.require(psd_id)
        self.env.bind_door(
            psd_id,
            gate.station,
            air_t=air_t_sensor,
            rh=rh_sensor,
            surface=surface_sensors,
        )
        self.audit.record(
            "condensation_bind",
            "",
            gate.station,
            psd_id=psd_id,
            air_t=air_t_sensor,
            rh=rh_sensor,
            surface=",".join(
                [surface_sensors] if isinstance(surface_sensors, str) else list(surface_sensors)
            ),
        )
        return {"ok": True, "psd_id": psd_id, "station_id": gate.station,
                "binding": self.env.bindings()[psd_id]}

    def auto_bind_env(self, adapter) -> dict:
        """按适配器提供的传感器命名约定自动绑定全部门（演示/交付检查用）。

        约定：门区空气传感器按站台共享 ``<station>-air`` / ``<station>-rh``，
        门体表面传感器每樘一只 ``<psd_id>-surface``。
        """

        attached = self.attach_env_adapter(adapter)
        bound: list[str] = []
        for gate in self.gates.list():
            air_id = f"{gate.station}-air"
            rh_id = f"{gate.station}-rh"
            surface_id = f"{gate.id}-surface"
            known = adapter.sensor_ids() if hasattr(adapter, "sensor_ids") else {}
            if not known or {air_id, rh_id, surface_id} <= set(known):
                self.env.bind_door(
                    gate.id, gate.station,
                    air_t=air_id, rh=rh_id, surface=surface_id,
                )
                bound.append(gate.id)
        return {"ok": True, "adapter": type(adapter).__name__, "bound": bound}

    def env_inject_fault(self, sensor_id: str, broken: bool, error: str = "联调注入断线") -> dict:
        """联调/巡检：对已挂载的脚本适配器注入或撤销传感器断线。"""

        adapter = getattr(self.env, "_adapter", None)
        if adapter is None or not hasattr(adapter, "fail"):
            raise RailError("当前适配器不支持故障注入（仅联调脚本适配器支持）",
                            component="console")
        if broken:
            adapter.fail(sensor_id, error)
        else:
            adapter.recover(sensor_id)
        self.audit.record("condensation_fault_inject", "", "", sensor_id=sensor_id,
                          broken=broken)
        return {"ok": True, "sensor_id": sensor_id, "broken": broken}

    def env_poll(self) -> dict:
        """手动推进一个凝露采集节拍（不等控制周期）。"""

        return self.env.poll(self.clock.now())

    def report_wiped(self, psd_id: str, assignee: str = "现场班组", note: str = "") -> dict:
        """班组回报擦拭/检修完成；门是否放开由判据恢复确认决定。"""

        return self.env.report_wiped(psd_id, self.clock.now(), assignee=assignee, note=note)

    def force_release_env(self, psd_id: str, operator: str, reason: str) -> dict:
        """值班员紧急放开凝露摘除（必须登记操作人与理由）。"""

        return self.env.force_release(psd_id, operator, reason, self.clock.now())

    def env_door_view(self, psd_id: str) -> dict:
        """单门凝露视图：绑定、最新输入、判定事实、摘除与工单。"""

        self.gates.require(psd_id)
        view = self.env.door_view(psd_id)
        view["station_id"] = self.env.station_of(psd_id) or view["station_id"]
        return view

    def env_status(self) -> dict:
        """凝露链汇总：摘除、传感器健康、工单、哈希链尾。"""

        return self.env.summary()

    def env_replay(self, psd_id: str = "", seq: int | None = None) -> dict:
        """重放凝露链：哈希校验 + 判据复算比对，可只看某条判定的依据。"""

        from .condensation.replay import explain

        result = replay_chain(self.env.chain, self.config.condensation, self.env.bindings())
        payload = result.as_dict()
        if seq is not None:
            payload["explain"] = explain(self.env.chain, int(seq))
        if psd_id:
            payload["levels"] = [item for item in result.levels if item["psd_id"] == psd_id]
        return payload

    # ---- 控制循环 -----------------------------------------------------

    def cycle(self, dt: float = 1.0) -> dict:
        """推进一个控制周期，返回本周期摘要。"""

        if dt <= 0:
            return {"ok": False, "violations": ["步长必须为正"], "cycle": self._cycles}
        self._cycles += 1
        moment = self.clock.now()
        stale = [
            train.id for train in self.trains.list() if self.positions.stale(train.id, moment)
        ]
        interlock = interlock_status(self.faults, self.gates, self.config.interlock.max_locked_ratio)
        holding = [
            train.id
            for train in self.trains.list()
            if not self.armers.armed(train.id) and depart_policy(self.doors, train.id)["allowed"]
        ]
        self.trend.append("locked_count", moment, interlock["locked_count"])
        self.trend.append("queued_commands", moment, queued_total(self.doors))
        self.trend.append("audit_entries", moment, len(self.audit.list()))
        env_poll = None
        if self.env_adapter_active():
            env_poll = self.env.poll(moment)
            self.trend.append(
                "condensation_held", moment, len(self.env.holds.held_ids())
            )
        self._persist()
        return {
            "ok": True,
            "cycle": self._cycles,
            "dt": dt,
            "stale_positions": stale,
            "locked_count": interlock["locked_count"],
            "can_release_depart": interlock["can_release_depart"],
            "ready_trains": holding,
            "gaps": gap_summary(self.gaps, [train.id for train in self.trains.list()]),
            "condensation": {
                "polled": env_poll is not None,
                "held": self.env.holds.held_ids(),
                "transitions": env_poll["transitions"] if env_poll else [],
            },
            "violations": [],
        }

    def env_adapter_active(self) -> bool:
        """凝露采集是否已接适配器且至少绑定了一樘门。"""

        return self.env.adapter_is_active() and bool(self.env.bound_ids())

    # ---- 查询 ---------------------------------------------------------

    def status(self) -> dict:
        """汇总状态快照，供控制台与命令行读取。"""

        trains = self.trains.list()
        ids = [train.id for train in trains]
        return {
            "platform": self.config.name,
            "unit": self.unit,
            "line": self.namespace.label(),
            "operations": self._operations,
            "cycles": self._cycles,
            "utc": self.clock.utc_iso(),
            "namespace": self.namespace.as_dict(),
            "namespaces": self.namespaces.describe(),
            "open_gates": self.gates.count_open(),
            "lockout_changes": self.lockouts.changes,
            "signals_raised": self.signals.raises,
            "signals_cleared": self.signals.clears,
            "interlock_counters": {
                "faults_set": self.faults.sets,
                "faults_cleared": self.faults.clears,
                "armed": self.armers.arms,
                "disarmed": self.armers.disarms,
                "armed_trains": self.armers.armed_trains(),
            },
            "gap_engine": {"checks": self.gaps.checks, "probes": self.gaps.probes},
            "event_counts": dict(sorted(self._event_counts.items())),
            "console_hits": len(self.bus.history("console")),
            "stations": [station.as_dict() for station in self.stations.list()],
            "trains": {train.id: train_state(train.id, self.align, self.train_doors, self.positions) for train in trains},
            "doors": [
                {**gate.as_dict(), "open": gate.open, "locked": gate.locked}
                for gate in self.gates.list()
            ],
            "door_report": door_report(self.doors, self.namespace.station_ids()),
            "signals": signal_summary(self.signals, ids),
            "interlock": interlock_status(
                self.faults, self.gates, self.config.interlock.max_locked_ratio
            ),
            "gaps": gap_summary(self.gaps, ids),
            "quota": {
                ARRIVAL: self.quotas.status(ARRIVAL),
                DEPART: self.quotas.status(DEPART),
            },
            "store": self.metrics.summary(),
            "audit": self.audit.summary(),
            "condensation": self.env.summary(),
            "trend": self.trend.snapshot(limit=4),
            "ids_issued": self.ids.issued(),
        }

    def snapshot(self) -> dict:
        """返回只读的整体快照（状态 + 命名空间 + 门区映射 + 审计时间线）。"""

        return {
            "status": self.status(),
            "groups": {
                station_id: group_keys(self.mappings, station_id)
                for station_id in self.namespace.station_ids()
            },
            "physical_groups": {
                station_id: physical_groups(self.mappings, station_id)
                for station_id in self.namespace.station_ids()
            },
            "mapping_revisions": {
                station_id: {
                    "latest": self.mappings.history(station_id).latest(),
                    "log": self.mappings.history(station_id).as_dict()["revisions"],
                }
                for station_id in self.namespace.station_ids()
            },
            "adjacent": self._adjacent_sample(),
            "timeline": build_timeline(self.audit.list(), limit=20),
        }

    def train_view(self, train_id: str) -> dict:
        """返回一列车的综合视图。"""

        return {
            "train": self.trains.get(train_id).as_dict() if self.trains.get(train_id) else None,
            "state": train_state(train_id, self.align, self.train_doors, self.positions),
            "doors": door_state(self.doors, train_id),
            "aspect": aspect_of(self.signals, train_id),
            "gap": self.gaps.state(train_id),
        }

    def station_view(self, station_id: str) -> dict:
        """返回一个站台的综合视图。"""

        layout = self.layouts.get(station_id)
        mapping = self.mappings.current(station_id)
        zones = list(layout.zones) if layout else []
        schedule = self.schedules.get(station_id)
        return {
            "station": self.stations.get(station_id).as_dict() if self.stations.get(station_id) else None,
            "group_count": self.stations.group_count(station_id),
            "zones": [zone.as_dict() for zone in zones],
            "order": [zone.id for zone in zones],
            "zone_distances": {
                zone.id: self.layouts.zone_distance(station_id, zone.id) for zone in zones
            },
            "span_m": spread_m(zones),
            "minimum_headway_s": self.schedules.minimum_headway(station_id) if schedule else 0,
            "revision": mapping.revision if mapping else 0,
            "groups": group_keys(self.mappings, station_id),
            "active_group": active_group(self.doors, station_id),
            "pressure": open_pressure(self.doors, station_id),
        }

    def resolve_group(self, station_id: str, group_key: str) -> dict:
        """解析一个逻辑门区。"""

        group = resolve(self.mappings, station_id, group_key)
        return {
            "station_id": station_id,
            "group_key": group_key,
            "group": group,
            "neighbours": adjacent(self.mappings, station_id, group_key),
        }

    def note_error(self, error: RailError) -> dict:
        """把平台异常写入审计，返回落盘记录。"""

        entry = self.audit.record(
            "error",
            "",
            "",
            component=error.component,
            message=error.message,
            error_type=type(error).__name__,
        )
        return entry.as_dict()

    def reset_state(self) -> bool:
        """清掉落盘的平台计数（交付前重新标定用）。"""

        if not self.store.exists("platform/state"):
            return False
        return self.store.remove("platform/state")

    # ---- 内部 ---------------------------------------------------------

    def _stop_position(self, station_id: str) -> int:
        """返回站台的停稳位置（取最近门区里程）。"""

        layout = self.layouts.get(station_id)
        zones = list(layout.zones) if layout else []
        nearest = nearest_zone(zones)
        return nearest.distance_m if nearest else 0

    def _primary_group(self, station_id: str) -> str:
        """返回站台的首个门区。"""

        keys = group_keys(self.mappings, station_id)
        return keys[0] if keys else ""

    def _primary_gate(self, station_id: str) -> PSD | None:
        """返回站台的首樘站台门。"""

        gates = self.gates.by_station(station_id)
        return gates[0] if gates else None

    def _adjacent_sample(self) -> dict:
        """给快照提供一个相邻关系样例。"""

        station_ids = self.namespace.station_ids()
        if not station_ids:
            return {}
        station_id = station_ids[0]
        keys = group_keys(self.mappings, station_id)
        if not keys:
            return {}
        return {keys[0]: adjacent(self.mappings, station_id, keys[0])}


def build_platform(
    config: PlatformConfig | None = None,
    clock: Clock | None = None,
    storage_root: str | None = None,
    unit: str | None = None,
) -> Platform:
    """构造一个平台；``storage_root`` 可覆盖配置里的存储目录。"""

    base = config or default_config()
    if storage_root is not None:
        base = with_storage_root(base, storage_root)
    return Platform(base, clock=clock, unit=unit)
