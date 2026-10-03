"""端到端：环境采集→判定→摘/放门→批量开门→擦拭，以及不可变审计与重放。"""

from __future__ import annotations

import json
import pathlib

import pytest

from psdline.app import build_platform
from psdline.clock import ManualClock
from psdline.config import default_config, with_storage_root
from psdline.dew import DewPolicySchedule, EventChain, Replayer
from psdline.dew.coordinator import DewSystem
from psdline.dew.topology import ZoneTopology


# 雨季的一个周一（UTC），保证走雨季策略。
RAIN_TS = 1_782_000_000.0  # 2026-06-20 前后


def _platform(tmp_path):
    config = with_storage_root(default_config(), str(tmp_path / "state"))
    return build_platform(config, clock=ManualClock(start=RAIN_TS), storage_root=str(tmp_path / "state"))


def test_full_risk_hold_wipe_release_cycle(tmp_path):
    platform = _platform(tmp_path)
    platform.start()
    station, group = "ST1", "G01"

    # 雨季安全环境，先喂过 requalify。
    platform.clock.advance(0)
    platform.dew_sample(station, group, 28.0, 70.0, 27.0)
    for _ in range(3):
        platform.clock.advance(60)
        platform.dew_sample(station, group, 28.0, 70.0, 27.0)
    assert platform.status()["dew"]["zones"][f"{station}-{group}"]["state"] == "OK"

    # 此时批量开门不摘该门区。
    report = platform.batch_open(station)
    assert group in report["opened"]
    for gate in platform.gates.by_station(station):
        if gate.group == group:
            platform.gates.set_open(gate.id, False)

    # 起雾：雨季渐变（两拍过渡，让湿度变化率落在物理合理区间）。
    platform.clock.advance(60)
    platform.dew_sample(station, group, 29.0, 80.0, 28.6)
    platform.clock.advance(60)
    platform.dew_sample(station, group, 30.0, 90.0, 28.0)  # 露点≈28.3，玻璃已破露点（margin<0）
    state = platform.status()["dew"]
    assert f"{station}-{group}" in state["holds"]
    report = platform.batch_open(station)
    assert report["skipped"].get(group) == "dew"
    assert group not in report["opened"]

    # 擦拭工单已生成并自动派单。
    orders = [order for order in platform.dew.planner.list_orders(f"{station}-{group}")]
    wipes = [order for order in orders if order.kind == "WIPE"]
    assert wipes and wipes[0].state == "dispatched"

    # 擦完但仍起雾：不应放开。
    order_id = wipes[0].id
    platform.dew_report_wipe(order_id)
    platform.clock.advance(60)
    platform.dew_sample(station, group, 30.0, 90.0, 28.0)
    assert platform.dew.holds.is_held(f"{station}-{group}")

    # 雨季环境改善（空调投入），持续高过解除线后放开。
    for _ in range(10):
        platform.clock.advance(60)
        platform.dew_sample(station, group, 26.0, 55.0, 30.0)
    assert not platform.dew.holds.is_held(f"{station}-{group}")
    report = platform.batch_open(station)
    assert group in report["opened"]


def test_wire_break_holds_dark_and_inspection_then_recovers(tmp_path):
    platform = _platform(tmp_path)
    platform.start()
    station, group = "ST1", "G02"
    key = f"{station}-{group}"
    for _ in range(4):
        platform.dew_sample(station, group, 28.0, 70.0, 27.0)
        platform.clock.advance(60)
    platform.dew_sample(station, group, 28.0, 70.0, 27.0)
    assert not platform.dew.holds.is_held(key)

    # 湿度通道断线超过 stale 窗口：进入 DARK，摘门，派核查单（不是擦拭单）。
    platform.clock.advance(120)
    platform.dew_sample(station, group, 28.0, None, 27.0)
    assert platform.dew.holds.describe(key)["state"] == "DARK"
    inspects = [
        order for order in platform.dew.planner.list_orders(key)
        if order.kind == "INSPECT" and order.active()
    ]
    assert inspects

    # 接线修复：数据健康持续 requalify 后自动放开，核查单取消。
    for _ in range(6):
        platform.clock.advance(60)
        platform.dew_sample(station, group, 28.0, 70.0, 27.0)
    assert not platform.dew.holds.is_held(key)
    assert not any(order.active() for order in platform.dew.planner.list_orders(key))


def test_dispatch_failure_is_retried(tmp_path):
    platform = _platform(tmp_path)
    platform.start()
    station, group = "ST1", "G03"

    platform.dew._dispatcher = lambda order, now: (True, "ok")  # 观察期先受理核查单
    for _ in range(3):
        platform.dew_sample(station, group, 28.0, 70.0, 27.0)
        platform.clock.advance(60)
    platform.dew_sample(station, group, 28.0, 70.0, 27.0)
    # 健康后换上门禁网关：前两次派单失败，第三次受理。
    attempts = {"n": 0}

    def flaky(order, now):
        attempts["n"] += 1
        return attempts["n"] >= 3, "retry" if attempts["n"] < 3 else "ok"

    platform.dew._dispatcher = flaky
    # 渐变进入风险。
    platform.clock.advance(60)
    platform.dew_sample(station, group, 29.0, 80.0, 28.6)
    platform.clock.advance(60)
    platform.dew_sample(station, group, 30.0, 90.0, 28.0)
    order = [o for o in platform.dew.planner.list_orders() if o.kind == "WIPE"][0]
    assert order.state == "failed" and order.attempts == 1
    # 超过重试间隔后再试：第二次仍失败，第三次受理。
    platform.clock.advance(60)
    platform.dew_sample(station, group, 30.0, 90.0, 28.0)
    assert order.state == "failed" and order.attempts == 2
    platform.clock.advance(60)
    platform.dew_sample(station, group, 30.0, 90.0, 28.0)
    assert order.state == "dispatched"
    assert order.attempts == 3


