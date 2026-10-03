"""环境采集与传感器健康判定。

每樘门绑定：一只（或多只共用的）空气温湿度传感器，以及至少一只门体表面温度
传感器，表面侧建议冗余。采集层要扛住三类现场问题：

断线      适配器直接报错，或读数时间戳老于画像 ``stale_after_s``；
坏值/漂移  超物理量程、变化率超限（毛刺，丢点）、长时间不变（卡值）、冗余
          表面传感器彼此持续偏离（离群者隔离）；
部分失败   单只传感器失败只隔离它自己；可用表面传感器少于
          ``min_surface_sensors`` 或门区空气通道不可用时，整樘门判
          ``UNKNOWN``——按有风险摘除并开检修单，绝不"没数据当没事"。

适配器只负责"读"，健康状态由 :class:`SensorMonitor` 在控制器内累积，这样
断线恢复后只需连续读到若干好点即可自动撤销隔离，不需要重启或人工复位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Callable, Protocol

from ..errors import RailError
from .config import AIR_RANGE_C, RH_RANGE, SURFACE_RANGE_C, CondensationProfile
from .physics import dew_point_c, margin_c

KIND_AIR_T = "air_t"
KIND_RH = "rh"
KIND_SURFACE = "surface"

VALID_KINDS = (KIND_AIR_T, KIND_RH, KIND_SURFACE)

HEALTH_OK = "ok"
HEALTH_STALE = "stale"
HEALTH_OUT_OF_RANGE = "out_of_range"
HEALTH_SPIKE = "spike"
HEALTH_STUCK = "stuck"
HEALTH_DRIFT = "drift"

INPUT_OK = "ok"
INPUT_UNKNOWN = "unknown"

# 撤销隔离（suspect）前需要连续读到的好点数（5s 节拍下约半分钟）。
RECOVER_GOOD_POINTS = 6
STUCK_EPSILON = 1e-9


@dataclass(frozen=True)
class Reading:
    """一只传感器在某时刻的原始读数；``ok=False`` 表示采集失败（断线）。"""

    sensor_id: str
    kind: str
    ts: float
    value: float | None = None
    ok: bool = True
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "sensor_id": self.sensor_id,
            "kind": self.kind,
            "ts": round(self.ts, 3),
            "value": None if self.value is None else round(self.value, 4),
            "ok": self.ok,
            "error": self.error,
        }


# 编排层按"门区环境读数"称呼同一种对象，保留语义别名。
AmbientReading = Reading


class EnvAdapter(Protocol):
    """环境采集适配器协议：一次性读出本周期能拿到的全部传感器。"""

    def read(self, ts: float) -> dict[str, Reading]:
        """返回 ``sensor_id -> 读数``；读失败给 ``ok=False`` 或直接缺席。"""
        ...


class _SensorState:
    """单只传感器的滚动健康状态。"""

    def __init__(self, sensor_id: str, kind: str) -> None:
        self.sensor_id = sensor_id
        self.kind = kind
        self.last_ts: float | None = None
        self.last_value: float | None = None
        self.stuck_count = 0
        self.suspect = False
        self.suspect_reason = ""
        self.good_streak = 0
        self.last_status = HEALTH_STALE

    def as_dict(self) -> dict:
        return {
            "sensor_id": self.sensor_id,
            "kind": self.kind,
            "suspect": self.suspect,
            "suspect_reason": self.suspect_reason,
            "last_status": self.last_status,
            "last_ts": self.last_ts,
            "last_value": self.last_value,
            "stuck_count": self.stuck_count,
            "good_streak": self.good_streak,
        }


@dataclass(frozen=True)
class SensorHealth:
    """一次健康评估后某只传感器的结论。"""

    sensor_id: str
    kind: str
    status: str
    usable: bool
    value: float | None = None
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "sensor_id": self.sensor_id,
            "kind": self.kind,
            "status": self.status,
            "usable": self.usable,
            "value": None if self.value is None else round(self.value, 4),
            "detail": self.detail,
        }


class SensorMonitor:
    """滚动维护全部传感器健康状态，并把一批原始读数解析成可判定的输入。"""

    def __init__(self, profile_getter: Callable[[float], CondensationProfile]) -> None:
        self._profile_for = profile_getter
        self._states: dict[str, _SensorState] = {}
        # 同一只传感器在同一个采集节拍可能被多樘门共用；同一 (sensor, ts)
        # 只评估一次，其余门复用结论，避免卡值/跳变计数被门数放大。
        self._verdict_cache: dict[str, tuple[float, SensorHealth]] = {}

    def _state(self, reading: Reading) -> _SensorState:
        state = self._states.get(reading.sensor_id)
        if state is None:
            state = _SensorState(reading.sensor_id, reading.kind)
            self._states[reading.sensor_id] = state
        return state

    def states(self) -> dict[str, dict]:
        """返回全部传感器状态快照（供状态接口与审计）。"""

        return {sid: state.as_dict() for sid, state in sorted(self._states.items())}

    def suspect_ids(self) -> list[str]:
        return sorted(sid for sid, state in self._states.items() if state.suspect)

    def evaluate(self, reading: Reading, ts: float) -> SensorHealth:
        """评估单只传感器本拍读数；同 (sensor, ts) 的重复请求复用结论。"""

        cached = self._verdict_cache.get(reading.sensor_id)
        if cached is not None and cached[0] == ts:
            return cached[1]
        verdict = self._evaluate_raw(reading, ts)
        self._verdict_cache[reading.sensor_id] = (ts, verdict)
        return verdict

    def _evaluate_raw(self, reading: Reading, ts: float) -> SensorHealth:
        """评估单只传感器本周期读数，更新其滚动状态。"""

        if reading.kind not in VALID_KINDS:
            raise RailError(f"未知传感器类型: {reading.kind}", component="condensation")
        profile = self._profile_for(ts)
        state = self._state(reading)

        # 1) 断线：适配器报错（读数缺席由 mark_missing 处理）。
        if not reading.ok or reading.value is None:
            state.good_streak = 0
            state.last_status = HEALTH_STALE
            return SensorHealth(
                reading.sensor_id, reading.kind, HEALTH_STALE, False,
                detail=reading.error or "采集失败",
            )

        value = float(reading.value)
        age = ts - reading.ts

        # 2) 过期：读数时间戳太老（采到缓存值也按断线处理）。
        if reading.ts > ts + 1.0 or age > profile.stale_after_s:
            state.good_streak = 0
            state.last_status = HEALTH_STALE
            return SensorHealth(
                reading.sensor_id, reading.kind, HEALTH_STALE, False,
                detail=f"读数过期 {age:.1f}s",
            )

        # 3) 越界：物理上不可能，隔离后等连续好点恢复。
        low, high = _range_of(reading.kind)
        if not low <= value <= high:
            self._quarantine(state, HEALTH_OUT_OF_RANGE, f"读数 {value:.2f} 越界 [{low},{high}]")
            return SensorHealth(
                reading.sensor_id, reading.kind, HEALTH_OUT_OF_RANGE, False, value,
                state.suspect_reason,
            )

        # 4) 尖峰毛刺：变化率超限只丢本点，不改变隔离状态。
        if state.last_value is not None and state.last_ts is not None:
            dt = reading.ts - state.last_ts
            if dt > 0 and abs(value - state.last_value) / dt > profile.rate_limit_c_per_s:
                state.last_status = HEALTH_SPIKE
                state.good_streak = 0
                return SensorHealth(
                    reading.sensor_id, reading.kind, HEALTH_SPIKE, False, value,
                    detail=(
                        f"变化率 {abs(value - state.last_value) / dt:.2f} ℃/s 超限，"
                        "按毛刺丢点"
                    ),
                )

        # 5) 卡值：长时间一字不变（传感器死在常值、链路却还是通的）。
        if state.last_value is not None and abs(value - state.last_value) <= STUCK_EPSILON:
            state.stuck_count += 1
        else:
            state.stuck_count = 0
        if state.stuck_count >= profile.stuck_limit:
            self._quarantine(state, HEALTH_STUCK, f"连续 {state.stuck_count} 点卡值 {value:.2f}")
            return SensorHealth(
                reading.sensor_id, reading.kind, HEALTH_STUCK, False, value,
                state.suspect_reason,
            )

        # 好点：推进窗口，连续恢复后自动撤销隔离。
        state.last_value = value
        state.last_ts = reading.ts
        state.good_streak += 1
        if state.suspect and state.good_streak >= RECOVER_GOOD_POINTS:
            state.suspect = False
            state.suspect_reason = ""
            state.stuck_count = 0
        state.last_status = HEALTH_OK
        return SensorHealth(reading.sensor_id, reading.kind, HEALTH_OK, not state.suspect, value)

    def mark_missing(self, sensor_id: str, kind: str, ts: float) -> SensorHealth:
        """本周期完全读不到某只传感器（缺席即断线）。"""

        cached = self._verdict_cache.get(sensor_id)
        if cached is not None and cached[0] == ts:
            return cached[1]
        placeholder = Reading(sensor_id, kind, ts, ok=False, error="本周期无读数")
        state = self._state(placeholder)
        state.good_streak = 0
        state.last_status = HEALTH_STALE
        verdict = SensorHealth(sensor_id, kind, HEALTH_STALE, False, detail="读数缺席")
        self._verdict_cache[sensor_id] = (ts, verdict)
        return verdict

    @staticmethod
    def _quarantine(state: _SensorState, reason: str, detail: str) -> None:
        state.suspect = True
        state.suspect_reason = detail
        state.last_status = reason
        state.good_streak = 0

    def resolve(
        self,
        psd_id: str,
        readings: dict[str, Reading],
        bindings: dict[str, list[str]],
        ts: float,
    ) -> "DoorEnvInput":
        """把一樘门绑定的全部传感器读数解析成一个可判定输入。

        ``bindings`` 形如 ``{"air_t": ["s-1"], "rh": ["s-2"], "surface": [...]}``。
        """

        profile = self._profile_for(ts)
        health: list[SensorHealth] = []
        values: dict[str, list[float]] = {}
        rejected: list[SensorHealth] = []

        for kind in (KIND_AIR_T, KIND_RH, KIND_SURFACE):
            for sensor_id in bindings.get(kind, ()):
                reading = readings.get(sensor_id)
                if reading is None:
                    verdict = self.mark_missing(sensor_id, kind, ts)
                else:
                    verdict = self.evaluate(reading, ts)
                health.append(verdict)
                if verdict.usable and verdict.value is not None:
                    values.setdefault(kind, []).append(verdict.value)
                else:
                    rejected.append(verdict)

        return self._build_input(psd_id, values, health, rejected, bindings, profile, ts)

    def _build_input(
        self,
        psd_id: str,
        values: dict[str, list[float]],
        health: list[SensorHealth],
        rejected: list[SensorHealth],
        bindings: dict[str, list[str]],
        profile: CondensationProfile,
        ts: float,
    ) -> "DoorEnvInput":
        reasons: list[str] = [item.detail for item in rejected if item.detail]
        surface = list(values.get(KIND_SURFACE, ()))

        # 冗余表面传感器持续偏离：隔离离群者，避免一只漂移件拉偏整扇门。
        drift_ids: list[str] = []
        if len(surface) >= 2 and (max(surface) - min(surface)) > profile.surface_divergence_c:
            center = median(surface)
            surface_items = [item for item in health if item.kind == KIND_SURFACE]
            worst = max(
                surface_items,
                key=lambda item: abs((item.value if item.value is not None else 0.0) - center),
            )
            state = self._states.get(worst.sensor_id)
            detail = (
                f"表面温度离群：与中位数偏离 "
                f"{abs((worst.value if worst.value is not None else 0.0) - center):.2f} ℃"
            )
            if state is not None:
                self._quarantine(state, HEALTH_DRIFT, detail)
            drift_ids.append(worst.sensor_id)
            reasons.append(f"表面传感器 {worst.sensor_id} 漂移隔离")
            surface = [
                item.value
                for item in surface_items
                if item.usable and item.sensor_id != worst.sensor_id and item.value is not None
            ]

        air_values = values.get(KIND_AIR_T, ())
        rh_values = values.get(KIND_RH, ())
        usable = True
        if not air_values:
            usable = False
            reasons.append("门区空气温度不可用")
        if not rh_values:
            usable = False
            reasons.append("门区湿度不可用")
        if len(surface) < profile.min_surface_sensors:
            usable = False
            reasons.append(f"可用表面传感器 {len(surface)} < 要求 {profile.min_surface_sensors}")

        if not usable:
            return DoorEnvInput(
                psd_id=psd_id, ts=ts, usable=False, status=INPUT_UNKNOWN,
                reasons=reasons, health=health, drift_ids=drift_ids,
            )

        air_t = float(median(air_values))
        rh = float(median(rh_values))
        surface_t = float(median(surface))
        tdp = dew_point_c(air_t, rh)
        return DoorEnvInput(
            psd_id=psd_id,
            ts=ts,
            usable=True,
            status=INPUT_OK,
            air_t=air_t,
            rh=rh,
            surface_t=surface_t,
            dew_point_c=tdp,
            margin_c=margin_c(air_t, rh, surface_t),
            reasons=reasons,
            health=health,
            drift_ids=drift_ids,
        )


def _range_of(kind: str) -> tuple[float, float]:
    if kind == KIND_AIR_T:
        return AIR_RANGE_C
    if kind == KIND_RH:
        return RH_RANGE
    return SURFACE_RANGE_C


@dataclass(frozen=True)
class DoorEnvInput:
    """一樘门在一个采集节拍上的可判定环境输入。"""

    psd_id: str
    ts: float
    usable: bool
    status: str
    air_t: float | None = None
    rh: float | None = None
    surface_t: float | None = None
    dew_point_c: float | None = None
    margin_c: float | None = None
    reasons: list[str] = field(default_factory=list)
    health: list[SensorHealth] = field(default_factory=list)
    drift_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "psd_id": self.psd_id,
            "ts": round(self.ts, 3),
            "usable": self.usable,
            "status": self.status,
            "air_t": _round(self.air_t),
            "rh": _round(self.rh),
            "surface_t": _round(self.surface_t),
            "dew_point_c": _round(self.dew_point_c),
            "margin_c": _round(self.margin_c),
            "reasons": list(self.reasons),
            "drift_ids": list(self.drift_ids),
            "sensor_health": [item.as_dict() for item in self.health],
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


class ScriptedAdapter:
    """供演示与验证使用的脚本化适配器。

    ``script`` 把每只传感器映射成 ``(kind, ts -> float | None | Reading)``；
    返回 ``None`` 或函数抛异常即按断线处理。也可用 :meth:`fail` /
    :meth:`recover` 显式制造采集故障。
    """

    def __init__(
        self,
        script: dict[str, tuple[str, Callable[[float], object]]] | None = None,
    ) -> None:
        self._kinds: dict[str, str] = {}
        self._funcs: dict[str, Callable[[float], object]] = {}
        self._failed: set[str] = set()
        self._fail_error: dict[str, str] = {}
        for sensor_id, (kind, func) in (script or {}).items():
            self.add_sensor(sensor_id, kind, func)

    def add_sensor(self, sensor_id: str, kind: str, func: Callable[[float], object]) -> None:
        if kind not in VALID_KINDS:
            raise RailError(f"未知传感器类型: {kind}", component="condensation")
        self._kinds[sensor_id] = kind
        self._funcs[sensor_id] = func

    def fail(self, sensor_id: str, error: str = "适配器读取失败") -> None:
        self._failed.add(sensor_id)
        self._fail_error[sensor_id] = error

    def recover(self, sensor_id: str) -> None:
        self._failed.discard(sensor_id)
        self._fail_error.pop(sensor_id, None)

    def read(self, ts: float) -> dict[str, Reading]:
        result: dict[str, Reading] = {}
        for sensor_id, kind in self._kinds.items():
            if sensor_id in self._failed:
                result[sensor_id] = Reading(
                    sensor_id, kind, ts, ok=False,
                    error=self._fail_error.get(sensor_id, "适配器读取失败"),
                )
                continue
            try:
                value = self._funcs[sensor_id](ts)
            except Exception as error:  # 现场适配器自身异常等价于断线
                result[sensor_id] = Reading(sensor_id, kind, ts, ok=False, error=str(error))
                continue
            if isinstance(value, Reading):
                result[sensor_id] = value
            elif value is None:
                result[sensor_id] = Reading(sensor_id, kind, ts, ok=False, error="无返回值")
            else:
                result[sensor_id] = Reading(sensor_id, kind, ts, float(value))
        return result

    def sensor_ids(self) -> dict[str, str]:
        return dict(self._kinds)


def resolve_door_input(
    monitor: SensorMonitor,
    psd_id: str,
    readings: dict[str, Reading],
    bindings: dict[str, list[str]],
    ts: float,
) -> DoorEnvInput:
    """模块级入口：见 :meth:`SensorMonitor.resolve`。"""

    return monitor.resolve(psd_id, readings, bindings, ts)
