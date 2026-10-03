"""环境采集：传感器健康检查与冗余融合。

一条门区判定需要三个量：门区气温、门区相对湿度、门体玻璃温度。采集层对每个量
给出一个带质量标签的融合结果，判定层只消费融合结果而不碰原始读数：

``good``    读数可信（单路健康，或多路一致）；
``suspect`` 读数可疑（跳变、卡死、冗余不一致），不直接用于凝露判定；
``bad``     读数不可用（断线、长时间无数据、越界、漂移被坐实）。

扛故障的办法：

* 断线——驱动读不到值（``None``）或长时间无新采样（stale），立即 bad；
* 漂移——变化率异常先记 suspect，冗余通道持续不一致并各自伴有异常证据时，把
  漂移通道坐实为 bad，剩下的健康通道继续供数（部分失败可降级运行）；
* 卡死——长时间输出不变按 suspect 处理（雨后恒温是合法的，但不能拿来证明
  "没有凝露"）。

质量变化通过事件总线广播，并由协调器写进哈希链，采样原始值同样入链，保证事后
能区分"当时真没有凝露"和"当时根本看不见"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

GOOD = "good"


class Unconnected(RuntimeError):
    """通道尚未接线（区别于已接线但读不到值的断线）。"""


def unconnected() -> Optional[float]:
    """未接线通道的占位驱动：抛异常以便轮询层区分"没装"与"坏了"。"""

    raise Unconnected("channel not wired")


class DriverHolder:
    """可热替换的读数持有器。

    现场断线恢复、换探头时只换驱动函数，通道对象与其健康史（跳变、卡死、
    冗余分歧计数）保留，避免每换一次驱动就丢失全部观测上下文。
    """

    def __init__(self, func: Callable[[], Optional[float]] = unconnected) -> None:
        self._func = func

    def set(self, func: Callable[[], Optional[float]]) -> None:
        self._func = func

    def __call__(self) -> Optional[float]:
        return self._func()

    def wired(self) -> bool:
        """是否已接真实驱动（False 表示仍是未接线占位）。"""

        return self._func is not unconnected
SUSPECT = "suspect"
BAD = "bad"

# 合理量程，越界直接 bad。
RANGE = {
    "air_c": (-40.0, 80.0),
    "rh_pct": (0.0, 100.0),
    "glass_c": (-40.0, 90.0),
}
# 变化率软告警阈值（每分钟）。
ROC_LIMIT = {"air_c": 3.0, "rh_pct": 12.0, "glass_c": 4.0}
# 冗余通道分歧容忍。
DISAGREE_C = 2.0
DISAGREE_RH = 8.0
# 卡死判定窗口（秒）与"视作相同"的死区。
STUCK_WINDOW_S = 1800.0
STUCK_DEADBAND = 0.05
# 分歧持续多少次后坐实漂移通道（该通道需同时具备累计跳变证据）。
PEER_RUN_LIMIT = 3
# 坐实漂移所需的窗口内累计跳变次数。
SPIKE_RUN_LIMIT = 2


@dataclass(frozen=True)
class Channel:
    """一个物理通道：名字、量纲类型、读数驱动（返回 None 即断线）。"""

    name: str
    kind: str
    read: Callable[[], Optional[float]]

    def as_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind}


@dataclass
class _Point:
    ts: float
    value: float


@dataclass
class _ChannelState:
    """通道的运行期状态（最近读数、质量与证据）。"""

    channel: Channel
    history: list[_Point] = field(default_factory=list)
    quality: str = BAD  # 启动时尚无数据，按不可用起步。
    reasons: tuple[str, ...] = ("no_data",)
    disagree_run: int = 0
    spike_count: int = 0      # 观察窗内累计跳变次数（漂移坐实证据）
    last_change_ts: float = -1.0

    def evidence(self) -> dict:
        latest = self.history[-1] if self.history else None
        return {
            "channel": self.channel.name,
            "kind": self.channel.kind,
            "quality": self.quality,
            "reasons": list(self.reasons),
            "value": round(latest.value, 3) if latest else None,
            "ts": latest.ts if latest else None,
        }


@dataclass(frozen=True)
class FusedSample:
    """一个量纲的融合采样结果。"""

    kind: str
    value: Optional[float]
    quality: str
    ts: float
    reasons: tuple[str, ...] = ()
    channels: tuple[dict, ...] = ()

    def usable(self) -> bool:
        return self.quality == GOOD and self.value is not None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "value": None if self.value is None else round(self.value, 3),
            "quality": self.quality,
            "ts": self.ts,
            "reasons": list(self.reasons),
            "channels": [dict(item) for item in self.channels],
        }


class SensorMonitor:
    """按门区组织的传感器组：轮询、健康检查、冗余融合。"""

    def __init__(
        self,
        zone_id: str,
        channels: dict[str, list],
        history_limit: int = 64,
    ) -> None:
        if not {"air_c", "rh_pct", "glass_c"} <= set(channels):
            raise ValueError("传感器组缺少必要量纲（air_c/rh_pct/glass_c）")
        self.zone_id = zone_id
        self._kinds = ("air_c", "rh_pct", "glass_c")
        self._states: dict[str, list[_ChannelState]] = {
            kind: [_ChannelState(channel=self._as_channel(kind, entry)) for entry in channels.get(kind, [])]
            for kind in self._kinds
        }
        self._history_limit = max(4, history_limit)

    @staticmethod
    def _as_channel(kind: str, entry: object) -> Channel:
        """构造期既接受 :class:`Channel`，也接受重放用的通道名字符串。"""

        if isinstance(entry, Channel):
            return entry
        return Channel(name=str(entry), kind=kind, read=unconnected)

    def kinds(self) -> tuple[str, ...]:
        return self._kinds

    def channel_names(self) -> list[str]:
        return [
            state.channel.name
            for states in self._states.values()
            for state in states
        ]

    def poll(self, now: float, stale_after_s: float) -> dict[str, FusedSample]:
        """轮询全部通道并输出每个量纲的融合结果；同时推进健康状态。"""

        for kind, states in self._states.items():
            for state in states:
                self._poll_channel(state, now, stale_after_s)
        return {
            kind: self._fuse(kind, now)
            for kind in self._kinds
        }

    def replay_step(self, kind: str, sample: dict) -> FusedSample:
        """用入链的历史采样证据推进一步融合（重放专用）。

        健康检查（断线/跳变/漂移坐实）是实时侧的事，其结论已经连同采样一起
        入链；重放只重建融合，不再重新发明健康史，因此不会因为"重放时间轴上
        样本紧挨在一起"而得到不同的 ROC 结论。
        """

        now = float(sample.get("ts", 0.0))
        evidence = {item.get("channel"): item for item in sample.get("channels", [])}
        restored: list[_ChannelState] = []
        for state in self._states[kind]:
            item = evidence.get(state.channel.name)
            if item is not None:
                value = item.get("value")
                if value is not None:
                    state.history.append(_Point(now, float(value)))
                    self._trim(state)
                state.quality = str(item.get("quality", BAD))
                state.reasons = tuple(item.get("reasons", ()))
            restored.append(state)
        value = sample.get("value")
        quality = sample.get("quality", BAD)
        reasons = tuple(sample.get("reasons", ()))
        # 多通道时仍按历史证据重算分歧与池选择，保证降级/隔离逻辑可复现。
        if len(restored) > 1:
            return self._fuse_from(
                kind, now, restored,
                fallback_value=value, fallback_quality=quality, fallback_reasons=reasons,
            )
        return FusedSample(
            kind, value, quality, now, reasons,
            tuple(state.evidence() for state in restored),
        )

    # ---- 通道健康 -----------------------------------------------------

    def _poll_channel(self, state: _ChannelState, now: float, stale_after_s: float) -> None:
        channel = state.channel
        try:
            raw = channel.read()
        except Unconnected:
            # 未接线不是故障：保持当前质量，不产生新点，留给上层跳过该门区。
            return
        except Exception:  # 驱动抛错等同断线，不能让一个探头拖垮整条链。
            raw = None
        if raw is None:
            # 已有历史时保留旧点，由 stale 检查决定何时判坏；首次即坏。
            if not state.history or now - state.history[-1].ts > stale_after_s:
                state.quality, state.reasons = BAD, ("no_data",)
            return
        try:
            value = float(raw)
        except (TypeError, ValueError):
            state.quality, state.reasons = BAD, ("bad_value",)
            return
        low, high = RANGE[channel.kind]
        if not low <= value <= high:
            state.quality, state.reasons = BAD, ("out_of_range",)
            state.history.append(_Point(now, value))
            self._trim(state)
            return

        reasons: list[str] = []
        if state.history:
            previous = state.history[-1]
            age = max(1e-6, now - previous.ts)
            rate = abs(value - previous.value) / age * 60.0
            if rate > ROC_LIMIT[channel.kind]:
                reasons.append("rate_spike")
            # 统计"含本拍在内、最近卡死窗口内"的跳变次数。旧证据超出窗口后
            # 自然失效，避免一次早期跳变长期贴标签；累计多跳是慢漂移的证据。
            points = [*state.history, _Point(now, value)]
            state.spike_count = sum(
                1
                for earlier, later in zip(points, points[1:])
                if now - earlier.ts <= STUCK_WINDOW_S
                and abs(later.value - earlier.value)
                / max(1e-6, later.ts - earlier.ts) * 60.0 > ROC_LIMIT[channel.kind]
            )
        self._update_change(state, value, now)
        if state.last_change_ts >= 0 and now - state.last_change_ts >= STUCK_WINDOW_S:
            reasons.append("stuck")
        state.history.append(_Point(now, value))
        self._trim(state)
        state.reasons = tuple(reasons)
        # 已经被坐实的漂移（bad）不自动翻案。
        if state.quality == BAD and "drift" in state.reasons:
            return
        # 单通道跳变不降级质量：来雨、车门贯入都会让真实读数快速变化，只有冗余
        # 通道在跳变同时持续分歧才由 _resolve_peers 坐实为漂移。卡死（长期恒定）
        # 不能用来证明"安全"，保留为 suspect。跳变标记随证据入链，供事后核查。
        state.quality = SUSPECT if "stuck" in reasons else GOOD

    def _update_change(self, state: _ChannelState, value: float, now: float) -> None:
        if not state.history or abs(value - state.history[-1].value) > STUCK_DEADBAND:
            state.last_change_ts = now

    def _trim(self, state: _ChannelState) -> None:
        if len(state.history) > self._history_limit:
            del state.history[: len(state.history) - self._history_limit]

    # ---- 融合 ---------------------------------------------------------

    def _fuse(self, kind: str, now: float) -> FusedSample:
        states = self._states[kind]
        live = [state for state in states if state.history]
        if not live:
            return FusedSample(kind, None, BAD, now, ("no_data",), self._evidence(states))
        # 单通道：直接采用其质量。
        if len(states) == 1:
            only = states[0]
            return FusedSample(
                kind, only.history[-1].value, only.quality, only.history[-1].ts,
                only.reasons, self._evidence(states),
            )
        # 多通道：最新值两两比对，分歧需要持续若干拍才坐实漂移。
        self._resolve_peers(kind, live)
        return self._fuse_from(kind, now, states)

    def _fuse_from(
        self,
        kind: str,
        now: float,
        states: list[_ChannelState],
        fallback_value: object = None,
        fallback_quality: str = BAD,
        fallback_reasons: tuple[str, ...] = (),
    ) -> FusedSample:
        """按通道当前状态做冗余融合。"""

        healthy = [state for state in states if state.quality != BAD]
        good = [state for state in healthy if state.quality == GOOD]
        pool = [state for state in (good or healthy) if state.history]
        if not pool:
            return FusedSample(
                kind, fallback_value, BAD, now,
                fallback_reasons or ("all_channels_bad",), self._evidence(states),
            )
        value = sum(state.history[-1].value for state in pool) / len(pool)
        ts = min(state.history[-1].ts for state in pool)
        reasons = {reason for state in pool for reason in state.reasons}
        reasons.update(fallback_reasons)
        if any(state.quality == BAD for state in states):
            reasons.add("channel_isolated")
        if any(state.quality == SUSPECT for state in pool):
            quality = SUSPECT
        elif len(pool) < len(states):
            quality = SUSPECT  # 降级运行：有通道被隔离，剩余值不用于自动放开。
        else:
            quality = GOOD
        if fallback_quality == SUSPECT and quality == GOOD:
            quality = SUSPECT
        return FusedSample(kind, value, quality, ts, tuple(sorted(reasons)), self._evidence(states))

    def _resolve_peers(self, kind: str, live: list[_ChannelState]) -> None:
        latest = [(state, state.history[-1].value) for state in live]
        disagree = self._disagreement(kind, [value for _, value in latest])
        for state, _value in latest:
            if disagree:
                state.disagree_run += 1
            else:
                state.disagree_run = 0
            if disagree and state.disagree_run >= PEER_RUN_LIMIT:
                # 坐实条件：分歧持续 + 该通道窗口内累计跳变达到漂移证据线。
                # 健康对端只记录一次"曾有分歧"，不把它打成可疑（噪声来自坏通道）。
                already = state.quality == BAD and "drift" in state.reasons
                if already or state.spike_count >= SPIKE_RUN_LIMIT:
                    state.quality = BAD
                    state.reasons = tuple(dict.fromkeys((*state.reasons, "drift")))
                elif "peer_disagree" not in state.reasons:
                    state.reasons = tuple(dict.fromkeys((*state.reasons, "peer_disagree")))

    @staticmethod
    def _disagreement(kind: str, values: list[float]) -> bool:
        if len(values) < 2:
            return False
        tolerance = DISAGREE_RH if kind == "rh_pct" else DISAGREE_C
        return max(values) - min(values) > tolerance

    @staticmethod
    def _evidence(states: list[_ChannelState]) -> tuple[dict, ...]:
        return tuple(state.evidence() for state in states)
