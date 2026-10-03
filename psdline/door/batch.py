"""批量开门。

批量开门按门区里程由近及远执行，并受批量上限约束：超限时只开最近的那一批，剩下
的留给下一个周期。被联锁锁定的门区直接跳过并记进 ``skipped``，不会静默漏开。

凝露摘除接入后，被摘门在批量开门规划里逐樘剔除：同组其他门照开，被摘的门进
``held`` 明细（逐门记录是 ``condensation_risk`` 还是 ``env_sensor_fault``），
整组都被摘时该组进 ``skipped``。这是第一道防线；真正执行开门的
:func:`psdline.door.group.open_group` 还会再查一次摘除状态。
"""

from __future__ import annotations

from ..station import DoorZone, open_order
from .door import DoorController


def batch_open(
    controller: DoorController,
    station_id: str,
    zones: list[DoorZone],
    holds: object | None = None,
) -> dict:
    """按里程顺序批量开门；``holds`` 为凝露摘除登记表时逐门摘除。"""

    order = open_order(zones)
    limit = controller.batch_limit()
    scheduled = order[:limit]
    deferred = order[limit:]
    opened: list[str] = []
    skipped: dict[str, str] = {}
    held: dict[str, list[str]] = {}
    held_gates: dict[str, str] = {}
    for group in scheduled:
        gate_ids = [gate.id for gate in controller.gates.by_station(station_id) if gate.group == group]
        if not gate_ids:
            skipped[group] = "gate_missing"
            continue
        locked_ids = [gate_id for gate_id in gate_ids if controller.lockouts.is_locked(gate_id)]
        if locked_ids:
            skipped[group] = "locked"
            continue
        clear_ids: list[str] = []
        group_held: list[str] = []
        for gate_id in gate_ids:
            if holds is not None and holds.is_held(gate_id):
                group_held.append(gate_id)
                held_gates[gate_id] = holds.reason_of(gate_id)
            else:
                clear_ids.append(gate_id)
        if not clear_ids:
            skipped[group] = held_gates.get(gate_ids[0], "condensation_risk")
            held[group] = group_held
            continue
        for gate_id in clear_ids:
            controller.gates.set_open(gate_id, True)
        opened.append(group)
        if group_held:
            held[group] = group_held
    return {
        "station_id": station_id,
        "order": order,
        "opened": opened,
        "deferred": deferred,
        "skipped": skipped,
        "held": held,
        "held_gates": held_gates,
        "limit": limit,
    }
