"""凝露防护的控制台接口。"""

from __future__ import annotations

from psdline.console.api import Api


def test_dew_http_chain(online, clock):
    api = Api(online)
    station, group = "ST1", "G01"

    def sample(air, rh, glass):
        code, payload = api.handle(
            "POST",
            "/api/dew/sample",
            {},
            {"station_id": station, "group": group, "air_c": air, "rh_pct": rh, "glass_c": glass},
        )
        assert code == 200, (code, payload)
        return payload

    for _ in range(4):
        sample(28.0, 70.0, 27.0)
        clock.advance(60)
    code, status = api.handle("GET", "/api/dew", {}, {})
    assert code == 200
    assert status["zones"][f"{station}-{group}"]["state"] == "OK"

    sample(29.0, 80.0, 28.6)
    clock.advance(60)
    sample(30.0, 90.0, 28.0)  # 玻璃破露点

    code, batch = api.handle("POST", "/api/batch-open", {}, {"station_id": station})
    assert code == 200
    assert batch["skipped"].get(group) == "dew"

    code, replay = api.handle("GET", "/api/dew/replay", {}, {})
    assert code == 200
    assert replay["integrity"]["ok"]
    assert replay["ok"]

    code, explain = api.handle(
        "GET", "/api/dew/explain", {"station_id": station, "group": group}, {}
    )
    assert code == 200
    assert explain["state"] == "RISK"
    assert "instant_trip" in explain["why"]
    assert explain["margin_c"] < 0
    assert set(explain["inputs"]) == {"air_c", "rh_pct", "glass_c"}
    assert any(item["kind"] == "dew.workorder.created" for item in explain["actions_after"])


def test_dew_sample_validation(online):
    api = Api(online)
    code, payload = api.handle(
        "POST", "/api/dew/sample", {}, {"station_id": "ST1", "air_c": 1.0}
    )
    assert code == 400  # 缺少 group
