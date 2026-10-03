"""凝露风险引擎（每樘门一个）。

判据取舍：**瞬时值与趋势一起用，以瞬时裕量为主、趋势只调灵敏度**。只看瞬时
值，雨季裕量贴着阈值抖动会来回摘门/放门；只看趋势，已经结露的门还要等一个
完整趋势窗，司机早看不清了。因此：

* 裕量 ``Td-Ts`` 越过 ``high_margin_c`` 必须**持续** ``high_dwell_s`` 才判
  高风险，窗内任意一个有效采样跌回线下，持续计时清零（抗单点毛刺）；
* 趋势窗内裕量以快于 ``trend_rate_c_per_min`` 的速度逼近露点（正在恶化）时，
  确认时长缩短到 ``high_dwell_s * fast_dwell_ratio``，发展快的雾情提前拦住；
* 高风险放行用独立、更靠安全侧的 ``release_margin_c`` 迟滞阈值，并且要在
  安全区稳满 ``release_dwell_s`` 才退出，擦完立刻又起雾不会把门抖出去；
* 输入 ``UNKNOWN``（断线/漂移导致不可信）期间输出 ``UNKNOWN``，已摘的门保持
  摘除——没有可信数据时不做"自动变好"的推断。

引擎是纯状态机：只吃 :class:`~psdline.condensation.sensors.DoorEnvInput` 和
当时生效的画像，不碰门控、工单与审计，方便重放时逐拍重建。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import CondensationProfile
from .physics import CRITERION_VERSION, linear_slope_c_per_s
from .sensors import INPUT_OK, INPUT_UNKNOWN, DoorEnvInput

RISK_NORMAL = "normal"
RISK_WATCH = "watch"
RISK_HIGH = "high"
RISK_UNKNOWN = "unknown"

ENGINE_VERSION = "risk-engine-v1"


@dataclass(frozen=True)
class RiskFact:
    """一次风险判定的完整事实，摘门/擦拭/审计全部基于它。"""

    psd_id: str
    ts: float
    level: str
    input_status: str
    margin_c: float | None
    air_t: float | None = None
    rh: float | None = None
    surface_t: float | None = None
    dew_point_c: float | None = None
    slope_c_per_min: float = 0.0
    worsening: bool = False
    dwell_required_s: float = 0.0
    high_for_s: float = 0.0
    clear_for_s: float = 0.0
    high_since: float | None = None
    reasons: list[str] = field(default_factory=list)
    profile_version: str = ""
    profile: dict = field(default_factory=dict)
    criterion_version: str = CRITERION_VERSION
    engine_version: str = ENGINE_VERSION
    changed: bool = False
    previous_level: str = ""

    def as_dict(self) -> dict:
        return {
            "psd_id": self.psd_id,
            "ts": round(self.ts, 3),
            "level": self.level,
            "input_status": self.input_status,
            "margin_c": _round(self.margin_c),
            "air_t": _round(self.air_t),
            "rh": _round(self.rh),
            "surface_t": _round(self.surface_t),
            "dew_point_c": _round(self.dew_point_c),
            "slope_c_per_min": round(self.slope_c_per_min, 5),
            "worsening": self.worsening,
            "dwell_required_s": self.dwell_required_s,
            "high_for_s": round(self.high_for_s, 3),
            "clear_for_s": round(self.clear_for_s, 3),
            "high_since": self.high_since,
            "reasons": list(self.reasons),
            "profile_version": self.profile_version,
            "profile": dict(self.profile),
            "criterion_version": self.criterion_version,
            "engine_version": self.engine_version,
            "changed": self.changed,
            "previous_level": self.previous_level,
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


class DoorEnvEngine:
    """单门风险状态机。"""

    def __init__(self, psd_id: str, window_s: float) -> None:
        self.psd_id = psd_id
        self._window_s = max(1.0, window_s)
        self._samples: list[tuple[float, float]] = []
        self._level = RISK_NORMAL
        self._previous_level = RISK_NORMAL
        self._high_since: float | None = None
        self._clear_since: float | None = None
        self._last_fact: RiskFact | None = None

    @property
    def level(self) -> str:
        return self._level

    @property
    def last_fact(self) -> RiskFact | None:
        return self._last_fact

    def evaluate(self, env_input: DoorEnvInput, profile: CondensationProfile) -> RiskFact:
        """推进一个采集节拍，返回本拍判定事实。"""

        previous = self._level
        if not env_input.usable:
            # 不可信输入：风险计时全部冻结/清零，输出 UNKNOWN（fail-safe）。
            self._high_since = None
            self._clear_since = None
            self._level = RISK_UNKNOWN
            fact = RiskFact(
                psd_id=self.psd_id,
                ts=env_input.ts,
                level=RISK_UNKNOWN,
                input_status=INPUT_UNKNOWN,
                margin_c=None,
                reasons=list(env_input.reasons) or ["环境输入不可用"],
                profile_version=profile.version,
                profile=profile.as_dict(),
                changed=previous != RISK_UNKNOWN,
                previous_level=previous,
            )
            self._previous_level = previous
            self._last_fact = fact
            return fact

        ts = env_input.ts
        margin = float(env_input.margin_c)
        self._samples.append((ts, margin))
        cutoff = ts - max(self._window_s, profile.trend_window_s)
        self._samples = [(stamp, value) for stamp, value in self._samples if stamp >= cutoff]

        slope_per_s = linear_slope_c_per_s(
            [(stamp, value) for stamp, value in self._samples if stamp >= ts - profile.trend_window_s]
        )
        slope_per_min = slope_per_s * 60.0
        worsening = slope_per_min >= profile.trend_rate_c_per_min
        dwell_required = (
            profile.high_dwell_s * profile.fast_dwell_ratio
            if worsening
            else profile.high_dwell_s
        )

        level, reasons = self._advance(profile, ts, margin, worsening, slope_per_min, dwell_required)
        high_for = (ts - self._high_since) if self._high_since is not None else 0.0
        clear_for = (ts - self._clear_since) if self._clear_since is not None else 0.0

        if margin >= profile.high_margin_c:
            reasons.append(
                f"凝露裕量 {margin:+.2f}℃ 达高风险线 {profile.high_margin_c:+.2f}℃"
            )
        elif margin >= profile.watch_margin_c:
            reasons.append(f"凝露裕量 {margin:+.2f}℃ 进入关注区（{profile.watch_margin_c:+.2f}℃）")
        if worsening:
            reasons.append(
                f"裕量以 {slope_per_min:.2f}℃/min 恶化（快于 {profile.trend_rate_c_per_min}℃/min）"
            )

        fact = RiskFact(
            psd_id=self.psd_id,
            ts=ts,
            level=level,
            input_status=INPUT_OK,
            margin_c=margin,
            air_t=env_input.air_t,
            rh=env_input.rh,
            surface_t=env_input.surface_t,
            dew_point_c=env_input.dew_point_c,
            slope_c_per_min=slope_per_min,
            worsening=worsening,
            dwell_required_s=dwell_required,
            high_for_s=max(0.0, high_for),
            clear_for_s=max(0.0, clear_for),
            high_since=self._high_since,
            reasons=reasons,
            profile_version=profile.version,
            profile=profile.as_dict(),
            changed=previous != level,
            previous_level=previous,
        )
        self._level = level
        self._previous_level = previous
        self._last_fact = fact
        return fact

    def _advance(
        self,
        profile: CondensationProfile,
        ts: float,
        margin: float,
        worsening: bool,
        slope_per_min: float,
        dwell_required: float,
    ) -> tuple[str, list[str]]:
        reasons: list[str] = []

        # 已在高风险：迟滞放行，必须回到 release 线以下并稳满 release_dwell。
        if self._level == RISK_HIGH:
            if margin >= profile.release_margin_c:
                self._clear_since = None
                if self._high_since is None:
                    self._high_since = ts
                return RISK_HIGH, reasons
            if self._clear_since is None:
                self._clear_since = ts
            if ts - self._clear_since >= profile.release_dwell_s:
                self._high_since = None
                self._clear_since = None
                if margin >= profile.watch_margin_c:
                    return RISK_WATCH, ["裕量回到安全区并持续稳定，退出高风险（仍关注）"]
                return RISK_NORMAL, ["裕量回到安全区并持续稳定，解除凝露风险"]
            remain = profile.release_dwell_s - (ts - self._clear_since)
            reasons.append(f"裕量回落但还需稳定 {remain:.0f}s 才放行")
            return RISK_HIGH, reasons

        # 非高风险：越线持续确认，窗内任一点跌回线下则重新计时。
        if margin >= profile.high_margin_c:
            if self._high_since is None:
                self._high_since = ts
            if ts - self._high_since >= dwell_required:
                self._clear_since = None
                trigger = "趋势恶化加速确认" if worsening else "瞬时持续确认"
                reasons.append(
                    f"高风险持续 {ts - self._high_since:.0f}s ≥ {dwell_required:.0f}s（{trigger}）"
                )
                return RISK_HIGH, reasons
            remain = dwell_required - (ts - self._high_since)
            reasons.append(f"高风险确认中，还需 {remain:.0f}s 持续越线")
            return RISK_WATCH, reasons

        # 跌回线下：高风险计时清零（严格抗抖）。
        self._high_since = None
        if margin >= profile.watch_margin_c:
            return RISK_WATCH, reasons
        return RISK_NORMAL, reasons

    def state(self) -> dict:
        """返回引擎内部状态（状态接口/重启重建用）。"""

        return {
            "psd_id": self.psd_id,
            "level": self._level,
            "high_since": self._high_since,
            "clear_since": self._clear_since,
            "samples": len(self._samples),
            "last": self._last_fact.as_dict() if self._last_fact else None,
        }
