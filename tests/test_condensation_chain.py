"""凝露链端到端：采集容错、判据、摘除、擦拭、恢复、重放、篡改检测。"""

from __future__ import annotations

import json
import os

import pytest

from psdline.app import build_platform
from psdline.clock import ManualClock
from psdline.condensation import (
    ORDER_REPAIR,
    RISK_HIGH,
    RISK_NORMAL,
    RISK_UNKNOWN,
    ScriptedAdapter,
    dew_point_c,
)
from psdline.config import default_config


@pytest.fixture()
def env_platform(tmp_path):
    clock = ManualClock()
    platform = build_platform(
        default_config(), clock=clock, storage_root=str(tmp_path / "state")
    )
    platform.start()
    return platform, clock


def _steady_adapter(psd_ids, values):
    """values: psd_id -> (air, rh, surface 或 None 表示断线)；空气按站共享。"""

    stations = {}
    for pid in psd_ids:
        stations.setdefault(pid.split("-")[0], pid)

    def script_for(station, gates):
        air, rh, _ = values[gates[0]]
        script = {
            f"{station}-air": ("air_t", lambda ts, a=air: a),
            f"{station}-rh": ("rh", lambda ts, h=rh: h),
        }
        for pid in gates:
            def make(p=pid):
                def surf(ts):
                    return values[p][2]
                return surf
            script[f"{pid}-surface"] = ("surface", make())
        return script

    grouped: dict[str, list[str]] = {}
    for pid in psd_ids:
        grouped.setdefault(pid.split("-")[0], []).append(pid)
    script: dict = {}
    for station, gates in grouped.items():
        script.update(script_for(station, gates))
    return ScriptedAdapter(script)


def test_full_risk_hold_wipe_release_cycle(env_platform):
    platform, clock = env_platform
    station = "ST1"
    gates = [g.id for g in platform.gates.by_station(station)]
    target, other = gates[0], gates[1]

    air, rh = 28.0, 88.0
    tdp = dew_point_c(air, rh)
    safe = tdp + 8.0
    values = {pid: (air, rh, safe) for pid in gates}

    adapter = _steady_adapter(gates, values)
    platform.auto_bind_env(adapter)

    platform.env_poll()
    clock.advance(5)
    platform.env_poll()
    assert not platform.env.holds.held_ids()

    # 起雾：表面降到露点以下
    values[target] = (air, rh, tdp - 1.0)
    held = False
    for _ in range(12):
        clock.advance(5)
        platform.env_poll()
        if platform.env.holds.is_held(target):
            held = True
            break
    assert held
    hold = platform.env.holds.get(target)
    assert hold.reason == "risk"
    view = platform.env_door_view(target)
    assert view["last_fact"]["level"] == RISK_HIGH

    # 摘门进批量开门明细，第二道防线拦截联动
    batch = platform.batch_open(station)
    assert batch["held_gates"].get(target) == "condensation_risk"

    # 擦拭完成但雾没散：不放
    platform.report_wiped(target, assignee="班组")
    assert platform.env.holds.is_held(target)

    # 雾散：稳定满放行时间后自动放开、工单关闭
    values[target] = (air, rh, safe)
    for _ in range(60):
        clock.advance(5)
        platform.env_poll()
        if not platform.env.holds.is_held(target):
            break
    assert not platform.env.holds.is_held(target)
    assert all(o["status"] == "closed" for o in platform.env_status()["orders"]
               if o["psd_id"] == target)


def test_sensor_offline_is_fail_safe_with_repair_order(env_platform):
    platform, clock = env_platform
    station = "ST1"
    gates = [g.id for g in platform.gates.by_station(station)]
    target = gates[2]

    air, rh = 26.0, 70.0
    tdp = dew_point_c(air, rh)
    values = {pid: (air, rh, tdp + 6.0) for pid in gates}
    adapter = _steady_adapter(gates, values)
    platform.auto_bind_env(adapter)

    platform.env_poll()
    clock.advance(5)
    platform.env_poll()
    assert platform.env_door_view(target)["last_fact"]["level"] == RISK_NORMAL

    # 表面传感器断线
    adapter.fail(f"{target}-surface")
    for _ in range(3):
        clock.advance(5)
        platform.env_poll()
    hold = platform.env.holds.get(target)
    assert hold is not None and hold.reason == "sensor"
    view = platform.env_door_view(target)
    assert view["last_fact"]["level"] == RISK_UNKNOWN
    assert any(o["kind"] == ORDER_REPAIR for o in view["active_orders"])

    # 断线恢复 + 检修回报后放开
    adapter.recover(f"{target}-surface")
    for _ in range(8):
        clock.advance(5)
        platform.env_poll()
    platform.report_wiped(target, assignee="检修")
    assert not platform.env.holds.is_held(target)