def test_wipe_failure_retried_then_accepted(tmp_path):
    platform = _platform(tmp_path)
    platform.start()
    station, group = "ST1", "G04"
    key = f"{station}-{group}"
    # 网关前两次拒收擦拭派单。
    attempts = {"n": 0}

    def flaky(order, now):
        attempts["n"] += 1
        return attempts["n"] >= 2, "busy" if attempts["n"] < 2 else "ok"

    platform.dew._dispatcher = lambda o, n: (True, "ok")
    for _ in range(4):
        platform.dew_sample(station, group, 28.0, 70.0, 27.0)
        platform.clock.advance(60)
    platform.dew._dispatcher = flaky
    platform.dew_sample(station, group, 29.0, 80.0, 28.6)
    platform.clock.advance(60)
    platform.dew_sample(station, group, 30.0, 90.0, 28.0)
    order = [o for o in platform.dew.planner.list_orders(key) if o.kind == "WIPE"][0]
    assert order.state == "failed" and order.attempts == 1
    # 班组回报失败也要被记录，且不新建重复单。
    platform.dew_report_failed(order.id, "still foggy")
    assert order.state == "failed"
    assert len([o for o in platform.dew.planner.list_orders(key) if o.kind == "WIPE"]) == 1
    # 下一个重试周期受理。
    platform.clock.advance(60)
    platform.dew_sample(station, group, 30.0, 90.0, 28.0)
    assert order.state == "dispatched" and order.attempts == 2


def test_hashchain_detects_tampering(tmp_path):
    platform = _platform(tmp_path)
    platform.start()
    platform.dew_sample("ST1", "G01", 28.0, 70.0, 27.0)
    chain = platform.dew.chain
    assert chain.verify()["ok"]

    # 直接改落盘文件，重新加载链，必须在被改位置断链。
    files = sorted(pathlib.Path(platform.store.root).glob("dew-chain-*.jsonl"))
    target = files[0]
    lines = target.read_text(encoding="utf-8").splitlines()
    # 改第一条非创世记录的负载。
    for index, line in enumerate(lines[1:], start=1):
        record = json.loads(line)
        if record["kind"] == "dew.sample":
            record["payload"]["value"] = 999.0
            lines[index] = json.dumps(record, ensure_ascii=False)
            break
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    reloaded = EventChain(type(chain._journal)(platform.store.root, "dew-chain", segment_limit=1024))
    report = reloaded.verify()
    assert not report["ok"]
    assert report["first_bad_seq"] is not None


def test_replay_reproduces_verdict_edges(tmp_path):
    platform = _platform(tmp_path)
    platform.start()
    station, group = "ST1", "G01"
    for _ in range(3):
        platform.dew_sample(station, group, 28.0, 70.0, 27.0)
        platform.clock.advance(60)
    platform.clock.advance(60)
    platform.dew_sample(station, group, 29.0, 80.0, 28.6)
    platform.clock.advance(60)
    platform.dew_sample(station, group, 30.0, 90.0, 28.0)  # 瞬时风险
    wipe = [o for o in platform.dew.planner.list_orders(f"{station}-{group}") if o.kind == "WIPE"][0]
    platform.dew_report_wipe(wipe.id)
    for _ in range(9):
        platform.clock.advance(60)
        platform.dew_sample(station, group, 26.0, 55.0, 30.0)

    replay = Replayer(platform.dew.chain, platform.dew.schedule).run()
    assert replay["integrity"]["ok"]
    assert replay["ok"], replay["mismatches"]
    assert replay["edges_rebuilt"] == replay["edges_recorded"]
    assert replay["edges_recorded"] >= 2  # 至少有 RISK 与 OK 两条边

    explanation = platform.dew_explain(station, group)
    assert explanation["state"] == "RISK"
    assert "instant_trip" in explanation["why"]
    assert explanation["policy"]["version"].startswith("rainy")
    assert explanation["margin_c"] < 0
    # 证据里带齐三路输入及其质量。
    assert set(explanation["inputs"]) == {"air_c", "rh_pct", "glass_c"}
    # 摘门之后确实跟了工单动作。
    assert any(item["kind"] == "dew.workorder.created" for item in explanation["actions_after"])


def test_chain_survives_restart(tmp_path):
    config = with_storage_root(default_config(), str(tmp_path / "state"))
    platform = build_platform(config, clock=ManualClock(start=RAIN_TS), storage_root=str(tmp_path / "state"))
    platform.start()
    platform.dew_sample("ST1", "G01", 28.0, 70.0, 27.0)
    seq_before = platform.dew.chain.head().seq

    platform2 = build_platform(config, clock=ManualClock(start=RAIN_TS + 60), storage_root=str(tmp_path / "state"))
    # 重启不重新登记拓扑；恢复后继续追加，新采样的 3 条样本事件接在链尾。
    topo_after = len(platform2.dew.chain.events("dew.topology"))
    topo_before = len(platform.dew.chain.events("dew.topology"))
    assert topo_after == topo_before
    assert platform2.dew.chain.verify()["ok"]
    platform2.dew_sample("ST1", "G01", 28.0, 70.0, 27.0)
    assert platform2.dew.chain.head().seq >= seq_before + 3
