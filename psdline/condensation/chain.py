"""凝露链：只追加的哈希链审计。

环境采样、风险判定、摘门/放行、擦拭工单的每一次状态变化都作为一条链上条目
落盘。条目按全局序号排列，每条携带前一条的 SHA-256；校验时重算哈希并核对
前驱指针，任何改写、删除、乱序都会在第一处断点暴露，事后无法"把当时的风险
记录改成正常"。

链与既有审计流水（:class:`psdline.audit.AuditLog`）并存：那条流水服务控制台
检索与时间线，这条链服务"不可变 + 可重放"，两者记录同一批事件、互相印证。
链按分段 JSONL 落盘（与 :class:`psdline.store.JsonlJournal` 同样的滚动方式），
写入 ``fsync``，崩溃最多丢最后半条（那一条本来就没有完整落盘）。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from ..errors import StorageError

GENESIS_HASH = "0" * 64

CHAIN_SAMPLE = "env_sample"
CHAIN_FACT = "env_fact"
CHAIN_HOLD = "env_hold"
CHAIN_WIPE = "env_wipe"
CHAIN_NOTE = "env_note"


@dataclass(frozen=True)
class Entry:
    """哈希链上的一条不可变条目。"""

    seq: int
    ts: float
    kind: str
    psd_id: str
    payload: dict
    prev_hash: str
    hash: str = ""

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "psd_id": self.psd_id,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Entry":
        payload = raw.get("payload", {})
        return cls(
            seq=int(raw.get("seq", -1)),
            ts=float(raw.get("ts", 0.0)),
            kind=str(raw.get("kind", "")),
            psd_id=str(raw.get("psd_id", "")),
            payload=dict(payload) if isinstance(payload, dict) else {},
            prev_hash=str(raw.get("prev_hash", "")),
            hash=str(raw.get("hash", "")),
        )


@dataclass(frozen=True)
class ChainVerifyError:
    """一处链校验断点。"""

    seq: int
    reason: str
    expected: str = ""
    actual: str = ""

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "reason": self.reason,
            "expected": self.expected,
            "actual": self.actual,
        }


def canonical_digest(
    seq: int,
    ts: float,
    kind: str,
    psd_id: str,
    payload: dict,
    prev_hash: str,
) -> str:
    """按规范化字节序列化计算条目哈希。

    规范化（sort_keys + 固定分隔符）保证 Python 版本/字典插入顺序不同也得到
    同一哈希，这是跨时间重放可复算的前提。
    """

    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    material = f"{seq}\n{ts!r}\n{kind}\n{psd_id}\n{body}\n{prev_hash}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ChainJournal:
    """分段 JSONL 哈希链。"""

    def __init__(self, root: str, topic: str = "envchain", segment_limit: int = 1024) -> None:
        self._root = os.path.abspath(root)
        self._topic = topic
        self._segment_limit = max(1, segment_limit)
        self._lock = threading.RLock()
        os.makedirs(self._root, exist_ok=True)
        entries = self._read_all_locked()
        self._entries: list[Entry] = entries
        self._next_seq = (entries[-1].seq + 1) if entries else 0
        self._tail = entries[-1].hash if entries else GENESIS_HASH

    @property
    def topic(self) -> str:
        return self._topic

    def _segment_path(self, index: int) -> str:
        return os.path.join(self._root, f"{self._topic}-{index:05d}.jsonl")

    def append(self, ts: float, kind: str, psd_id: str, payload: dict[str, Any]) -> Entry:
        """追加一条并返回带哈希的条目。"""

        if not kind:
            raise StorageError("链条目类型为空", component="condensation")
        with self._lock:
            seq = self._next_seq
            digest = canonical_digest(seq, float(ts), kind, psd_id, payload, self._tail)
            entry = Entry(
                seq=seq,
                ts=float(ts),
                kind=kind,
                psd_id=psd_id,
                payload=dict(payload),
                prev_hash=self._tail,
                hash=digest,
            )
            path = self._segment_path(seq // self._segment_limit)
            try:
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry.as_dict(), ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as error:
                raise StorageError(f"追加凝露链失败: {error}", component="condensation") from error
            self._entries.append(entry)
            self._next_seq = seq + 1
            self._tail = digest
            return entry

    def read_all(self) -> list[Entry]:
        with self._lock:
            return list(self._entries)

    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def tail_hash(self) -> str:
        with self._lock:
            return self._tail

    def verify(self) -> list[ChainVerifyError]:
        """全链校验：序号连续、内容哈希与前驱指针一致。"""

        with self._lock:
            return verify_entries(self._read_all_locked())

    def segments(self) -> list[str]:
        found: list[str] = []
        for index in range(65536):
            path = self._segment_path(index)
            if not os.path.isfile(path):
                break
            found.append(path)
        return found

    def _read_all_locked(self) -> list[Entry]:
        entries: list[Entry] = []
        for index in range(65536):
            path = self._segment_path(index)
            if not os.path.isfile(path):
                break
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip():
                            entries.append(Entry.from_dict(json.loads(line)))
            except (OSError, json.JSONDecodeError) as error:
                raise StorageError(f"读取凝露链 {path} 失败: {error}", component="condensation") from error
        return entries


def verify_entries(entries: list[Entry]) -> list[ChainVerifyError]:
    """对一组链条目做全量校验。"""

    errors: list[ChainVerifyError] = []
    prev_hash = GENESIS_HASH
    previous_seq = -1
    for entry in entries:
        if entry.seq != previous_seq + 1:
            errors.append(
                ChainVerifyError(
                    entry.seq,
                    f"序号不连续：期望 {previous_seq + 1}，实际 {entry.seq}（可能有删除/缺段）",
                )
            )
        if entry.prev_hash != prev_hash:
            errors.append(
                ChainVerifyError(entry.seq, "前驱哈希不匹配（链断裂或被改写）",
                                 expected=prev_hash, actual=entry.prev_hash)
            )
        digest = canonical_digest(
            entry.seq, entry.ts, entry.kind, entry.psd_id, entry.payload, entry.prev_hash
        )
        if digest != entry.hash:
            errors.append(
                ChainVerifyError(entry.seq, "内容哈希不匹配（条目被篡改）",
                                 expected=digest, actual=entry.hash)
            )
        previous_seq = entry.seq
        prev_hash = entry.hash
    return errors


def verify_chain(journal: ChainJournal) -> list[ChainVerifyError]:
    """模块级校验入口。"""

    return journal.verify()
