# -*- coding: utf-8 -*-
"""
session/seq_manager.py — 序列号分配与去重

SeqManager:
  allocate()             → 循环分配 0~255，threading.Lock 保护，多线程安全
  is_duplicate(pp, seq)  → (port_path, seq) 二元组判重，防止网络重传重复处理
  reset_seen(pp)         → 清空某条路径的历史记录（断连/重连时调用）
"""
from __future__ import annotations

import threading
from typing import Dict, Set, Tuple

from protocol.models import PortPath


class SeqManager:
    """
    序列号管理器。

    - ``allocate()``：线程安全循环分配 0~255
    - ``is_duplicate()``：(port_path_key, seq) 二元组去重，窗口为最近 128 个 seq
    """

    _WINDOW = 128   # 去重滑动窗口大小

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_seq: int = 0
        # key: tuple(sorted ports) + has485，value: set of recent seq numbers
        self._seen: Dict[Tuple, Set[int]] = {}

    # ------------------------------------------------------------------
    # 分配
    # ------------------------------------------------------------------

    def allocate(self) -> int:
        """
        线程安全地分配下一个序列号（0~255 循环）。

        多线程同时调用时互斥，保证 seq 不碰撞。
        """
        with self._lock:
            seq = self._next_seq
            self._next_seq = (self._next_seq + 1) & 0xFF
            return seq

    # ------------------------------------------------------------------
    # 去重
    # ------------------------------------------------------------------

    @staticmethod
    def _pp_key(port_path: PortPath) -> tuple:
        return (tuple(port_path.ports), port_path.has485)

    def is_duplicate(self, port_path: PortPath, seq: int) -> bool:
        """
        检查 (port_path, seq) 是否为重复帧。

        - 相同来源路径下相同 seq 在滑动窗口内出现过 → True（重复）
        - 首次出现 → 记录并返回 False

        注意：seq 只有 8 位，窗口设为 128，足以覆盖正常往返延迟场景。
        """
        key = self._pp_key(port_path)
        with self._lock:
            seen_set = self._seen.setdefault(key, set())
            if seq in seen_set:
                return True
            seen_set.add(seq)
            # 滑动窗口：超过窗口大小时移除最旧的 seq（近似策略，足够实用）
            if len(seen_set) > self._WINDOW:
                # 移除值最小的（seq 循环分配，绝大多数情况下最小值即最旧）
                seen_set.discard(min(seen_set))
            return False

    def reset_seen(self, port_path: PortPath) -> None:
        """清空某条路径的去重历史（断连/重连时调用）。"""
        key = self._pp_key(port_path)
        with self._lock:
            self._seen.pop(key, None)

    def reset_all(self) -> None:
        """清空全部去重历史。"""
        with self._lock:
            self._seen.clear()
