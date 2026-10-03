"""擦拭与检修工单。

摘除动作必须接上闭环，否则门会被永远挂在"摘出"状态：

* 凝露高风险摘门 → 开 **擦拭工单**（WIPE），清扫班组擦玻璃；
* 传感器断线/漂移摘门 → 开 **检修工单**（REPAIR），擦玻璃没有用，要修传感器。

工单状态机：``OPEN``（已派单，未完成）→ ``DONE``（班组回报已擦/已修，等待
风险恢复确认）→ ``CLOSED``（风险已回到安全区且稳满放行时长，门重新放回批量
开门）。执行（派单通道）失败不改变工单状态，留在 ``OPEN`` 等下个周期重派；
擦拭回报完成但裕量没恢复，工单停在 ``DONE``，门继续摘着——"人擦了"和"玻璃
不结露了"是两件事，恢复以判据为准，不以工单回报为准。

同一次结露事件（同一 ``incident_id``）在一次摘除周期内只有一张工单；擦完
恢复放开后再次起雾算新事件、开新单，审计链上能数出这樘门雨季擦了几次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..errors import RailError
from ..store import JsonStore

ORDER_WIPE = "wipe"
ORDER_REPAIR = "repair"

STATUS_OPEN = "open"
STATUS_DONE = "done"
STATUS_CLOSED = "closed"
STATUS_FAILED = "dispatch_failed"


class WipeStatus:
    """工单状态常量。"""

    OPEN = STATUS_OPEN
    DONE = STATUS_DONE
    CLOSED = STATUS_CLOSED
    FAILED = STATUS_FAILED


@dataclass(frozen=True)
class WipeOrder:
    """一张擦拭/检修工单。"""

    id: str
    psd_id: str
    station_id: str
    kind: str
    status: str
    incident_id: str
    created_at: float
    opened_at: float | None = None
    done_at: float | None = None
    closed_at: float | None = None
    dispatch_attempts: int = 0
    last_error: str = ""
    assignee: str = ""
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "psd_id": self.psd_id,
            "station_id": self.station_id,
            "kind": self.kind,
            "status": self.status,
            "incident_id": self.incident_id,
            "created_at": self.created_at,
            "opened_at": self.opened_at,
            "done_at": self.done_at,
            "closed_at": self.closed_at,
            "dispatch_attempts": self.dispatch_attempts,
            "last_error": self.last_error,
            "assignee": self.assignee,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "WipeOrder":
        return cls(
            id=str(payload.get("id", "")),
            psd_id=str(payload.get("psd_id", "")),
            station_id=str(payload.get("station_id", "")),
            kind=str(payload.get("kind", ORDER_WIPE)),
            status=str(payload.get("status", STATUS_OPEN)),
            incident_id=str(payload.get("incident_id", "")),
            created_at=float(payload.get("created_at", 0.0)),
            opened_at=payload.get("opened_at"),
            done_at=payload.get("done_at"),
            closed_at=payload.get("closed_at"),
            dispatch_attempts=int(payload.get("dispatch_attempts", 0)),
            last_error=str(payload.get("last_error", "")),
            assignee=str(payload.get("assignee", "")),
            note=str(payload.get("note", "")),
        )


class WipeStore:
    """工单登记表，落在 ``wipe/<order_id>`` 文档（与门控事实同一存储）。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    @staticmethod
    def _doc_id(order_id: str) -> str:
        return f"wipe/{order_id}"

    def save(self, order: WipeOrder) -> WipeOrder:
        if not order.id:
            raise RailError("工单标识为空", component="condensation")
        self._store.write(self._doc_id(order.id), order.as_dict())
        return order

    def get(self, order_id: str) -> WipeOrder | None:
        raw = self._store.read(self._doc_id(order_id))
        return WipeOrder.from_dict(raw) if isinstance(raw, dict) else None

    def require(self, order_id: str) -> WipeOrder:
        order = self.get(order_id)
        if order is None:
            raise RailError(f"工单不存在: {order_id}", component="condensation")
        return order

    def list(self) -> list[WipeOrder]:
        found: list[WipeOrder] = []
        for doc_id in self._store.list_ids():
            if not doc_id.startswith("wipe/") or doc_id.count("/") != 1:
                continue
            raw = self._store.read(doc_id)
            if isinstance(raw, dict):
                found.append(WipeOrder.from_dict(raw))
        return sorted(found, key=lambda item: item.created_at)

    def open_for(self, psd_id: str, incident_id: str) -> WipeOrder | None:
        """返回某事件当前未关闭的工单（一次摘除周期最多一张）。"""

        for order in self.list():
            if (
                order.psd_id == psd_id
                and order.incident_id == incident_id
                and order.status != STATUS_CLOSED
            ):
                return order
        return None

    def active_for(self, psd_id: str) -> list[WipeOrder]:
        """返回某门所有未关闭工单。"""

        return [
            order
            for order in self.list()
            if order.psd_id == psd_id and order.status != STATUS_CLOSED
        ]


