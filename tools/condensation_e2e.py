"""凝露链端到端验证脚本（无 pytest 环境也能直接跑）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from psdline.app import build_platform
from psdline.clock import ManualClock
from psdline.condensation import (
    ScriptedAdapter,
    dew_point_c,
)
from psdline.config import default_config


def make_adapter(station: str, psd_ids: list[str], regime: dict):
    """regime: psd_id -> lambda(ts) -> (air_t, rh, surface) 或故障标记。"""

    script = {}
    # 门区共享空气传感器：所有门用同一个函数取当时天气
    def air_func(ts):
        return regime["weather"](ts)[0]

    def rh_func(ts):
        return regime["weather"](ts)[1]

    script[f"{station}-air"] = ("air_t", air_func)
    script[f"{station}-rh"] = ("rh", rh_func)
    for psd_id in psd_ids:
        def make_surface(pid):
            def surface_func(ts):
                return regime["doors"][pid](ts)
            return surface_func
        script[f"{psd_id}-surface"] = ("surface", make_surface(psd_id))
    return ScriptedAdapter(script)


def main() -> int:
    root = tempfile.mkdtemp(prefix="condensation-e2e-")
    clock = ManualClock()
    platform = build_platform(default_config(), clock=clock, storage_root=root)
    platform.start()

    station = "ST1"
    gates = [g.id for g in platform.gates.by_station(station)]
    target = gates[0]
    safe_door = gates[1]

    # 物理量：先算"安全"与"起雾"需要的表面温度
    air, rh = 28.0, 88.0
    tdp = dew_point_c(air, rh)
    foggy_surface = tdp - 1.0          # 表面低于露点 1℃ → margin=+1
    safe_surface = tdp + 8.0           # 远高于露点 → margin=-8

    state = {"mode": "safe", "fail_sensor": None}

    def weather(ts):
        return air, rh

    def door_temp_factory(pid):
        def temp(ts):
            if state["fail_sensor"] == pid:
                return None  # 断线
            if pid == target:
                return foggy_surface if state["mode"] in ("fog", "fog_recover") else safe_surface
            return safe_surface
        return temp

    adapter = make_adapter(station, gates, {
        "weather": weather,
        "doors": {pid: door_temp_factory(pid) for pid in gates},
    })
    bound = platform.auto_bind_env(adapter)
    assert set(bound["bound"]) == set(gates), bound

    # ---- 1) 安全基线：连续两拍正常，不摘门 ----
    platform.env_poll()
    clock.advance(5)
    r = platform.env_poll()
    levels = {f["psd_id"]: f["level"] for f in r["facts"]}
    assert levels[target] == "normal", levels
    assert not platform.env.holds.held_ids()

    # ---- 2) 起雾：裕量越线，持续确认满 30s 后摘门并开擦拭单 ----
    state["mode"] = "fog"
    polls = []
    for _ in range(8):
        clock.advance(5)
        polls.append(platform.env_poll())
    held = platform.env.holds.held_ids()
    assert target in held, held
    hold = platform.env.holds.get(target)
    assert hold.reason == "risk"
    view = platform.env_door_view(target)
    assert view["last_fact"]["level"] == "high"
    assert view["last_fact"]["margin_c"] > 0.4
    orders = [o for o in view["active_orders"] if o["kind"] == "wipe"]
    assert len(orders) == 1, view
    print("起雾摘门:", hold.as_dict())
    print("判定理由:", view["last_fact"]["reasons"])

    # ---- 3) 摘除的门必须从批量开门里剔出来，同组其他门照开 ----
    batch = platform.batch_open(station)
    assert target in batch["held_gates"], batch
    assert batch["held_gates"][target] == "condensation_risk"
    # 门区联动第二道防线也要拦住
    try:
        from psdline.door import open_group
        open_group(platform.doors, platform.mappings, station, "G01", platform.env.holds)
        raise AssertionError("被摘门应当无法联动开门")
    except Exception as error:  # noqa: BLE001
        assert "凝露" in str(error), error

    # ---- 4) 擦拭回报完成但裕量没恢复：门继续摘着 ----
    wiped = platform.report_wiped(target, assignee="王师傅", note="玻璃已擦")
    assert wiped["held"] is True
    view = platform.env_door_view(target)
    assert all(o["status"] == "done" for o in view["active_orders"])

    # ---- 5) 恢复：裕量回到安全区，需稳满 release_dwell（雨季 120s）才放开 ----
    state["mode"] = "safe"
    ticks = 0
    released = False
    while ticks < 40:
        clock.advance(5)
        platform.env_poll()
        ticks += 1
        if not platform.env.holds.is_held(target):
            released = True
            break
    assert released, "裕量恢复并稳定后应自动放开"
    view = platform.env_door_view(target)
    assert view["hold"] is None
    assert all(o["status"] == "closed" for o in view["active_orders"]) is True or not view["active_orders"]
    print("恢复放开，经过周期数:", ticks, "工单状态全部关闭")

    # ---- 6) 传感器断线：fail-safe 摘除 + 检修单 ----
    state["fail_sensor"] = safe_door
    for _ in range(3):
        clock.advance(5)
        platform.env_poll()
    hold2 = platform.env.holds.get(safe_door)
    assert hold2 is not None and hold2.reason == "sensor", hold2
    view2 = platform.env_door_view(safe_door)
    assert any(o["kind"] == "repair" for o in view2["active_orders"])
    print("断线 fail-safe 摘除:", hold2.as_dict())

    # 恢复传感器后，检修单回报完成，下一拍判据正常即放开
    state["fail_sensor"] = None
    for _ in range(8):
        clock.advance(5)
        platform.env_poll()
    platform.report_wiped(safe_door, assignee="李工", note="更换表面探头接线")
    assert not platform.env.holds.is_held(safe_door)

    # ---- 7) 重放：哈希链校验 + 判据复算全部一致 ----
    replay = platform.env_replay()
    assert replay["chain_ok"], replay["errors"]
    assert not replay["fact_mismatches"], replay["fact_mismatches"][:2]
    assert replay["sample_count"] > 0 and replay["fact_count"] > 0
    print("重放校验: entries=%d samples=%d facts=%d 不一致=%d" % (
        replay["entries"], replay["sample_count"], replay["fact_count"],
        len(replay["fact_mismatches"])))

    # 某条高风险判定的依据可查
    high_seq = next(item["seq"] for item in replay["levels"] if item["level"] == "high")
    explain = platform.env_replay(seq=high_seq)
    assert explain["explain"]["found"]
    print("高风险判定依据 seq=%d，理由: %s" % (
        high_seq, explain["explain"]["entry"]["payload"]["fact"]["reasons"][:2]))

    # ---- 8) 重启：从同一份存储重建平台，摘除/链全部接上 ----
    platform2 = build_platform(default_config(), clock=clock, storage_root=root)
    assert platform2.env.chain.count() == platform.env.chain.count()
    summary2 = platform2.env_status()
    assert summary2["chain_entries"] == platform.env.chain.count()
    print("重启恢复链条数:", summary2["chain_entries"])

    # ---- 9) 篡改检测：直接改写链文件，校验必须抓到 ----
    chain_file = platform.env.chain.segments()[0]
    with open(chain_file, "r", encoding="utf-8") as handle:
        lines = handle.readlines()
    # 改第一条 fact 的 margin
    for index, line in enumerate(lines):
        if '"env_fact"' in line:
            payload = json.loads(line)
            payload["payload"]["fact"]["margin_c"] = 99.0
            lines[index] = json.dumps(payload, ensure_ascii=False) + "\n"
            break
    with open(chain_file, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    # 篡改后：校验能抓出断点，恢复必须拒绝（平台启动也应被挡下）。
    bad = platform.env.chain.verify()
    assert bad, "篡改后哈希校验必须失败"
    try:
        platform.env.restore_from_chain()
        raise AssertionError("篡改后必须拒绝恢复")
    except Exception as error:  # noqa: BLE001
        assert "凝露" in str(error) or "哈希" in str(error), error
    try:
        build_platform(default_config(), clock=clock, storage_root=root)
        raise AssertionError("链被篡改后平台必须拒绝启动")
    except Exception as error:  # noqa: BLE001
        assert type(error).__name__ == "RailError", error
    print("篡改已被捕获，断点 seq=%d: %s" % (bad[0].seq, bad[0].reason))

    print("全部端到端检查通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
