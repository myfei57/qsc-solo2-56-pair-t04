"""门区环境拓扑：门区、量纲与物理通道的绑定。

拓扑描述"哪个门区有哪些传感器"。它同时服务实时链路与重放：实时侧用通道驱动
轮询，重放侧不需要驱动（值直接来自历史采样），因此驱动允许为空。

通道命名约定：``<门区>:<量纲>[#序号]``，例如 ``ST1-G01:air_c#1``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .sensors import Channel, unconnected

KINDS = ("air_c", "rh_pct", "glass_c")


@dataclass(frozen=True)
class ZoneTopology:
    """一个门区的通道布置。每个量纲至少一个通道，冗余通道给同一量纲多条。"""

    zone_id: str
    station_id: str
    channels: dict[str, tuple[str, ...]]
    readers: dict[str, Callable[[], Optional[float]]] = field(default_factory=dict)

    def build_channels(self) -> dict[str, list[Channel]]:
        grouped: dict[str, list[Channel]] = {}
        for kind in KINDS:
            names = self.channels.get(kind, ())
            if not names:
                raise ValueError(f"门区 {self.zone_id} 缺少量纲 {kind} 的通道")
            grouped[kind] = [
                Channel(name=name, kind=kind, read=self.readers.get(name, unconnected))
                for name in names
            ]
        return grouped

    def channel_names(self) -> list[str]:
        return [name for names in self.channels.values() for name in names]


def default_channels(zone_id: str) -> dict[str, tuple[str, ...]]:
    """给出最小可用布置：每量纲单通道。"""

    return {
        "air_c": (f"{zone_id}:air_c",),
        "rh_pct": (f"{zone_id}:rh_pct",),
        "glass_c": (f"{zone_id}:glass_c",),
    }


def redundant_channels(zone_id: str, duplicate: tuple[str, ...] = ("air_c", "rh_pct")) -> dict[str, tuple[str, ...]]:
    """给出冗余布置：指定量纲双通道，玻璃温度单通道。"""

    channels: dict[str, tuple[str, ...]] = {}
    for kind in KINDS:
        if kind in duplicate:
            channels[kind] = (f"{zone_id}:{kind}#1", f"{zone_id}:{kind}#2")
        else:
            channels[kind] = (f"{zone_id}:{kind}",)
    return channels
