"""擦拭与人工核查工单。

RISK 摘门的同时生成擦拭工单（WIPE），DARK 摘门生成人工核查工单（INSPECT，
目的是恢复采集可信，而不是擦掉不存在的证据）。工单有完整生命周期，每次派单、
接收、完成、失败都写进哈希链：

* RISK 期间只允许一张未完成 WIPE；擦拭完成后仍有风险，按冷却时间安排下一次；
* DARK 期间只允许一张未完成 INSPECT；
* 派单可能失败（清扫调度不可用），失败后按固定间隔重试，不会悄悄丢掉；
* 放开（RISK/DARK -> OK）时取消仍未完成的工单。

调度器是纯内存状态 + 事件落盘；重启后由事件重放恢复工单（见 replay 模块）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .assess import DARK, RISK

WIPE = "WIPE"
INSPECT = "INSPECT"

OPEN = "open"          # 已生成，等待调度下发
DISPATCHED = "dispatched"  # 已派给班组
DONE = "done"
FAILED = "failed"      # 班组上报失败，等待重试
CANCELLED = "cancelled"

RETRY_INTERVAL_S = 60.0


@dataclass
class WorkOrder:
    """一张擦拭/核查工单。"""

    id: str
    zone_id: str
    kind: str
    reason: str
    created_ts: float
    state: str = OPEN
    dispatched_ts: Optional[float] = None
    completed_ts: Optional[float] = None
    attempts: int = 0
    note: str = ""
    history: list[dict] = field(default_factory=list)

    def active(self) -> bool:
        return self.state in (OPEN, DISPATCHED, FAILED)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "zone_id": self.zone_id,
            "kind": self.kind,
            "reason": self.reason,
            "created_ts": self.created_ts,
            "state": self.state,
            "dispatched_ts": self.dispatched_ts,
            "completed_ts": self.completed_ts,
            "attempts": self.attempts,
            "note": self.note,
            "history": list(self.history),
        }


# 派单网关：入参 (工单, 时间戳)，返回 (是否受理, 备注)。失败要可重试。
Dispatcher = Callable[[WorkOrder, float], tuple[bool, str]]


class CleaningPlanner:
    """按门区维护工单状态机。"""

    def __init__(self, id_prefix: str = "wo") -> None:
        self._orders: dict[str, WorkOrder] = {}
        self._seq = 0
        self._prefix = id_prefix

    def _new_id(self) -> str:
        self._seq += 1
        return f"{self._prefix}-{self._seq:05d}"

    def list_orders(self, zone_id: str | None = None) -> list[WorkOrder]:
        orders = self._orders.values()
        if zone_id is not None:
            orders = [order for order in orders if order.zone_id == zone_id]
        return sorted(orders, key=lambda item: item.created_ts)

    def active_order(self, zone_id: str, kind: str) -> Optional[WorkOrder]:
        for order in self._orders.values():
            if order.zone_id == zone_id and order.kind == kind and order.active():
                return order
        return None

    def wipe_in_progress(self, zone_id: str) -> bool:
        return self.active_order(zone_id, WIPE) is not None

    def ensure_for_state(
        self,
        zone_id: str,
        state: str,
        reasons: tuple[str, ...],
        now: float,
        cooldown_s: float = 0.0,
    ) -> Optional[WorkOrder]:
        """风险/黑暗进入时确保有对应工单；其他状态不管。

        ``cooldown_s`` 给出最近一张同类型工单完成后的抑制窗口：擦完短时间内
        风险仍在不自动再派（是否复擦由班组上报失败驱动），避免工单自我繁殖。
        """

        if state == RISK and self.active_order(zone_id, WIPE) is None:
            if self._within_done_cooldown(zone_id, WIPE, now, cooldown_s):
                return None
            order = WorkOrder(
                id=self._new_id(),
                zone_id=zone_id,
                kind=WIPE,
                reason=",".join(reasons) or "condensation_risk",
                created_ts=now,
            )
            order.history.append({"ts": now, "state": OPEN})
            self._orders[order.id] = order
            return order
        if state == DARK and self.active_order(zone_id, INSPECT) is None:
            if self._within_done_cooldown(zone_id, INSPECT, now, cooldown_s):
                return None
            order = WorkOrder(
                id=self._new_id(),
                zone_id=zone_id,
                kind=INSPECT,
                reason=",".join(reasons) or "sensor_untrusted",
                created_ts=now,
            )
            order.history.append({"ts": now, "state": OPEN})
            self._orders[order.id] = order
            return order
        return None

    def _within_done_cooldown(
        self, zone_id: str, kind: str, now: float, cooldown_s: float
    ) -> bool:
        if cooldown_s <= 0:
            return False
        for order in self.list_orders(zone_id):
            if (
                order.kind == kind
                and order.state == DONE
                and order.completed_ts is not None
                and now - order.completed_ts < cooldown_s
            ):
                return True
        return False

    def dispatch_due(self, now: float, dispatch: Dispatcher) -> list[tuple[WorkOrder, bool, str]]:
        """把待派/失败重试的工单发出去，返回每个工单的派单结果。"""

        results: list[tuple[WorkOrder, bool, str]] = []
        for order in list(self._orders.values()):
            if order.state == OPEN or (
                order.state == FAILED
                and (order.dispatched_ts is None or now - order.dispatched_ts >= RETRY_INTERVAL_S)
            ):
                order.attempts += 1
                accepted, note = dispatch(order, now)
                order.note = note
                if accepted:
                    order.state = DISPATCHED
                    order.dispatched_ts = now
                else:
                    order.state = FAILED
                    order.dispatched_ts = now
                order.history.append(
                    {"ts": now, "state": order.state, "attempt": order.attempts, "note": note}
                )
                results.append((order, accepted, note))
        return results

    def report_done(self, order_id: str, now: float, note: str = "") -> Optional[WorkOrder]:
        """班组上报工单完成。"""

        order = self._orders.get(order_id)
        if order is None or not order.active():
            return None
        order.state = DONE
        order.completed_ts = now
        order.note = note or order.note
        order.history.append({"ts": now, "state": DONE, "note": note})
        return order

    def report_failed(self, order_id: str, now: float, note: str = "") -> Optional[WorkOrder]:
        """班组上报失败（擦完仍起雾/探头没修好），进入重试。"""

        order = self._orders.get(order_id)
        if order is None or not order.active():
            return None
        order.state = FAILED
        order.note = note or order.note
        order.history.append({"ts": now, "state": FAILED, "note": note})
        return order

    def cancel_active(self, zone_id: str, kind: str, now: float) -> list[WorkOrder]:
        """门区恢复正常时取消在途工单。"""

        cancelled: list[WorkOrder] = []
        for order in self.list_orders(zone_id):
            if order.kind == kind and order.active():
                order.state = CANCELLED
                order.history.append({"ts": now, "state": CANCELLED})
                cancelled.append(order)
        return cancelled

    def restore(self, order: WorkOrder) -> None:
        """重放恢复用：直接装入一张工单。"""

        self._orders[order.id] = order
        self._seq += 1
