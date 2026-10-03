"""凝露摘除（把不安全的门从批量开门里摘出来）。

摘除是"规划 + 执行"两道防线：

1. 批量开门规划（:func:`psdline.door.batch.batch_open`）调用 :meth:`HoldRegistry.held_ids`
   把被摘的门从开门名单里剔除，按门记录 ``skipped`` 原因，绝不静默漏开；
2. 门区联动真正开门前（:func:`psdline.door.group.open_group`）再查一次
   :meth:`HoldRegistry.is_held`，防止状态变化后用旧规划把门打开。

摘除原因两种：``risk``（凝露高风险）走擦拭流程，``sensor``（环境数据不可信）
走检修流程；高风险后又断线时原因升级为 ``sensor``（擦玻璃修不好传感器），
反之断线期间风险仍高也不降级。恢复由擦拭/检修闭环显式放行，状态机自己不摘
门也不自动放门——摘/放都是审计事件。

紧急情况下值班员可以 :meth:`force_release` 强开放行，但必须登记操作人与理由，
放行事件照常进哈希链，事后能追是谁在看不清的情况下开的门。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import InterlockError

HOLD_RISK = "risk"
HOLD_SENSOR = "sensor"


class HoldReason:
    """摘除原因常量。"""

    RISK = HOLD_RISK
    SENSOR = HOLD_SENSOR


@dataclass(frozen=True)
class Hold:
    """一樘门当前的摘除状态。"""

    psd_id: str
    reason: str
    since: float
    incident_id: str
    profile_version: str = ""
    margin_c: float | None = None
    level: str = ""
    detail: str = ""
    updated: float = 0.0

    def as_dict(self) -> dict:
        return {
            "psd_id": self.psd_id,
            "reason": self.reason,
            "since": self.since,
            "incident_id": self.incident_id,
            "profile_version": self.profile_version,
            "margin_c": None if self.margin_c is None else round(self.margin_c, 4),
            "level": self.level,
            "detail": self.detail,
            "updated": self.updated or self.since,
        }


class HoldRegistry:
    """凝露摘除登记表（内存态，由哈希链重放恢复）。"""

    def __init__(self) -> None:
        self._holds: dict[str, Hold] = {}

    # ---- 摘/放 --------------------------------------------------------

    def hold_for_risk(
        self,
        psd_id: str,
        since: float,
        incident_id: str,
        profile_version: str,
        margin_c: float,
        detail: str = "",
    ) -> tuple[Hold, bool]:
        """因凝露高风险摘门；已处于传感器摘除时只刷新不降级。

        返回 ``(摘除状态, 是否为新摘除)``。
        """

        existing = self._holds.get(psd_id)
        if existing is not None and existing.reason == HOLD_SENSOR:
            updated = Hold(
                psd_id=psd_id,
                reason=HOLD_SENSOR,
                since=existing.since,
                incident_id=existing.incident_id,
                profile_version=existing.profile_version,
                margin_c=margin_c,
                level="high",
                detail=existing.detail,
                updated=since,
            )
            self._holds[psd_id] = updated
            return updated, False
        if existing is not None and existing.reason == HOLD_RISK:
            return existing, False
        hold = Hold(
            psd_id=psd_id,
            reason=HOLD_RISK,
            since=since,
            incident_id=incident_id,
            profile_version=profile_version,
            margin_c=margin_c,
            level="high",
            detail=detail,
        )
        self._holds[psd_id] = hold
        return hold, True

    def hold_for_sensor(
        self,
        psd_id: str,
        since: float,
        incident_id: str,
        profile_version: str,
        detail: str = "",
    ) -> tuple[Hold, bool]:
        """因环境数据不可信摘门（fail-safe）；风险摘除可升级为此。"""

        existing = self._holds.get(psd_id)
        if existing is not None and existing.reason == HOLD_SENSOR:
            return existing, False
        hold = Hold(
            psd_id=psd_id,
            reason=HOLD_SENSOR,
            since=since,
            incident_id=incident_id,
            profile_version=profile_version,
            level="unknown",
            detail=detail,
        )
        self._holds[psd_id] = hold
        return hold, (existing is None)

    def release(self, psd_id: str) -> Hold | None:
        """闭环恢复后放行，返回被移除的摘除状态；本来就没摘返回 ``None``。"""

        return self._holds.pop(psd_id, None)

    def force_release(self, psd_id: str) -> Hold | None:
        """强开等价于放行，区别只由调用方把操作人与理由写进审计链。"""

        return self._holds.pop(psd_id, None)

    def restore(self, hold: Hold) -> None:
        """重放链时直接重建摘除状态。"""

        self._holds[hold.psd_id] = hold

    # ---- 查询 ---------------------------------------------------------

    def get(self, psd_id: str) -> Hold | None:
        return self._holds.get(psd_id)

    def is_held(self, psd_id: str) -> bool:
        return psd_id in self._holds

    def held_ids(self) -> list[str]:
        return sorted(self._holds)

    def risk_ids(self) -> list[str]:
        return sorted(p for p, h in self._holds.items() if h.reason == HOLD_RISK)

    def sensor_ids(self) -> list[str]:
        return sorted(p for p, h in self._holds.items() if h.reason == HOLD_SENSOR)

    def list(self) -> list[Hold]:
        return [self._holds[key] for key in sorted(self._holds)]

    def reason_of(self, psd_id: str) -> str:
        """返回门控批量开门使用的摘除原因码；未摘除返回空串。"""

        hold = self._holds.get(psd_id)
        if hold is None:
            return ""
        _allowed, code = self.gate_open_allowed(psd_id)
        return code

    def gate_open_allowed(self, psd_id: str) -> tuple[bool, str]:
        """门控第二道防线：被摘的门一律不允许开，拒绝原因带回给批量开门。"""

        hold = self._holds.get(psd_id)
        if hold is None:
            return True, ""
        if hold.reason == HOLD_RISK:
            return False, "condensation_risk"
        return False, "env_sensor_fault"

    def assert_open_allowed(self, psd_id: str) -> None:
        """门区联动开门前的硬性检查，被摘直接抛联锁异常。"""

        allowed, reason = self.gate_open_allowed(psd_id)
        if not allowed:
            label = "凝露高风险" if reason == "condensation_risk" else "环境传感器不可信"
            raise InterlockError(f"门 {psd_id} 已因{label}摘除: {reason}", component="condensation")