def test_out_of_range_reading_isolated_not_trusted(env_platform):
    platform, clock = env_platform
    station = "ST1"
    gates = [g.id for g in platform.gates.by_station(station)]
    target = gates[3]
    air, rh = 25.0, 60.0
    tdp = dew_point_c(air, rh)
    values = {pid: (air, rh, tdp + 5.0) for pid in gates}
    adapter = _steady_adapter(gates, values)
    platform.auto_bind_env(adapter)
    platform.env_poll()

    # 物理上不可能的表面温度 → 越界隔离，门判 UNKNOWN 摘除
    values[target] = (air, rh, 200.0)
    clock.advance(5)
    platform.env_poll()
    assert platform.env.holds.get(target).reason == "sensor"
    assert f"{target}-surface" in platform.env.monitor.suspect_ids()


def test_replay_recomputes_history_and_detects_tampering(env_platform):
    platform, clock = env_platform
    station = "ST1"
    gates = [g.id for g in platform.gates.by_station(station)]
    target = gates[0]
    air, rh = 29.0, 92.0
    tdp = dew_point_c(air, rh)
    values = {pid: (air, rh, tdp - 1.0) for pid in gates}
    adapter = _steady_adapter(gates, values)
    platform.auto_bind_env(adapter)
    for _ in range(10):
        clock.advance(5)
        platform.env_poll()
    assert platform.env.holds.is_held(target)

    replay = platform.env_replay()
    assert replay["chain_ok"]
    assert replay["fact_mismatches"] == []
    assert replay["fact_count"] >= 10

    # 篡改磁盘链文件
    chain_file = platform.env.chain.segments()[0]
    with open(chain_file, encoding="utf-8") as handle:
        lines = handle.readlines()
    for index, line in enumerate(lines):
        if '"env_fact"' in line:
            payload = json.loads(line)
            payload["payload"]["fact"]["margin_c"] = -99.0
            lines[index] = json.dumps(payload, ensure_ascii=False) + "\n"
            break
    with open(chain_file, "w", encoding="utf-8") as handle:
        handle.writelines(lines)

    from psdline.condensation import ChainJournal
    disk = ChainJournal(platform.store.root)
    assert disk.verify()
    with pytest.raises(Exception):
        platform.env.restore_from_chain()


def test_restart_restores_hold_state(tmp_path):
    root = str(tmp_path / "state")
    clock = ManualClock()
    platform = build_platform(default_config(), clock=clock, storage_root=root)
    platform.start()
    station = "ST1"
    gates = [g.id for g in platform.gates.by_station(station)]
    target = gates[4]
    air, rh = 27.0, 95.0
    tdp = dew_point_c(air, rh)
    values = {pid: (air, rh, tdp - 1.5) for pid in gates}
    adapter = _steady_adapter(gates, values)
    platform.auto_bind_env(adapter)
    for _ in range(12):
        clock.advance(5)
        platform.env_poll()
    assert platform.env.holds.is_held(target)
    chain_count = platform.env.chain.count()

    # 用同一存储重新构造：绑定、哈希链、摘除表全部接上
    rebuilt = build_platform(default_config(), clock=clock, storage_root=root)
    assert rebuilt.env.chain.count() == chain_count
    assert rebuilt.env.holds.is_held(target)
    assert rebuilt.env.bound_ids() == platform.env.bound_ids()


def test_force_release_requires_operator_and_reason(env_platform):
    platform, _clock = env_platform
    station = "ST1"
    target = platform.gates.by_station(station)[5].id
    hold = platform.env.holds
    from psdline.condensation.hold import Hold

    hold.restore(Hold(psd_id=target, reason="risk", since=1.0, incident_id="inc-x",
                      profile_version="rainy-v1", margin_c=1.0, level="high"))
    with pytest.raises(Exception):
        platform.force_release_env(target, "", "")
    result = platform.force_release_env(target, "值班员甲", "司机已人工确认视线良好")
    assert result["ok"]
    assert not hold.is_held(target)
    # 强开事件在链上
    kinds = [e.kind for e in platform.env.chain.read_all()]
    assert "env_hold" in kinds
