"""门区凝露风险链。

把「环境采集 → 凝露判据 → 批量开门摘除 → 擦拭/检修安排 → 恢复放开 → 不可变
审计重放」串成一条链：

采集（:mod:`sensors`）          门区空气温湿度 + 门体表面温度，支持多只表面
                                传感器冗余、断线/过期/越界/尖峰/卡值/漂移判定；
判据（:mod:`physics`/:mod:`engine`）
                                露点 + 门体温差安全裕量，瞬时值要持续确认，
                                趋势快速恶化时缩短确认时间，迟滞放行使状态
                                不抖动；阈值随季节画像切换但版本随判定落盘；
摘除（:mod:`hold`）             风险高（或传感器不可信）的门从批量开门里摘
                                出来，门控层在真正开门前做第二道防线；
擦拭（:mod:`wiping`）           摘出即开擦拭工单，擦完且裕量恢复后才放开；
                                传感器断线/漂移开的是检修工单而不是擦拭单；
编排（:mod:`controller`）       每拍采集、判定、摘除、派单、恢复的总装；
审计（:mod:`chain`/:mod:`replay`）
                                哈希链只追加，任何一次风险判定都能凭链上的
                                原始采样、画像版本和判据版本完整重放。
"""

from .chain import (
    CHAIN_FACT,
    CHAIN_HOLD,
    CHAIN_SAMPLE,
    CHAIN_WIPE,
    ChainJournal,
    ChainVerifyError,
    Entry,
    canonical_digest,
    verify_chain,
    verify_entries,
)
from .config import (
    CondensationConfig,
    CondensationProfile,
    PROFILE_RAINY,
    PROFILE_NORMAL,
    condensation_from_mapping,
    condensation_to_mapping,
    profile_for_time,
    profile_from_dict,
)
from .controller import EnvController
from .engine import DoorEnvEngine, RiskFact, RISK_HIGH, RISK_NORMAL, RISK_UNKNOWN, RISK_WATCH
from .hold import HOLD_RISK, HOLD_SENSOR, Hold, HoldReason, HoldRegistry
from .physics import CRITERION_VERSION, dew_point_c, linear_slope_c_per_s, margin_c
from .replay import ReplayResult, explain, fold_holds, replay_chain
from .sensors import (
    AmbientReading,
    DoorEnvInput,
    EnvAdapter,
    Reading,
    ScriptedAdapter,
    SensorHealth,
    SensorMonitor,
    resolve_door_input,
)
from .wiping import (
    ORDER_REPAIR,
    ORDER_WIPE,
    CleaningDispatcher,
    WipeOrder,
    WipePlanner,
    WipeStatus,
    WipeStore,
)

__all__ = [
    "CHAIN_FACT",
    "CHAIN_HOLD",
    "CHAIN_SAMPLE",
    "CHAIN_WIPE",
    "CRITERION_VERSION",
    "AmbientReading",
    "ChainJournal",
    "ChainVerifyError",
    "CleaningDispatcher",
    "CondensationConfig",
    "CondensationProfile",
    "DoorEnvEngine",
    "DoorEnvInput",
    "EnvAdapter",
    "EnvController",
    "Entry",
    "HOLD_RISK",
    "HOLD_SENSOR",
    "Hold",
    "HoldReason",
    "HoldRegistry",
    "ORDER_REPAIR",
    "ORDER_WIPE",
    "PROFILE_NORMAL",
    "PROFILE_RAINY",
    "Reading",
    "ReplayResult",
    "RiskFact",
    "RISK_HIGH",
    "RISK_NORMAL",
    "RISK_UNKNOWN",
    "RISK_WATCH",
    "ScriptedAdapter",
    "SensorHealth",
    "SensorMonitor",
    "WipeOrder",
    "WipePlanner",
    "WipeStatus",
    "WipeStore",
    "canonical_digest",
    "condensation_from_mapping",
    "condensation_to_mapping",
    "dew_point_c",
    "explain",
    "fold_holds",
    "linear_slope_c_per_s",
    "margin_c",
    "profile_for_time",
    "profile_from_dict",
    "replay_chain",
    "resolve_door_input",
    "verify_chain",
    "verify_entries",
]
