"""判定状态机：趋势确认、瞬时触发、迟滞放开、数据黑暗与恢复。"""

from __future__ import annotations

from psdline.dew.assess import DARK, OK, RISK, RiskAssessor
from psdline.dew.policy import DewPolicy, standard_policy
from psdline.dew.sensors import FusedSample, GOOD, SUSPECT


def _sample(kind: str, value: float, quality: str = GOOD, ts: float = 0.0):
    return FusedSample(kind=kind, value=value, quality=quality, ts=ts)


def _step(assessor, policy, air, rh, glass, now, wipe=False):
    return assessor.process(
        policy,
        _sample("air_c", air, ts=now),
        _sample("rh_pct", rh, ts=now),
        _sample("glass_c", glass, ts=now),
        now,
        wipe_in_progress=wipe,
    )


def _policy(**overrides):
    base = standard_policy()
    return DewPolicy(**{**base.as_dict(), **overrides})


def test_starts_dark_then_ok_after_requalify():
    assessor = RiskAssessor("z")
    policy = _policy(requalify_s=120.0)
    verdict = _step(assessor, policy, 28.0, 70.0, 27.0, 0.0)
    assert verdict.state == DARK
    verdict = _step(assessor, policy, 28.0, 70.0, 27.0, 130.0)
    assert verdict.state == OK


def test_trend_trip_needs_dwell_then_clears_with_hysteresis():
    assessor = RiskAssessor("z")
    # 雨季式参数：确认 60s，解除 120s，预警 2.5，解除 5，alpha 取大让趋势可观察。
    policy = _policy(
        warn_margin_c=2.5, clear_margin_c=5.0, trip_dwell_s=60.0,
        clear_dwell_s=120.0, requalify_s=10.0, ewma_alpha=0.5,
    )
    _step(assessor, policy, 28.0, 70.0, 27.0, 0.0)      # DARK 观察期
    _step(assessor, policy, 28.0, 70.0, 27.0, 20.0)      # OK
    # 30 ℃/90% 露点约 28.3；玻璃 28.5 -> 余量约 0.2，平滑后逐步下穿预警线。
    v = _step(assessor, policy, 30.0, 90.0, 28.5, 30.0)
    assert v.state == OK
    v = _step(assessor, policy, 30.0, 90.0, 28.5, 90.0)
    assert v.state == OK and "watching" in v.reasons
    v = _step(assessor, policy, 30.0, 90.0, 28.5, 150.0)
    assert v.state == RISK and "below_warn_trend" in v.reasons
    # 玻璃烘回 35 ℃：余量高过解除线，但必须持续 clear_dwell（中间拍按 60s 间隔）。
    for ts in (210.0, 270.0):
        v = _step(assessor, policy, 30.0, 90.0, 35.0, ts)
    assert v.state == RISK and "clearing" in v.reasons
    for ts in (330.0, 400.0):
        v = _step(assessor, policy, 30.0, 90.0, 35.0, ts)
    assert v.state == OK


def test_instant_trip_does_not_wait_for_trend():
    assessor = RiskAssessor("z")
    policy = _policy(requalify_s=10.0, trip_dwell_s=600.0)
    _step(assessor, policy, 28.0, 70.0, 27.0, 0.0)
    _step(assessor, policy, 28.0, 70.0, 27.0, 20.0)
    # 玻璃直接跌破露点：当拍即 RISK，即使趋势确认要 600s。
    v = _step(assessor, policy, 30.0, 95.0, 29.0, 30.0)
    assert v.state == RISK
    assert "instant_trip" in v.reasons
    assert v.margin_c < 0


def test_single_sample_jitter_does_not_release():
    """一次抖动的好读数不能越过解除确认。"""

    assessor = RiskAssessor("z")
    policy = _policy(requalify_s=10.0, trip_dwell_s=60.0, clear_dwell_s=300.0)
    _step(assessor, policy, 28.0, 70.0, 27.0, 0.0)
    _step(assessor, policy, 28.0, 70.0, 27.0, 20.0)
    _step(assessor, policy, 30.0, 95.0, 28.0, 30.0)  # 瞬时进 RISK
    _step(assessor, policy, 30.0, 95.0, 40.0, 90.0)  # 单拍"变好"
    v = _step(assessor, policy, 30.0, 95.0, 28.0, 100.0)  # 再次起雾
    assert v.state == RISK


def test_missing_input_holds_dark_and_no_condensation_claim():
    assessor = RiskAssessor("z")
    policy = _policy(requalify_s=10.0)
    _step(assessor, policy, 28.0, 70.0, 27.0, 0.0)
    _step(assessor, policy, 28.0, 70.0, 27.0, 20.0)
    blind = FusedSample("rh_pct", None, "bad", 30.0, ("no_data",), ())
    v = assessor.process(
        policy, _sample("air_c", 28.0, ts=30.0), blind,
        _sample("glass_c", 27.0, ts=30.0), 30.0,
    )
    assert v.state == DARK
    assert v.margin_c is None  # 不下"已凝露"结论
    assert v.confidence == "blind"


def test_wipe_in_progress_blocks_release():
    assessor = RiskAssessor("z")
    policy = _policy(requalify_s=10.0, trip_dwell_s=30.0, clear_dwell_s=60.0)
    _step(assessor, policy, 28.0, 70.0, 27.0, 0.0)
    _step(assessor, policy, 28.0, 70.0, 27.0, 20.0)
    _step(assessor, policy, 30.0, 95.0, 28.0, 30.0)
    _step(assessor, policy, 30.0, 95.0, 28.0, 70.0)  # 坐稳 RISK
    # 余量够了、时间够了，但擦拭仍在途：不放开。
    v = _step(assessor, policy, 30.0, 60.0, 40.0, 200.0, wipe=True)
    assert v.state == RISK
    assert "wipe_in_progress" in v.reasons
    v = _step(assessor, policy, 30.0, 60.0, 40.0, 270.0, wipe=False)
    assert v.state == OK
