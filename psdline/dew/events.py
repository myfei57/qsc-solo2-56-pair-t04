"""凝露链路的不可变事件日志（哈希链）。

环境采集、风险判定、摘门/放开、擦拭工单、策略切换都以事件形式追加到同一条链：
每条事件携带序号、时间戳、类型、负载与前一条事件的 SHA-256。任何一条被删改都会
让从该点开始的哈希全部对不上，``verify`` 逐条复算即可发现。

事件只追加，不提供修改与删除入口；落盘复用分段 JSONL 流水（fsync），重启后从
流水重建链。判定证据随判定事件一起入链，因此事后可以回答"当时凭什么报了风险"，
也可以用事件流重放整条链路。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..store import JsonlJournal

GENESIS_SEQ = 0


def canonical(payload: Any) -> bytes:
    """确定性 JSON 序列化：键排序、无空白、非 ASCII 原样，保证哈希可复算。"""

    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


@dataclass(frozen=True)
class ChainEvent:
    """哈希链上的一条事件。"""

    seq: int
    ts: float
    kind: str
    payload: dict = field(default_factory=dict)
    prev_hash: str = ""
    digest: str = ""

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ChainEvent":
        return cls(
            seq=int(raw.get("seq", -1)),
            ts=float(raw.get("ts", 0.0)),
            kind=str(raw.get("kind", "")),
            payload=dict(raw.get("payload", {})),
            prev_hash=str(raw.get("prev_hash", "")),
            digest=str(raw.get("digest", "")),
        )


def digest_of(seq: int, ts: float, kind: str, payload: dict, prev_hash: str) -> str:
    """复算一条事件的摘要。"""

    body = canonical(
        {
            "seq": seq,
            "ts": round(float(ts), 6),
            "kind": kind,
            "payload": payload,
            "prev_hash": prev_hash,
        }
    )
    return hashlib.sha256(body).hexdigest()


class EventChain:
    """只追加的哈希链：内存索引 + 分段 JSONL 落盘。"""

    def __init__(self, journal: JsonlJournal | None = None, chain_id: str | None = None) -> None:
        self._journal = journal
        self._events: list[ChainEvent] = []
        self._by_kind: dict[str, list[ChainEvent]] = {}
        if journal is not None:
            for raw in journal.read_all():
                if isinstance(raw, dict) and "digest" in raw:
                    self._index(ChainEvent.from_dict(raw))
        if not self._events:
            self._append_genesis(chain_id or f"dew-{uuid.uuid4().hex[:16]}")

    def _index(self, event: ChainEvent) -> None:
        self._events.append(event)
        self._by_kind.setdefault(event.kind, []).append(event)

    def _append_genesis(self, chain_id: str) -> None:
        self.append("_genesis", 0.0, chain_id=chain_id)

    @property
    def chain_id(self) -> str:
        return str(self._events[0].payload.get("chain_id", ""))

    def head(self) -> ChainEvent:
        return self._events[-1]

    def append(self, event_kind: str, ts: float, **payload: Any) -> ChainEvent:
        """追加一条事件并落盘。"""

        if not event_kind:
            raise ValueError("事件类型为空")
        seq = self._events[-1].seq + 1 if self._events else GENESIS_SEQ
        prev_hash = self._events[-1].digest if self._events else ""
        digest = digest_of(seq, ts, event_kind, payload, prev_hash)
        event = ChainEvent(
            seq=seq, ts=float(ts), kind=event_kind, payload=dict(payload),
            prev_hash=prev_hash, digest=digest,
        )
        if self._journal is not None:
            self._journal.append(event.as_dict())
        self._index(event)
        return event

    def events(self, kind: str | None = None) -> list[ChainEvent]:
        """返回全部事件，或按类型过滤。"""

        if kind is None:
            return list(self._events)
        return list(self._by_kind.get(kind, ()))

    def verify(self) -> dict:
        """从创世事件起逐条复算哈希，返回链完整性报告。"""

        previous = ""
        for index, event in enumerate(self._events):
            expected = digest_of(event.seq, event.ts, event.kind, event.payload, previous)
            if index == 0:
                if event.kind != "_genesis":
                    return break_report(event, "首条事件不是创世事件")
            if event.prev_hash != previous:
                return break_report(event, "前链摘要不衔接")
            if event.digest != expected:
                return break_report(event, "摘要不匹配，记录可能被篡改")
            if index > 0 and event.seq != self._events[index - 1].seq + 1:
                return break_report(event, "序号不连续")
            previous = event.digest
        return {
            "ok": True,
            "events": len(self._events),
            "chain_id": self.chain_id,
            "first_bad_seq": None,
            "reason": "",
        }


def break_report(event: ChainEvent, reason: str) -> dict:
    return {
        "ok": False,
        "events": 0,
        "chain_id": "",
        "first_bad_seq": event.seq,
        "reason": reason,
    }