class CleaningDispatcher(Protocol):
    """外部清扫/检修派单通道协议（短信、班组终端、工单系统…）。"""

    def dispatch(self, order: WipeOrder) -> None:
        """派发成功返回，失败抛异常（工单保留，下周期重试）。"""
        ...


class WipePlanner:
    """工单生命周期编排：建单、重派、回报、关闭。"""

    def __init__(
        self,
        store: WipeStore,
        ids_factory: Callable[[], str],
        dispatcher: CleaningDispatcher | None = None,
    ) -> None:
        self._store = store
        self._next_id = ids_factory
        self._dispatcher = dispatcher

    def set_dispatcher(self, dispatcher: CleaningDispatcher) -> None:
        self._dispatcher = dispatcher

    def all_orders(self) -> list[WipeOrder]:
        """返回全部工单（按创建时间）。"""

        return self._store.list()

    def active_orders(self, psd_id: str) -> list[WipeOrder]:
        """返回某门所有未关闭工单。"""

        return self._store.active_for(psd_id)

    def create(
        self,
        psd_id: str,
        station_id: str,
        kind: str,
        incident_id: str,
        at: float,
        note: str = "",
    ) -> WipeOrder:
        """为一次摘除事件建工单；已有未关闭工单则直接返回（幂等）。"""

        existing = self._store.open_for(psd_id, incident_id)
        if existing is not None:
            return existing
        order = WipeOrder(
            id=self._next_id(),
            psd_id=psd_id,
            station_id=station_id,
            kind=kind,
            status=STATUS_OPEN,
            incident_id=incident_id,
            created_at=at,
            note=note,
        )
        return self._store.save(order)

    def dispatch_pending(self, at: float) -> list[dict]:
        """重派所有 OPEN 工单（含上周期派单失败的），逐条返回结果。

        部分失败隔离：一张派不出去不影响其他门，失败次数落盘，工单留在
        OPEN 等下一周期；没配派单通道时按"已通知"落 OPEN（现场人工模式）。
        """

        results: list[dict] = []
        for order in self._store.list():
            if order.status != STATUS_OPEN:
                continue
            attempts = order.dispatch_attempts
            if self._dispatcher is None:
                results.append({"order_id": order.id, "psd_id": order.psd_id, "ok": True,
                                "attempts": attempts, "mode": "manual"})
                continue
            attempts += 1
            try:
                self._dispatcher.dispatch(order)
            except Exception as error:  # 派单通道挂了：留单、计数、下周期再来
                failed = WipeOrder(
                    **{**order.as_dict(), "status": STATUS_OPEN, "dispatch_attempts": attempts,
                       "last_error": str(error)[:300]}
                )
                self._store.save(failed)
                results.append({"order_id": order.id, "psd_id": order.psd_id, "ok": False,
                                "attempts": attempts, "error": str(error)[:300]})
                continue
            saved = WipeOrder(
                **{**order.as_dict(), "dispatch_attempts": attempts, "last_error": "",
                   "opened_at": order.opened_at or at}
            )
            self._store.save(saved)
            results.append({"order_id": order.id, "psd_id": order.psd_id, "ok": True,
                            "attempts": attempts})
        return results

    def report_done(self, order_id: str, at: float, *, assignee: str = "", note: str = "") -> WipeOrder:
        """班组回报已擦/已修：进入 DONE，等判据确认恢复，门不立刻放开。"""

        order = self._store.require(order_id)
        if order.status == STATUS_CLOSED:
            raise RailError(f"工单 {order_id} 已关闭", component="condensation")
        done = WipeOrder(**{
            **order.as_dict(),
            "status": STATUS_DONE,
            "done_at": order.done_at or at,
            "assignee": assignee or order.assignee,
            "note": note or order.note,
        })
        return self._store.save(done)

    def close(self, order_id: str, at: float, note: str = "") -> WipeOrder:
        """判据确认恢复后关闭工单（放开动作的工单侧记录）。"""

        order = self._store.require(order_id)
        if order.status != STATUS_DONE:
            raise RailError(
                f"工单 {order_id} 未完成回报，不能关闭（当前 {order.status}）",
                component="condensation",
            )
        closed = WipeOrder(**{**order.as_dict(), "status": STATUS_CLOSED, "closed_at": at,
                              "note": note or order.note})
        return self._store.save(closed)

    def reopen(self, order_id: str, at: float, reason: str) -> WipeOrder:
        """DONE 后雾又起来（恢复确认没通过）：工单退回 OPEN 重擦。"""

        order = self._store.require(order_id)
        if order.status == STATUS_CLOSED:
            raise RailError(f"工单 {order_id} 已关闭，应开新事件新单", component="condensation")
        reopened = WipeOrder(**{
            **order.as_dict(),
            "status": STATUS_OPEN,
            "done_at": None,
            "note": (order.note + f" | 退回重派: {reason}").strip(" |"),
        })
        return self._store.save(reopened)
