"""门区凝露风险判定（纯状态机，实时链路与事后重放共用）。

输入只有"门区气温/湿度/门体温度的融合采样 + 当时生效策略 + 时间戳"，不做任何 IO，
因此同一段输入必定得到同一段结论——这是可重放的基础。

数据质量分三档，判定处理方式不同：

``good``    全部通道可信；
``suspect`` 有可用读数但质量可疑（跳变、卡死、冗余分歧、坏通道被隔离后的降级）；
``bad``     某必需量完全没有可用值（断线、越界、漂移被坐实）。

判定规则（取舍说明见包文档）：

* **物理线瞬时触发**：门体温度已低于露点（margin <= 物理线）立即 RISK，不等趋势；
* **趋势稳态触发**：指数平滑余量持续低于预警线达到确认时长，进入 RISK；
* **迟滞放开**：平滑余量持续高于解除线、数据质量恢复为 good、且没有擦拭在途，
  达到更长的确认时长才回到 OK；
* **数据黑暗 DARK**：必需量 bad 时摘门但不下"已凝露"结论，只派人工核查；
  suspect 不算盲——继续算物理量（真实降雨也会让读数剧烈变化），物理线触发
  仍能报 RISK，但 suspect 期间不允许自动放开，必须 requalify。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .physics import condensation_margin_c
from .policy import DewPolicy
from .sensors import FusedSample, GOOD

OK = "OK"        # 正常，门可进入批量开门
RISK = "RISK"    # 凝露风险：摘门 + 安排擦拭
DARK = "DARK"    # 数据不可信：保守摘门 + 安排人工核查（不下凝露结论）


@dataclass
class Assessment:
    """一次判定的完整证据。"""

    state: str
    ts: float
    policy_version: str
    confidence: str                      # good / degraded / blind
    margin_c: Optional[float]            # 瞬时物理余量
    ewma_margin_c: Optional[float]      # 平滑余量
    threshold_warn_c: float
    threshold_clear_c: float
    threshold_trip_c: float
    state_since: float
    wipe_in_progress: bool
    reasons: tuple[str, ...] = ()
    inputs: dict = field(default_factory=dict)

    def exposed(self) -> bool:
        """该门区当前是否应从批量开门中摘除（RISK 与 DARK 都摘）。"""

        return self.state in (RISK, DARK)

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "ts": self.ts,
            "policy_version": self.policy_version,
            "confidence": self.confidence,
            "margin_c": None if self.margin_c is None else round(self.margin_c, 3),
            "ewma_margin_c": None if self.ewma_margin_c is None else round(self.ewma_margin_c, 3),
            "threshold_warn_c": self.threshold_warn_c,
            "threshold_clear_c": self.threshold_clear_c,
            "threshold_trip_c": self.threshold_trip_c,
            "state_since": self.state_since,
            "wipe_in_progress": self.wipe_in_progress,
            "reasons": list(self.reasons),
            "inputs": self.inputs,
        }


class RiskAssessor:
    """单门区的凝露风险状态机。"""

    def __init__(self, zone_id: str) -> None:
        self.zone_id = zone_id
        self.state = DARK                   # 冷启动先保守，数据健康后自动放开。
        self.state_since: Optional[float] = None
        # 条件持续起点（与状态解耦）：低余量/高余量各自计时，中断即清零。
        self.below_since: Optional[float] = None
        self.above_since: Optional[float] = None
        self.ewma: Optional[float] = None
        self.good_since: Optional[float] = None  # 可信数据持续起点（DARK 恢复用）
        self.last: Optional[Assessment] = None

    def process(
        self,
        policy: DewPolicy,
        air: FusedSample,
        rh: FusedSample,
        glass: FusedSample,
        now: float,
        wipe_in_progress: bool = False,
    ) -> Assessment:
        """推进一步判定。"""

        samples = {"air_c": air, "rh_pct": rh, "glass_c": glass}
        blind = any(sample.value is None or sample.quality == "bad" for sample in samples.values())
        degraded = any(sample.quality != GOOD for sample in samples.values())
        margin = (
            condensation_margin_c(glass.value, air.value, rh.value)
            if not blind and air.value is not None and rh.value is not None and glass.value is not None
            else None
        )

        if self.state == DARK:
            return self._process_dark(
                policy, samples, margin, blind, degraded, now, wipe_in_progress
            )

        # 已在 OK/RISK：数据变盲 -> DARK（fail-safe），不更新平滑余量。
        if blind:
            return self._go_dark(policy, samples, now, wipe_in_progress)
        assert margin is not None
        self._update_ewma(policy, margin)
        ewma = self.ewma if self.ewma is not None else margin

        instant_trip = margin <= policy.instant_trip_margin_c
        below_warn = ewma < policy.warn_margin_c
        above_clear = ewma >= policy.clear_margin_c
        self.below_since = now if below_warn and self.below_since is None else (
            self.below_since if below_warn else None
        )
        self.above_since = now if above_clear and self.above_since is None else (
            self.above_since if above_clear else None
        )
        reasons = self._reasons(instant_trip, below_warn, degraded)

        if self.state == OK:
            if instant_trip:
                return self._assess(RISK, policy, margin, now, wipe_in_progress, reasons, samples)
            if below_warn and self._cond_dwelt(self.below_since, now, policy.trip_dwell_s):
                return self._assess(RISK, policy, margin, now, wipe_in_progress, reasons, samples)
            if below_warn:
                return self._assess(
                    OK, policy, margin, now, wipe_in_progress, ("watching", *reasons), samples
                )
            return self._assess(
                OK, policy, margin, now, wipe_in_progress,
                ("degraded_inputs",) if degraded else (), samples,
            )

        # 已在 RISK：放开要同时满足 高余量持续 + 数据恢复 good + 无擦拭在途。
        if above_clear and not degraded and not wipe_in_progress and self._cond_dwelt(
            self.above_since, now, policy.clear_dwell_s
        ):
            return self._assess(OK, policy, margin, now, wipe_in_progress, (), samples)
        held_reasons = list(reasons)
        if not above_clear:
            held_reasons.append("holding_risk")
        if above_clear and degraded:
            held_reasons.append("awaiting_good_quality")
        if wipe_in_progress:
            held_reasons.append("wipe_in_progress")
        elif above_clear:
            held_reasons.append("clearing")
        return self._assess(
            RISK, policy, margin, now, wipe_in_progress, tuple(dict.fromkeys(held_reasons)), samples
        )

    # ---- DARK 子流程 --------------------------------------------------

    def _process_dark(
        self,
        policy: DewPolicy,
        samples: dict,
        margin: Optional[float],
        blind: bool,
        degraded: bool,
        now: float,
        wipe_in_progress: bool,
    ) -> Assessment:
        if blind:
            return self._go_dark(policy, samples, now, wipe_in_progress)
        assert margin is not None
        if self.good_since is None:
            # 第一批可信数据：播种平滑器并开始健康观察，不把黑暗期算进观察时长。
            self.ewma = margin
            self.good_since = now
            return self._assess(
                DARK, policy, margin, now, wipe_in_progress, ("requalifying",), samples
            )
        if now - self.good_since < policy.requalify_s:
            self._update_ewma(policy, margin)
            return self._assess(
                DARK, policy, margin, now, wipe_in_progress, ("requalifying",), samples
            )
        # 观察期满。物理线瞬时风险优先（即使数据可疑也要先摘）。
        instant_trip = margin <= policy.instant_trip_margin_c
        if instant_trip:
            self._update_ewma(policy, margin)
            return self._assess(
                RISK, policy, margin, now, wipe_in_progress, ("instant_trip",), samples
            )
        if degraded:
            # 有可用读数但质量还在降级：继续保守占用，等待质量恢复重新计时。
            self.good_since = now
            self.ewma = margin
            return self._assess(
                DARK, policy, margin, now, wipe_in_progress,
                ("requalifying", "degraded_inputs"), samples,
            )
        self._update_ewma(policy, margin)
        ewma = self.ewma if self.ewma is not None else margin
        if ewma < policy.warn_margin_c:
            return self._assess(
                RISK, policy, margin, now, wipe_in_progress, ("below_warn_trend",), samples
            )
        return self._assess(OK, policy, margin, now, wipe_in_progress, (), samples)

    # ---- 内部 ---------------------------------------------------------

    @staticmethod
    def _reasons(instant_trip: bool, below_warn: bool, degraded: bool) -> tuple[str, ...]:
        reasons: list[str] = []
        if instant_trip:
            reasons.append("instant_trip")
        if below_warn:
            reasons.append("below_warn_trend")
        if degraded:
            reasons.append("degraded_inputs")
        return tuple(reasons)

    def _update_ewma(self, policy: DewPolicy, margin: float) -> None:
        if self.ewma is None:
            self.ewma = margin
        else:
            self.ewma = policy.ewma_alpha * margin + (1.0 - policy.ewma_alpha) * self.ewma

    @staticmethod
    def _cond_dwelt(since: Optional[float], now: float, dwell_s: float) -> bool:
        return since is not None and now - since >= dwell_s

    def _go_dark(
        self,
        policy: DewPolicy,
        samples: dict,
        now: float,
        wipe_in_progress: bool,
    ) -> Assessment:
        reasons = tuple(
            dict.fromkeys(
                reason
                for sample in samples.values()
                for reason in (sample.reasons or ())
            )
        ) or ("no_trusted_input",)
        if self.state != DARK:
            self.ewma = None
        self.good_since = None
        self.below_since = None
        self.above_since = None
        return self._assess(
            DARK, policy, None, now, wipe_in_progress,
            reasons or ("no_trusted_input",), samples, blind=True,
        )

    def _assess(
        self,
        state: str,
        policy: DewPolicy,
        margin: Optional[float],
        now: float,
        wipe_in_progress: bool,
        reasons: tuple[str, ...],
        samples: dict,
        blind: bool = False,
    ) -> Assessment:
        previous = self.state
        if previous != state or self.state_since is None:
            self.state_since = now
        self.state = state
        if blind:
            # 数据不可信导致的黑暗才清空观察计时；requalifying 期间保留计时。
            self.good_since = None
        if blind:
            confidence = "blind"
        elif any(sample.quality != GOOD for sample in samples.values()):
            confidence = "degraded"
        else:
            confidence = "good"
        result = Assessment(
            state=state,
            ts=now,
            policy_version=policy.version,
            confidence=confidence,
            margin_c=margin,
            ewma_margin_c=self.ewma,
            threshold_warn_c=policy.warn_margin_c,
            threshold_clear_c=policy.clear_margin_c,
            threshold_trip_c=policy.instant_trip_margin_c,
            state_since=self.state_since,
            wipe_in_progress=wipe_in_progress,
            reasons=reasons,
            inputs={kind: sample.as_dict() for kind, sample in samples.items()},
        )
        self.last = result
        return result
