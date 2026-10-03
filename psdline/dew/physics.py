"""凝露物理量计算。

判据建立在两个物理量上：

* 露点温度 ``Td``——给定气温与相对湿度后，空气达到饱和结露的温度；
* 凝露余量 ``margin``——门体玻璃温度减去露点温度，``margin <= 0`` 即表面已经
  达到凝露条件。

物理判据（margin 零线）是自然常数级别的，不随季节调整；随季节调整的只有策略里
的预警裕度与确认时长。露点用 Magnus 公式（WMO 推荐的 Tetens 参数），在车站
环境温域（-20~50 ℃）内误差远小于传感器误差，足够用于门区判定。
"""

from __future__ import annotations

import math

# Tetens 参数（水面，0 ℃ 以上）。
_MAGNUS_A = 17.625
_MAGNUS_B = 243.12


def dew_point_c(air_c: float, rh_percent: float) -> float:
    """按气温（℃）与相对湿度（%）计算露点温度（℃）。

    湿度被夹到 1..100：现场偶尔会报出 0 或越界值，把它们映射到合法区间而不是
    抛异常，越界本身由传感器层标记为可疑。
    """

    rh = min(100.0, max(1.0, float(rh_percent)))
    alpha = (_MAGNUS_A * air_c) / (_MAGNUS_B + air_c) + math.log(rh / 100.0)
    return _MAGNUS_B * alpha / (_MAGNUS_A - alpha)


def condensation_margin_c(glass_c: float, air_c: float, rh_percent: float) -> float:
    """返回凝露余量：门体温度 - 露点温度（℃），非正即已满足凝露条件。"""

    return glass_c - dew_point_c(air_c, rh_percent)
