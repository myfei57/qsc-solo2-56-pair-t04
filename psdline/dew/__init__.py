"""凝露风险防护子系统。

链路：环境采集（:mod:`sensors`）→ 风险判定（:mod:`assess`）→ 摘放门
（:class:`~psdline.dew.coordinator.DewSystem` 与 :mod:`psdline.door.batch`）
→ 擦拭/核查（:mod:`cleaning`），所有事实写入哈希链（:mod:`events`），
事后由 :mod:`replay` 重放并解释。
"""

from __future__ import annotations

from .assess import DARK, OK, RISK, Assessment, RiskAssessor
from .cleaning import CleaningPlanner, WorkOrder
from .coordinator import DewHoldRegistry, DewSystem, ZoneChain
from .events import ChainEvent, EventChain
from .physics import condensation_margin_c, dew_point_c
from .policy import (
    DewPolicy,
    DewPolicySchedule,
    RainySeason,
    rainy_policy,
    standard_policy,
)
from .replay import Replayer
from .sensors import BAD, GOOD, SUSPECT, Channel, FusedSample, SensorMonitor
from .topology import ZoneTopology, default_channels, redundant_channels

__all__ = [
    "DARK",
    "OK",
    "RISK",
    "Assessment",
    "RiskAssessor",
    "CleaningPlanner",
    "WorkOrder",
    "DewHoldRegistry",
    "DewSystem",
    "ZoneChain",
    "ChainEvent",
    "EventChain",
    "condensation_margin_c",
    "dew_point_c",
    "DewPolicy",
    "DewPolicySchedule",
    "RainySeason",
    "rainy_policy",
    "standard_policy",
    "Replayer",
    "BAD",
    "GOOD",
    "SUSPECT",
    "Channel",
    "FusedSample",
    "SensorMonitor",
    "ZoneTopology",
    "default_channels",
    "redundant_channels",
]
