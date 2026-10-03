"""凝露物理量。

判据建立在两个物理量上，不依赖季节：

露点温度 ``Td``：空气在当前含湿量下降温到相对湿度 100% 的温度，用 Magnus
公式由气温与相对湿度反算；
凝露裕量 ``margin = Td - Ts``：露点高出门体表面温度多少。裕量为正意味着
门体已经冷到露点以下，水汽会在玻璃上凝结；裕量为负且差距足够大才安全。

季节只改变"安全裕量取多少、确认多久"（见 :mod:`config`），不改物理量本身，
这样雨季/旱季用的同一把尺子，事后重放时结论不会随换季节而变。
"""

from __future__ import annotations

import math

# Magnus 公式在水面上的经验常数（气温 -45..60 ℃ 适用，门区环境完全覆盖）。
MAGNUS_A = 17.625
MAGNUS_B = 243.12

CRITERION_VERSION = "dewpoint-magnus-v1"


def saturation_gamma(temp_c: float, rh_percent: float) -> float:
    """返回 Magnus 公式里的 γ(T, RH)。

    ``rh_percent`` 按 0..100 传入，内部归一化；越界值由采集层拦，这里只做
    容错裁剪，避免单只传感器异常把整条计算链打挂。
    """

    rh = min(100.0, max(0.0, float(rh_percent))) / 100.0
    return math.log(max(rh, 1e-9)) + MAGNUS_A * temp_c / (MAGNUS_B + temp_c)


def dew_point_c(air_temp_c: float, rh_percent: float) -> float:
    """由气温（℃）与相对湿度（%）计算露点温度（℃）。"""

    gamma = saturation_gamma(air_temp_c, rh_percent)
    return MAGNUS_B * gamma / (MAGNUS_A - gamma)


def margin_c(air_temp_c: float, rh_percent: float, surface_temp_c: float) -> float:
    """返回凝露裕量 ``Td - Ts``（℃），越大越危险。"""

    return dew_point_c(air_temp_c, rh_percent) - float(surface_temp_c)


def linear_slope_c_per_s(values: list[tuple[float, float]]) -> float:
    """对一段 ``(ts, value)`` 做最小二乘，返回每秒变化量。

    点数不足或时间戳完全重合时返回 0（视为"没有趋势"而不是报错），趋势
    恶化加速确认只是灵敏度调节，不能因为采样抖动本身制造风险。
    """

    points = [(float(ts), float(v)) for ts, v in values]
    n = len(points)
    if n < 2:
        return 0.0
    mean_t = sum(ts for ts, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    denom = sum((ts - mean_t) ** 2 for ts, _ in points)
    if denom <= 1e-12:
        return 0.0
    numer = sum((ts - mean_t) * (v - mean_v) for ts, v in points)
    return numer / denom
