"""版本化、可按季节切换的凝露策略。

两个取舍在这里落地：

1. 判据分两层——物理凝露线（``instant_trip_margin``，余量瞬时跌破立即摘门，
   默认 0.0 ℃）与运行预警线（``warn_margin``）。物理线固定，不允许季节策略把
   它放松到零线以上；运行线可以随季节收紧。
2. 稳态判定看趋势（指数平滑余量 + 持续确认时长），防止一次抖动就摘门、也防止
   一次抖动就放开；恢复比摘出更保守（``clear_margin`` 更高、确认更久），形成
   迟滞。

策略是冻结的数据类，每次切换都生成新版本并写进哈希链；重放时按当时生效的策略
版本重建，事后任何参数调整都改不动历史结论。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..errors import ConfigError

# 物理凝露线允许的取值区间：只允许比 0 ℃ 更保守（更早上报）。
TRIP_FLOOR_C = -2.0
TRIP_CEIL_C = 0.5


@dataclass(frozen=True)
class DewPolicy:
    """一版凝露判定参数。"""

    version: str
    label: str = "custom"
    # 余量瞬时跌破该值立即摘门（物理线），℃。
    instant_trip_margin_c: float = 0.0
    # 平滑余量跌破该值并持续确认后摘门（运行预警线），℃。
    warn_margin_c: float = 2.0
    # 平滑余量回升到该值以上并持续确认后放开，℃（必须 > 预警线，形成迟滞）。
    clear_margin_c: float = 4.0
    # 摘门确认时长（趋势持续低于预警线多久）。
    trip_dwell_s: float = 90.0
    # 放开确认时长（趋势持续高于解除线、且最近无擦拭）。
    clear_dwell_s: float = 300.0
    # 指数平滑系数 alpha，越大越跟瞬时值，越小越平滑。
    ewma_alpha: float = 0.2
    # 数据不可信恢复后，需要连续健康多久才允许重新参与判定。
    requalify_s: float = 120.0
    # 一次擦拭完成后，若仍有风险，再次安排擦拭的冷却时间。
    rewipe_cooldown_s: float = 600.0
    # 采样断线超过该时长判为数据黑暗（fail-safe 摘门）。
    stale_after_s: float = 90.0

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "DewPolicy":
        known = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in payload.items() if key in known})

    def validate(self) -> "DewPolicy":
        """装配期校验：层次关系不能反，物理线不能放松。"""

        if not self.version:
            raise ConfigError("凝露策略版本为空", component="dew")
        if not TRIP_FLOOR_C <= self.instant_trip_margin_c <= TRIP_CEIL_C:
            raise ConfigError(
                f"物理凝露线必须落在 [{TRIP_FLOOR_C}, {TRIP_CEIL_C}] ℃",
                component="dew",
            )
        if self.instant_trip_margin_c > self.warn_margin_c:
            raise ConfigError("预警线不得低于物理凝露线", component="dew")
        if self.clear_margin_c <= self.warn_margin_c:
            raise ConfigError("解除线必须高于预警线（需要迟滞）", component="dew")
        if self.trip_dwell_s <= 0 or self.clear_dwell_s <= 0:
            raise ConfigError("确认时长必须为正", component="dew")
        if not 0.0 < self.ewma_alpha <= 1.0:
            raise ConfigError("平滑系数必须落在 (0, 1]", component="dew")
        if self.requalify_s <= 0 or self.stale_after_s <= 0:
            raise ConfigError("恢复确认与断线时长必须为正", component="dew")
        if self.rewipe_cooldown_s < 0:
            raise ConfigError("再擦拭冷却不能为负", component="dew")
        return self


@dataclass(frozen=True)
class RainySeason:
    """雨季窗口（按当地月/日，北半球默认 6-9 月）。"""

    start_md: tuple[int, int] = (6, 1)
    end_md: tuple[int, int] = (9, 30)

    def contains(self, month: int, day: int) -> bool:
        """判断月/日是否落在雨季窗口内（支持跨年窗口）。"""

        cur = month * 100 + day
        start = self.start_md[0] * 100 + self.start_md[1]
        end = self.end_md[0] * 100 + self.end_md[1]
        if start <= end:
            return start <= cur <= end
        return cur >= start or cur <= end

    def as_dict(self) -> dict:
        return {"start_md": list(self.start_md), "end_md": list(self.end_md)}

    @classmethod
    def from_dict(cls, payload: dict | None) -> "RainySeason":
        if not isinstance(payload, dict):
            return cls()
        start = tuple(payload.get("start_md", (6, 1)))
        end = tuple(payload.get("end_md", (9, 30)))
        season = cls(start_md=(int(start[0]), int(start[1])), end_md=(int(end[0]), int(end[1])))
        if not (1 <= season.start_md[0] <= 12 and 1 <= season.end_md[0] <= 12):
            raise ConfigError("雨季月份必须落在 1..12", component="dew")
        return season


def standard_policy() -> DewPolicy:
    """旱季/常规策略：裕度小、确认长。"""

    return DewPolicy(
        version="std-1",
        label="常规季节",
        warn_margin_c=1.5,
        clear_margin_c=3.5,
        trip_dwell_s=120.0,
        clear_dwell_s=300.0,
    ).validate()


def rainy_policy() -> DewPolicy:
    """雨季策略：提前预警、快速摘门、放开更慢。"""

    return DewPolicy(
        version="rainy-1",
        label="雨季",
        warn_margin_c=2.5,
        clear_margin_c=5.0,
        trip_dwell_s=60.0,
        clear_dwell_s=420.0,
    ).validate()


@dataclass(frozen=True)
class DewPolicySchedule:
    """按日期选择生效策略；非雨季用常规策略。"""

    standard: DewPolicy = field(default_factory=standard_policy)
    rainy: DewPolicy = field(default_factory=rainy_policy)
    season: RainySeason = field(default_factory=RainySeason)

    def for_date(self, month: int, day: int) -> DewPolicy:
        return self.rainy if self.season.contains(month, day) else self.standard

    def as_dict(self) -> dict:
        return {
            "standard": self.standard.as_dict(),
            "rainy": self.rainy.as_dict(),
            "season": self.season.as_dict(),
        }

    @classmethod
    def from_dict(cls, payload: dict | None) -> "DewPolicySchedule":
        if not isinstance(payload, dict):
            return cls()
        return cls(
            standard=DewPolicy.from_dict(payload.get("standard", {})) if payload.get("standard") else standard_policy(),
            rainy=DewPolicy.from_dict(payload.get("rainy", {})) if payload.get("rainy") else rainy_policy(),
            season=RainySeason.from_dict(payload.get("season")),
        )
