"""凝露物理量与季节画像的单元测试。"""

from __future__ import annotations

import time

from psdline.condensation.config import (
    CondensationConfig,
    PROFILE_NORMAL,
    PROFILE_RAINY,
    condensation_from_mapping,
    profile_for_time,
)
from psdline.condensation.physics import dew_point_c, linear_slope_c_per_s, margin_c
from psdline.errors import ConfigError


def test_dew_point_known_values():
    # 25℃ / RH 50% 露点约 13.9℃；30℃ / RH 80% 露点约 26.2℃
    assert abs(dew_point_c(25.0, 50.0) - 13.87) < 0.1
    assert abs(dew_point_c(30.0, 80.0) - 26.17) < 0.1


def test_dew_point_rh100_equals_air_temp():
    assert abs(dew_point_c(22.0, 100.0) - 22.0) < 1e-9


def test_margin_sign_matches_condensation_condition():
    # 表面低于露点 → 正裕量（会结露）；表面高于露点 → 负裕量（安全）
    tdp = dew_point_c(28.0, 90.0)
    assert margin_c(28.0, 90.0, tdp - 2.0) > 1.9
    assert margin_c(28.0, 90.0, tdp + 5.0) < -4.9


def test_rh_out_of_hundred_clamped_not_crashing():
    # 容错裁剪：异常湿度不应把计算链打挂
    assert dew_point_c(25.0, 200.0) >= dew_point_c(25.0, 100.0)
    assert dew_point_c(25.0, -10.0) < dew_point_c(25.0, 10.0)


def test_linear_slope_basic():
    values = [(0.0, 10.0), (10.0, 12.0), (20.0, 14.0)]
    assert abs(linear_slope_c_per_s(values) - 0.2) < 1e-9
    assert linear_slope_c_per_s([(0.0, 1.0)]) == 0.0
    assert linear_slope_c_per_s([(5.0, 1.0), (5.0, 2.0)]) == 0.0


def test_profile_selection_rainy_vs_dry_season():
    import calendar

    rainy_ts = calendar.timegm((2026, 7, 15, 12, 0, 0, 0, 0, 0))
    dry_ts = calendar.timegm((2026, 12, 15, 12, 0, 0, 0, 0, 0))
    assert profile_for_time(rainy_ts).version == PROFILE_RAINY.version
    assert profile_for_time(dry_ts).version == PROFILE_NORMAL.version


def test_rainy_profile_more_sensitive_than_normal():
    # 雨季确认更快、趋势窗更短
    assert PROFILE_RAINY.high_dwell_s < PROFILE_NORMAL.high_dwell_s
    assert PROFILE_RAINY.trend_window_s < PROFILE_NORMAL.trend_window_s


def test_config_validation_rejects_bad_thresholds():
    import dataclasses

    bad = dataclasses.replace(PROFILE_RAINY, release_margin_c=1.0)
    config = CondensationConfig(rainy=bad)
    try:
        config.validate()
        raise AssertionError("非法阈值必须在装配期被拦下")
    except ConfigError:
        pass


def test_config_round_trip_mapping():
    config = condensation_from_mapping(
        {"sample_period_s": 2.0, "rainy": {"high_dwell_s": 10.0}}
    )
    assert config.sample_period_s == 2.0
    assert config.rainy.high_dwell_s == 10.0
    # 未覆盖字段保留默认
    assert config.rainy.version == PROFILE_RAINY.version
    assert config.normal.high_dwell_s == PROFILE_NORMAL.high_dwell_s


def test_default_config_is_valid():
    CondensationConfig().validate()
