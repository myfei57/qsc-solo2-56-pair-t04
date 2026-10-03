"""凝露判据配置与季节画像。

判据是否随季节调整，这里的取舍是：**物理阈值固定，工程裕量与确认时长按季节
画像切换**。露点计算本身全年通用，不做季节修正；但雨季湿度长期顶格、结露
发展快，安全裕量留更大、确认时间更短，旱季则收紧灵敏度避免偶发高湿误摘门。

画像按日期生效（默认雨季 5-9 月），每条画像带版本号；每次风险判定都把当时
命中的画像版本与版本生效区间写进判定事实，重放时用链上画像而不是"当前画像"，
保证历史结论不可变、可复核。配置在装配期整体校验，非法值不允许带默认值开跑。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from ..errors import ConfigError

# 门体表面温度合理量程（℃）：玻璃表面不会超出这个区间，越界即传感器故障。
SURFACE_RANGE_C = (-40.0, 85.0)
# 门区空气温度合理量程（℃）。
AIR_RANGE_C = (-40.0, 70.0)
# 相对湿度合理量程（%）。
RH_RANGE = (0.0, 100.0)


@dataclass(frozen=True)
class CondensationProfile:
    """一版凝露判据画像。

    阈值全部以「凝露裕量 Td-Ts（℃）」表达，正值 = 已结露。
    """

    version: str
    # 裕量 >= high_margin_c 且持续 high_dwell_s：判高风险，摘门 + 擦拭单。
    high_margin_c: float
    # 高风险放行门槛：裕量必须回落到 release_margin_c 以下（迟滞，防抖）。
    release_margin_c: float
    # 裕量 >= watch_margin_c：关注态，不摘门但进审计、趋势加权。
    watch_margin_c: float
    # 高风险瞬时确认时长（秒）：持续越线才算，抑制单点毛刺。
    high_dwell_s: float
    # 放行持续时长（秒）：裕量回到安全区还要稳一段才放开，避免擦完又起雾。
    release_dwell_s: float
    # 趋势窗（秒）与恶化速率（℃/分钟）：裕量以快于该速度逼近露点时，
    # 高风险确认时长缩短到 high_dwell_s 的 fast_dwell_ratio。
    trend_window_s: float
    trend_rate_c_per_min: float
    fast_dwell_ratio: float
    # 每扇门最少需要几只在线表面传感器（其余按漂移/断线隔离）。
    min_surface_sensors: int
    # 冗余表面传感器两两差超过该值（℃）判漂移隔离。
    surface_divergence_c: float
    # 采样超过 stale_after_s 未刷新判断线。
    stale_after_s: float
    # 采样值变化率超 rate_limit_c_per_s 判尖峰毛刺，丢弃该点。
    rate_limit_c_per_s: float
    # 连续 stuck_limit 个采样值不变判卡值（传感器死掉但链路还通）。
    stuck_limit: int
    # 雨季起止月（含），用于默认画像选择。
    season_months: tuple[int, int] = (5, 9)

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "high_margin_c": self.high_margin_c,
            "release_margin_c": self.release_margin_c,
            "watch_margin_c": self.watch_margin_c,
            "high_dwell_s": self.high_dwell_s,
            "release_dwell_s": self.release_dwell_s,
            "trend_window_s": self.trend_window_s,
            "trend_rate_c_per_min": self.trend_rate_c_per_min,
            "fast_dwell_ratio": self.fast_dwell_ratio,
            "min_surface_sensors": self.min_surface_sensors,
            "surface_divergence_c": self.surface_divergence_c,
            "stale_after_s": self.stale_after_s,
            "rate_limit_c_per_s": self.rate_limit_c_per_s,
            "stuck_limit": self.stuck_limit,
            "season_months": list(self.season_months),
        }


PROFILE_RAINY = CondensationProfile(
    version="rainy-v1",
    high_margin_c=0.5,
    release_margin_c=-2.0,
    watch_margin_c=-1.0,
    high_dwell_s=30.0,
    release_dwell_s=120.0,
    trend_window_s=120.0,
    trend_rate_c_per_min=0.8,
    fast_dwell_ratio=0.5,
    min_surface_sensors=1,
    surface_divergence_c=2.0,
    stale_after_s=8.0,
    rate_limit_c_per_s=2.0,
    stuck_limit=360,
)

PROFILE_NORMAL = CondensationProfile(
    version="normal-v1",
    high_margin_c=0.5,
    release_margin_c=-2.0,
    watch_margin_c=-1.0,
    high_dwell_s=60.0,
    release_dwell_s=180.0,
    trend_window_s=300.0,
    trend_rate_c_per_min=0.5,
    fast_dwell_ratio=0.5,
    min_surface_sensors=1,
    surface_divergence_c=2.0,
    stale_after_s=8.0,
    rate_limit_c_per_s=2.0,
    stuck_limit=360,
)


@dataclass(frozen=True)
class CondensationConfig:
    """凝露子系统配置。"""

    # 采集周期（秒）；控制器按该节拍采一次全部门。
    sample_period_s: float = 5.0
    # 风险窗保留时长（秒）：引擎只保留这段时间的有效样本做趋势。
    window_s: float = 600.0
    # 哈希链每段条数。
    chain_segment: int = 1024
    # 启动时是否校验哈希链（发现篡改直接拒启动）。
    verify_on_start: bool = True
    rainy: CondensationProfile = field(default_factory=lambda: PROFILE_RAINY)
    normal: CondensationProfile = field(default_factory=lambda: PROFILE_NORMAL)
    # 画像覆盖表：(起月, 止月, 画像)，默认按雨季/旱季两段。
    # 允许现场用配置追加版本，画像对象自身不可变。
    season_overrides: tuple[CondensationProfile, ...] = ()

    def profile(self, at: float | None = None) -> CondensationProfile:
        """按时间返回生效画像；覆盖表优先，之后回落到雨季/默认两段。"""

        return profile_for_time(
            at if at is not None else time.time(),
            rainy=self.rainy,
            normal=self.normal,
            overrides=self.season_overrides,
        )

    def validate(self) -> "CondensationConfig":
        if self.sample_period_s <= 0:
            raise ConfigError("凝露采集周期必须为正", component="condensation")
        if self.window_s <= 0:
            raise ConfigError("凝露风险窗时长必须为正", component="condensation")
        if self.chain_segment <= 0:
            raise ConfigError("哈希链分段长度必须为正", component="condensation")
        for profile in (self.rainy, self.normal, *self.season_overrides):
            _validate_profile(profile)
        if self.window_s < self.rainy.trend_window_s:
            raise ConfigError("风险窗时长不能短于雨季趋势窗", component="condensation")
        return self


def _validate_profile(profile: CondensationProfile) -> None:
    if not profile.version:
        raise ConfigError("画像版本不能为空", component="condensation")
    if not profile.release_margin_c < profile.watch_margin_c <= 0.0:
        raise ConfigError(
            f"画像 {profile.version} 阈值顺序应为 release < watch <= 0",
            component="condensation",
        )
    if not profile.watch_margin_c < profile.high_margin_c:
        raise ConfigError(
            f"画像 {profile.version} 的高风险阈值应高于关注阈值",
            component="condensation",
        )
    if profile.high_dwell_s <= 0 or profile.release_dwell_s <= 0:
        raise ConfigError(f"画像 {profile.version} 确认时长必须为正", component="condensation")
    if not 0 < profile.fast_dwell_ratio <= 1:
        raise ConfigError(
            f"画像 {profile.version} 的快速确认比例应落在 (0,1]",
            component="condensation",
        )
    if profile.trend_window_s <= 0:
        raise ConfigError(f"画像 {profile.version} 趋势窗必须为正", component="condensation")
    if profile.trend_rate_c_per_min <= 0:
        raise ConfigError(f"画像 {profile.version} 趋势速率必须为正", component="condensation")
    if profile.min_surface_sensors < 1:
        raise ConfigError(f"画像 {profile.version} 至少需要一只表面传感器", component="condensation")
    if profile.surface_divergence_c <= 0:
        raise ConfigError(f"画像 {profile.version} 漂移阈值必须为正", component="condensation")
    if profile.stale_after_s <= 0:
        raise ConfigError(f"画像 {profile.version} 断线时长必须为正", component="condensation")
    if profile.rate_limit_c_per_s <= 0:
        raise ConfigError(f"画像 {profile.version} 变化率限值必须为正", component="condensation")
    if profile.stuck_limit < 2:
        raise ConfigError(f"画像 {profile.version} 卡值计数至少为 2", component="condensation")
    start, end = profile.season_months
    if not 1 <= start <= 12 or not 1 <= end <= 12:
        raise ConfigError(f"画像 {profile.version} 季节月份必须落在 1..12", component="condensation")


def _in_season(month: int, start: int, end: int) -> bool:
    """判断月份是否在含起止的季节区间内，支持跨年（如 11..2）。"""

    if start <= end:
        return start <= month <= end
    return month >= start or month <= end


def profile_for_time(
    at: float,
    *,
    rainy: CondensationProfile = PROFILE_RAINY,
    normal: CondensationProfile = PROFILE_NORMAL,
    overrides: tuple[CondensationProfile, ...] = (),
) -> CondensationProfile:
    """按 Unix 秒选画像：覆盖表优先（按声明顺序），否则雨季/旱季两段。"""

    month = time.gmtime(at).tm_mon
    for profile in overrides:
        start, end = profile.season_months
        if _in_season(month, start, end):
            return profile
    if _in_season(month, *rainy.season_months):
        return rainy
    return normal


def condensation_from_mapping(payload: object) -> CondensationConfig:
    """从配置 JSON 的 ``condensation`` 小节构造配置，缺省键回落默认值。"""

    if payload is None:
        return CondensationConfig().validate()
    if not isinstance(payload, dict):
        raise ConfigError("condensation 配置必须是 JSON 对象", component="condensation")

    def _profile(key: str, fallback: CondensationProfile) -> CondensationProfile:
        section = payload.get(key)
        if section is None:
            return fallback
        if not isinstance(section, dict):
            raise ConfigError(f"{key} 画像必须是 JSON 对象", component="condensation")
        known = set(CondensationProfile.__dataclass_fields__)
        kwargs: dict = {}
        for name, value in section.items():
            if name not in known:
                continue
            kwargs[name] = tuple(value) if name == "season_months" else value
        if "season_months" in kwargs:
            months = kwargs["season_months"]
            kwargs["season_months"] = (int(months[0]), int(months[1]))
        return replace(fallback, **kwargs)

    overrides: list[CondensationProfile] = []
    for section in payload.get("season_overrides", ()):
        if not isinstance(section, dict) or "version" not in section:
            raise ConfigError("季节画像覆盖必须带 version", component="condensation")
        base = _profile("__override__", PROFILE_NORMAL)
        known = set(CondensationProfile.__dataclass_fields__)
        kwargs = {
            name: (tuple(value) if name == "season_months" else value)
            for name, value in section.items()
            if name in known
        }
        overrides.append(replace(base, **kwargs))

    known_top = {"sample_period_s", "window_s", "chain_segment", "verify_on_start"}
    top = {key: value for key, value in payload.items() if key in known_top}
    config = CondensationConfig(
        rainy=_profile("rainy", PROFILE_RAINY),
        normal=_profile("normal", PROFILE_NORMAL),
        season_overrides=tuple(overrides),
        **top,
    )
    return config.validate()


def profile_from_dict(payload: dict) -> CondensationProfile:
    """从链上保存的画像字典无损重建画像（重放用）。"""

    known = set(CondensationProfile.__dataclass_fields__)
    kwargs: dict = {}
    for name in known:
        if name in payload:
            value = payload[name]
            if name == "season_months":
                value = (int(value[0]), int(value[1]))
            kwargs[name] = value
    return CondensationProfile(**kwargs)


def condensation_to_mapping(config: CondensationConfig) -> dict:
    """把凝露配置转成可落盘字典。"""

    return {
        "sample_period_s": config.sample_period_s,
        "window_s": config.window_s,
        "chain_segment": config.chain_segment,
        "verify_on_start": config.verify_on_start,
        "rainy": config.rainy.as_dict(),
        "normal": config.normal.as_dict(),
        "season_overrides": [profile.as_dict() for profile in config.season_overrides],
    }
