"""凝露链编排器。

:class:`EnvController` 把采集、判据、摘除、擦拭与哈希链缝成一条链，平台每个
控制周期调用一次 :meth:`poll`：

    适配器读全部传感器（单次批量，部分失败只影响失败的传感器）
      → SensorMonitor 逐门解析（断线/越界/毛刺/卡值/漂移）
      → 原始采样上链（CHAIN_SAMPLE，原始值 + 健康判定）
      → DoorEnvEngine 逐门判定（瞬时持续 + 趋势加速 + 迟滞放行）
      → 判定事实上链（CHAIN_FACT，含当时画像/判据版本、全部理由）
      → 状态迁移：摘门（risk/sensor）、恢复放行、擦拭工单全生命周期
      → 未派工单重派（部分失败隔离，逐单返回成败）

重启恢复不依赖易失内存：:meth:`restore_from_chain` 重放链上的采样与事件重建
传感器健康、引擎窗口、摘除表和工单表；链校验不过直接拒绝启动。
"""

from __future__ import annotations

from typing import Callable

from ..clock import Clock
from ..errors import RailError
from ..ids import IdFactory
from ..store import JsonStore
from .chain import (
    CHAIN_FACT,
    CHAIN_HOLD,
    CHAIN_SAMPLE,
    CHAIN_WIPE,
    ChainJournal,
    Entry,
)
from .config import CondensationConfig, profile_from_dict
from .engine import (
    RISK_HIGH,
    RISK_UNKNOWN,
    RISK_WATCH,
    DoorEnvEngine,
    RiskFact,
)
from .hold import HOLD_RISK, HOLD_SENSOR, Hold, HoldRegistry
from .sensors import (
    KIND_AIR_T,
    KIND_RH,
    KIND_SURFACE,
    DoorEnvInput,
    EnvAdapter,
    SensorMonitor,
)
from .wiping import (
    ORDER_REPAIR,
    ORDER_WIPE,
    STATUS_CLOSED,
    STATUS_DONE,
    CleaningDispatcher,
    WipeOrder,
    WipePlanner,
    WipeStore,
)

BINDING_DOC = "condensation/bindings"


