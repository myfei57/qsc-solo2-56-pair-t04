"""环境采集：断线、跳变、卡死、冗余漂移隔离与降级。"""

from __future__ import annotations

from psdline.dew.sensors import BAD, GOOD, SUSPECT, Channel, SensorMonitor


def _channels(values: dict[str, float | None]):
    return {
        kind: [Channel(name=name, kind=kind, read=lambda name=name: values.get(name))]
        for kind, name in (
            ("air_c", "z:air_c"),
            ("rh_pct", "z:rh_pct"),
            ("glass_c", "z:glass_c"),
        )
    }


def test_healthy_readings_fuse_good():
    values = {"z:air_c": 28.0, "z:rh_pct": 85.0, "z:glass_c": 27.0}
    monitor = SensorMonitor("z", _channels(values))
    fused = monitor.poll(now=0.0, stale_after_s=90.0)
    assert all(sample.quality == GOOD for sample in fused.values())
    assert fused["air_c"].value == 28.0


def test_wire_break_goes_bad_then_recovers():
    values = {"z:air_c": 28.0, "z:rh_pct": 85.0, "z:glass_c": 27.0}
    monitor = SensorMonitor("z", _channels(values))
    monitor.poll(0.0, 90.0)
    values["z:glass_c"] = None
    # 第一个丢点周期仍保留旧值（未超过 stale）。
    fused = monitor.poll(30.0, 90.0)
    assert fused["glass_c"].quality == GOOD
    # 超过 stale_after 仍读不到：bad。
    fused = monitor.poll(130.0, 90.0)
    assert fused["glass_c"].quality == BAD
    assert "no_data" in fused["glass_c"].reasons
    # 接线恢复：重新 good。
    values["z:glass_c"] = 27.0
    fused = monitor.poll(160.0, 90.0)
    assert fused["glass_c"].quality == GOOD


def test_rate_spike_keeps_value_trusted_but_marked():
    # 单通道快速变化是真实气象（来雨、车门贯入）也会有的，不能因此丢弃读数；
    # 证据照样记录，供冗余分歧时坐实漂移，以及事后核查。
    values = {"z:air_c": 28.0, "z:rh_pct": 85.0, "z:glass_c": 27.0}
    monitor = SensorMonitor("z", _channels(values))
    monitor.poll(0.0, 90.0)
    values["z:air_c"] = 34.0  # 一分钟内升 6 ℃，超 3 ℃/min 软线
    fused = monitor.poll(60.0, 90.0)
    assert fused["air_c"].quality == GOOD
    evidence = {item["channel"]: item for item in fused["air_c"].channels}
    assert "rate_spike" in evidence["z:air_c"]["reasons"]
    assert fused["air_c"].value == 34.0


def test_out_of_range_is_bad():
    values = {"z:air_c": 28.0, "z:rh_pct": 130.0, "z:glass_c": 27.0}
    monitor = SensorMonitor("z", _channels(values))
    fused = monitor.poll(0.0, 90.0)
    assert fused["rh_pct"].quality == BAD
    assert "out_of_range" in fused["rh_pct"].reasons


def test_redundant_drift_isolates_bad_channel_and_degrades():
    # 双通道气温：A 持续异常爬升（每分钟 +4 ℃，反复跳变软线），B 稳定；
    # 持续分歧 + 累计跳变证据后隔离 A。
    values = {"a": 28.0, "b": 28.0}
    channels = {
        "air_c": [
            Channel(name="a", kind="air_c", read=lambda: values["a"]),
            Channel(name="b", kind="air_c", read=lambda: values["b"]),
        ],
        "rh_pct": [Channel(name="z:rh_pct", kind="rh_pct", read=lambda: 85.0)],
        "glass_c": [Channel(name="z:glass_c", kind="glass_c", read=lambda: 27.0)],
    }
    monitor = SensorMonitor("z", channels)
    monitor.poll(0.0, 90.0)
    for step in range(1, 5):
        values["a"] = 28.0 + step * 4.0
        fused = monitor.poll(step * 60.0, 90.0)
    evidence = {item["channel"]: item for item in fused["air_c"].channels}
    assert evidence["a"]["quality"] == BAD
    assert "drift" in evidence["a"]["reasons"]
    # 好通道不被坏通道污染。
    assert evidence["b"]["quality"] == GOOD
    # 剩余通道继续供数，但融合结果降级为 suspect（部分失败可运行、不可自动放开）。
    assert fused["air_c"].value == 28.0
    assert fused["air_c"].quality == SUSPECT
    assert "channel_isolated" in fused["air_c"].reasons


def test_healthy_redundant_channels_average():
    channels = {
        "air_c": [
            Channel(name="a", kind="air_c", read=lambda: 27.8),
            Channel(name="b", kind="air_c", read=lambda: 28.2),
        ],
        "rh_pct": [Channel(name="r", kind="rh_pct", read=lambda: 85.0)],
        "glass_c": [Channel(name="g", kind="glass_c", read=lambda: 27.0)],
    }
    monitor = SensorMonitor("z", channels)
    fused = monitor.poll(0.0, 90.0)
    assert abs(fused["air_c"].value - 28.0) < 1e-9
    assert fused["air_c"].quality == GOOD
