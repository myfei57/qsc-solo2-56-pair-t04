"""凝露物理量与版本化策略。"""

from __future__ import annotations

import pytest

from psdline.dew.physics import condensation_margin_c, dew_point_c
from psdline.dew.policy import (
    DewPolicy,
    DewPolicySchedule,
    RainySeason,
    rainy_policy,
    standard_policy,
)
from psdline.errors import ConfigError


def test_dew_point_known_values():
    # 30 ℃ / 80% 露点约 26.2 ℃；20 ℃ / 50% 露点约 9.3 ℃。
    assert abs(dew_point_c(30.0, 80.0) - 26.17) < 0.1
    assert abs(dew_point_c(20.0, 50.0) - 9.27) < 0.1


def test_margin_sign_matches_condensation():
    # 玻璃 26 ℃、空气 30 ℃/80%（露点 26.2）：已达凝露条件。
    assert condensation_margin_c(26.0, 30.0, 80.0) < 0
    # 玻璃 30 ℃：安全。
    assert condensation_margin_c(30.0, 30.0, 80.0) > 0


def test_policy_hierarchy_validation():
    with pytest.raises(ConfigError):
        DewPolicy(version="bad", warn_margin_c=0.5, instant_trip_margin_c=1.0).validate()
    with pytest.raises(ConfigError):
        DewPolicy(version="bad", warn_margin_c=2.0, clear_margin_c=1.0).validate()
    with pytest.raises(ConfigError):
        DewPolicy(version="bad", instant_trip_margin_c=2.0).validate()


def test_season_schedule_picks_policy():
    schedule = DewPolicySchedule()
    assert schedule.for_date(7, 15).version == rainy_policy().version
    assert schedule.for_date(1, 15).version == standard_policy().version
    season = RainySeason((11, 1), (3, 31))
    assert season.contains(12, 10)
    assert season.contains(2, 1)
    assert not season.contains(5, 1)


def test_policy_roundtrip_dict():
    policy = rainy_policy()
    assert DewPolicy.from_dict(policy.as_dict()) == policy