class EnvController:
    """门区凝露风险链控制器（每平台一个）。"""

    def __init__(
        self,
        *,
        config: CondensationConfig,
        clock: Clock,
        store: JsonStore,
        adapter: EnvAdapter | None = None,
        audit: object | None = None,
        dispatcher: CleaningDispatcher | None = None,
        chain: ChainJournal | None = None,
    ) -> None:
        self.config = config.validate()
        self._clock = clock
        self._store = store
        self._adapter = adapter
        self._audit_log = audit
        self.monitor = SensorMonitor(self.config.profile)
        self.holds = HoldRegistry()
        self.wipes = WipePlanner(
            WipeStore(store),
            IdFactory("wipe").next,
            dispatcher=dispatcher,
        )
        self.chain = chain or ChainJournal(
            store.root, segment_limit=self.config.chain_segment
        )
        self._engines: dict[str, DoorEnvEngine] = {}
        self._bindings: dict[str, dict[str, list[str]]] = self._load_bindings()
        self._stations: dict[str, str] = {}
        self._last_inputs: dict[str, DoorEnvInput] = {}
        self._last_dispatch: list[dict] = []
        self.ticks = 0

    # ---- 装配 ---------------------------------------------------------

    def set_adapter(self, adapter: EnvAdapter) -> None:
        self._adapter = adapter

    def adapter_is_active(self) -> bool:
        return self._adapter is not None

    def bind_door(
        self,
        psd_id: str,
        station_id: str,
        *,
        air_t: str,
        rh: str,
        surface: str | list[str],
    ) -> None:
        """绑定一樘门的环境传感器；表面温度可给冗余多只。"""

        surface_ids = [surface] if isinstance(surface, str) else list(surface)
        if not air_t or not rh or not surface_ids:
            raise RailError(f"门 {psd_id} 环境传感器绑定不完整", component="condensation")
        self._bindings[psd_id] = {
            KIND_AIR_T: [air_t],
            KIND_RH: [rh],
            KIND_SURFACE: surface_ids,
        }
        self._stations[psd_id] = station_id
        self._save_bindings()

    def bindings(self) -> dict[str, dict[str, list[str]]]:
        return {key: {k: list(v) for k, v in value.items()} for key, value in self._bindings.items()}

    def bound_ids(self) -> list[str]:
        return sorted(self._bindings)

    def station_of(self, psd_id: str) -> str:
        return self._stations.get(psd_id, "")

    def _load_bindings(self) -> dict[str, dict[str, list[str]]]:
        raw = self._store.read(BINDING_DOC)
        if not isinstance(raw, dict):
            return {}
        result: dict[str, dict[str, list[str]]] = {}
        for psd_id, binding in raw.get("doors", {}).items():
            if not isinstance(binding, dict):
                continue
            result[psd_id] = {
                kind: list(binding.get(kind, []))
                for kind in (KIND_AIR_T, KIND_RH, KIND_SURFACE)
            }
        self._stations = dict(raw.get("stations", {}))
        return result

    def _save_bindings(self) -> None:
        self._store.write(
            BINDING_DOC,
            {"doors": self._bindings, "stations": self._stations},
        )

    def _engine(self, psd_id: str) -> DoorEnvEngine:
        engine = self._engines.get(psd_id)
        if engine is None:
            engine = DoorEnvEngine(psd_id, self.config.window_s)
            self._engines[psd_id] = engine
        return engine

    # ---- 主链 ---------------------------------------------------------

    def poll(self, ts: float | None = None) -> dict:
        """推进一个采集节拍：采集 → 判定 → 摘除/恢复 → 派单。"""

        if self._adapter is None:
            raise RailError("未配置环境采集适配器", component="condensation")
        moment = self._clock.now() if ts is None else float(ts)
        self.ticks += 1
        readings = self._adapter.read(moment)

        facts: list[RiskFact] = []
        transitions: list[dict] = []
        for psd_id in sorted(self._bindings):
            binding = self._bindings[psd_id]
            env_input = self.monitor.resolve(psd_id, readings, binding, moment)
            self._last_inputs[psd_id] = env_input
            self._append_sample(moment, psd_id, env_input)
            profile = self.config.profile(moment)
            fact = self._engine(psd_id).evaluate(env_input, profile)
            self.chain.append(moment, CHAIN_FACT, psd_id, {"fact": fact.as_dict()})
            facts.append(fact)
            transitions.extend(self._apply_fact(psd_id, fact, moment, profile))

        dispatched = self.wipes.dispatch_pending(moment)
        self._last_dispatch = dispatched
        return {
            "ok": True,
            "ts": moment,
            "tick": self.ticks,
            "doors": len(self._bindings),
            "facts": [fact.as_dict() for fact in facts],
            "transitions": transitions,
            "dispatched": dispatched,
            "held": [hold.as_dict() for hold in self.holds.list()],
            "suspect_sensors": self.monitor.suspect_ids(),
        }

    def _append_sample(self, ts: float, psd_id: str, env_input: DoorEnvInput) -> None:
        """原始采样上链：逐只传感器的原始值/健康状态与解析结果一并冻结。

        健康明细里带每只传感器当时的 value、status、usable 与断线/越界原因，
        重放时无需再访问现场适配器即可完整重建解析输入与风险判定。
        """

        self.chain.append(
            ts,
            CHAIN_SAMPLE,
            psd_id,
            {
                "input": env_input.as_dict(),
                "sensor_states": self.monitor.states(),
                "profile_version": self.config.profile(ts).version,
            },
        )

    def _apply_fact(self, psd_id: str, fact: RiskFact, ts: float,
                    profile: CondensationConfig | object | None = None) -> list[dict]:
        """把判定事实翻译成摘除/工单/放行事件，返回本拍事件列表。

        ``profile`` 用于区分 HIGH 里的两种情形：裕量仍在危险区 vs 已回落安全区
        但放行稳定时长没攒够（迟滞等待）。后者不能退回重擦。
        """

        events: list[dict] = []
        station_id = self._stations.get(psd_id, "")
        existing = self.holds.get(psd_id)
        release_line = getattr(profile, "release_margin_c", -2.0)
        still_dangerous = fact.margin_c is None or fact.margin_c >= release_line

        if fact.level == RISK_HIGH:
            # DONE 的擦拭单又遇到真实雾情（裕量还在危险区）：退回重擦。
            # 裕量已回落安全区、只是迟滞放行时间没满时不退回。
            if still_dangerous:
                for order in self.wipes.active_orders(psd_id):
                    if order.status == STATUS_DONE and order.kind == ORDER_WIPE:
                        reopened = self.wipes.reopen(order.id, ts, "擦拭后凝露裕量再次越线")
                        self._wipe_event(ts, psd_id, reopened, "reopen")
                        events.append({"psd_id": psd_id, "event": "wipe_reopen", "order_id": reopened.id})
            hold, is_new = self.holds.hold_for_risk(
                psd_id, ts, incident_id=(existing.incident_id if existing else f"inc-{psd_id}-{self.ticks}"),
                profile_version=fact.profile_version,
                margin_c=fact.margin_c if fact.margin_c is not None else 0.0,
                detail="；".join(fact.reasons),
            )
            if is_new:
                self._hold_event(ts, hold, "hold_risk")
                self._write_audit("condensation_hold", psd_id, station_id, reason="risk")
                order = self.wipes.create(
                    psd_id, station_id, ORDER_WIPE, hold.incident_id, ts,
                    note="凝露高风险：门区玻璃起雾，从批量开门摘除并安排擦拭",
                )
                self._wipe_event(ts, psd_id, order, "create")
                events.append({"psd_id": psd_id, "event": "hold", "reason": "risk",
                               "incident_id": hold.incident_id, "order_id": order.id})
            return events

        if fact.level == RISK_UNKNOWN:
            hold, is_new = self.holds.hold_for_sensor(
                psd_id, ts,
                incident_id=(existing.incident_id if existing else f"inc-{psd_id}-{self.ticks}"),
                profile_version=fact.profile_version,
                detail="；".join(fact.reasons),
            )
            if is_new:
                self._hold_event(ts, hold, "hold_sensor")
                self._write_audit("condensation_hold", psd_id, station_id, reason="sensor")
                order = self.wipes.create(
                    psd_id, station_id, ORDER_REPAIR, hold.incident_id, ts,
                    note="环境数据不可信（断线/漂移/越界）：fail-safe 摘除并安排检修",
                )
                self._wipe_event(ts, psd_id, order, "create")
                events.append({"psd_id": psd_id, "event": "hold", "reason": "sensor",
                               "incident_id": hold.incident_id, "order_id": order.id})
            return events

        # 风险解除（normal/watch）：只在确实有摘除、且对应工单都已回报完成时放行。
        if existing is None:
            return events
        blockers = [
            order for order in self.wipes.active_orders(psd_id)
            if order.status != STATUS_DONE
        ]
        if blockers:
            return [{
                "psd_id": psd_id,
                "event": "await_completion",
                "orders": [order.id for order in blockers],
            }]
        release_note = "擦拭/检修完成且凝露裕量恢复并稳定"
        released = self.holds.release(psd_id)
        if released is not None:
            self.chain.append(
                ts, CHAIN_HOLD, psd_id,
                {"action": "release", "hold": released.as_dict(), "note": release_note},
            )
            self._write_audit("condensation_release", psd_id, station_id, reason=released.reason)
        for order in self.wipes.active_orders(psd_id):
            closed = self.wipes.close(order.id, ts, note=release_note)
            self._wipe_event(ts, psd_id, closed, "close")
            events.append({"psd_id": psd_id, "event": "release", "order_id": closed.id,
                           "incident_id": released.incident_id if released else order.incident_id})
        return events

    # ---- 人工动作 -----------------------------------------------------

    def report_wiped(self, psd_id: str, at: float | None = None, *, assignee: str = "",
                     note: str = "") -> dict:
        """班组回报某门当前摘除事件的工单已擦/已修（不代表门已放开）。"""

        moment = self._clock.now() if at is None else float(at)
        hold = self.holds.get(psd_id)
        if hold is None:
            raise RailError(f"门 {psd_id} 当前没有摘除中的事件", component="condensation")
        orders = self.wipes.active_orders(psd_id)
        if not orders:
            raise RailError(f"门 {psd_id} 没有进行中的工单", component="condensation")
        done_orders = []
        for order in orders:
            if order.status == STATUS_DONE:
                done_orders.append(order)
                continue
            done = self.wipes.report_done(order.id, moment, assignee=assignee, note=note)
            self._wipe_event(moment, psd_id, done, "report_done")
            done_orders.append(done)
        self._write_audit("condensation_wipe_done", psd_id, self._stations.get(psd_id, ""),
                    assignee=assignee or "现场班组")
        # 回报后立即用最近一次输入复判，恢复条件满足则当场放开。
        self._try_release_now(psd_id, moment)
        return {
            "ok": True,
            "psd_id": psd_id,
            "orders": [order.id for order in done_orders],
            "held": self.holds.is_held(psd_id),
        }

    def _try_release_now(self, psd_id: str, ts: float) -> None:
        engine = self._engines.get(psd_id)
        fact = engine.last_fact if engine else None
        if fact is not None and fact.level not in (RISK_HIGH, RISK_UNKNOWN):
            self._apply_fact(psd_id, fact, ts, self.config.profile(ts))

    def force_release(
        self,
        psd_id: str,
        operator: str,
        reason: str,
        at: float | None = None,
    ) -> dict:
        """值班员紧急强开：必须登记操作人与理由，链上留痕。"""

        if not operator or not reason:
            raise RailError("强开必须登记操作人与理由", component="condensation")
        moment = self._clock.now() if at is None else float(at)
        hold = self.holds.get(psd_id)
        released = self.holds.force_release(psd_id)
        if released is None:
            return {"ok": True, "psd_id": psd_id, "already_clear": True}
        self.chain.append(
            moment, CHAIN_HOLD, psd_id,
            {"action": "force_release", "hold": hold.as_dict(),
             "operator": operator, "reason": reason},
        )
        self._write_audit("condensation_force_release", psd_id, self._stations.get(psd_id, ""),
                    operator=operator, reason=reason)
        return {"ok": True, "psd_id": psd_id, "incident_id": hold.incident_id,
                "operator": operator, "reason": reason}

    # ---- 重放/恢复 ----------------------------------------------------

    def restore_from_chain(self) -> dict:
        """从哈希链重建全部内存状态（重启入口）。

        采样（SAMPLE）重建传感器健康与引擎窗口，判定（FACT）重算校验，摘除
        表由 HOLD 事件折叠，工单表从 ``wipe/*.json`` 当前态文档读取。链被
        篡改或复算结论与历史不一致时抛异常拒绝启动，绝不带着被改过的历史跑。
        """

        from .replay import replay_chain

        # 始终从磁盘重新读链做校验：内存对象可能在文件被外部改动后仍保留旧视图。
        disk_chain = ChainJournal(self.chain_segments_root(), segment_limit=self.config.chain_segment)
        result = replay_chain(disk_chain, self.config, self._bindings)
        if result.errors:
            raise RailError(
                f"凝露哈希链校验失败 {len(result.errors)} 处，拒绝启动: "
                f"{result.errors[0].as_dict()}",
                component="condensation",
            )
        if result.fact_mismatches:
            raise RailError(
                f"凝露判据重放与历史判定不一致 {len(result.fact_mismatches)} 处: "
                f"{result.fact_mismatches[0]}",
                component="condensation",
            )
        self.monitor = result.monitor
        self._engines = result.engines
        self.holds = result.holds
        # 磁盘链校验通过后，把链视图刷新为磁盘版本（可能有外部合法追加）。
        self.chain = disk_chain
        self.ticks = result.tick_count
        self._last_inputs = dict(result.last_inputs)
        return {
            "ok": True,
            "entries": len(result.entries),
            "samples": result.sample_count,
            "facts": result.fact_count,
            "held": [hold.as_dict() for hold in self.holds.list()],
            "open_orders": [
                order.id
                for order in self.wipes.all_orders()
                if order.status != STATUS_CLOSED
            ],
        }

    # ---- 查询 ---------------------------------------------------------

    def door_view(self, psd_id: str) -> dict:
        engine = self._engines.get(psd_id)
        env_input = self._last_inputs.get(psd_id)
        hold = self.holds.get(psd_id)
        orders = self.wipes.active_orders(psd_id)
        return {
            "psd_id": psd_id,
            "station_id": self._stations.get(psd_id, ""),
            "binding": self._bindings.get(psd_id, {}),
            "level": engine.level if engine else "uninitialized",
            "hold": hold.as_dict() if hold else None,
            "last_input": env_input.as_dict() if env_input else None,
            "last_fact": engine.last_fact.as_dict() if engine and engine.last_fact else None,
            "active_orders": [order.as_dict() for order in orders],
        }

    def summary(self) -> dict:
        return {
            "ticks": self.ticks,
            "doors_bound": len(self._bindings),
            "held": [hold.as_dict() for hold in self.holds.list()],
            "held_ids": self.holds.held_ids(),
            "risk_ids": self.holds.risk_ids(),
            "sensor_hold_ids": self.holds.sensor_ids(),
            "suspect_sensors": self.monitor.suspect_ids(),
            "sensor_states": self.monitor.states(),
            "orders": [order.as_dict() for order in self.wipes.all_orders()],
            "chain_entries": self.chain.count(),
            "chain_tail": self.chain.tail_hash(),
            "last_dispatch": self._last_dispatch,
            "active_profile": self.config.profile(self._clock.now()).version,
        }

    def env_snapshot(self) -> dict:
        return {psd_id: self.door_view(psd_id) for psd_id in sorted(self._bindings)}

    # ---- 内部 ---------------------------------------------------------

    def chain_segments_root(self) -> str:
        """返回哈希链分段文件所在目录。"""

        return self._store.root

    def _hold_event(self, ts: float, hold: Hold, action: str) -> Entry:
        return self.chain.append(
            ts, CHAIN_HOLD, hold.psd_id,
            {"action": action, "hold": hold.as_dict()},
        )

    def _wipe_event(self, ts: float, psd_id: str, order: WipeOrder, action: str) -> Entry:
        return self.chain.append(
            ts, CHAIN_WIPE, psd_id,
            {"action": action, "order": order.as_dict()},
        )

    def _write_audit(self, kind: str, psd_id: str, station_id: str, **detail: object) -> object | None:
        if self._audit_log is None:
            return None
        return self._audit_log.record(kind, "", station_id, psd_id=psd_id, **detail)
