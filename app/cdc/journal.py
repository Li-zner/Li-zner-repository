"""CDC 变更日志落盘：JSONL 追加写 + 按日/按大小滚动 + 断点续传 checkpoint

落盘目录默认 cdc_journal/（docker-compose 挂载到宿主机 SSD），
写入使用 asyncio.to_thread 避免阻塞事件循环；每条 flush，
每 100 条 fsync 一次保证持久化（兼顾吞吐与耐用性）。
"""
import asyncio
import json
import os
import threading
from datetime import datetime

from ..core.logging import setup_logging

logger = setup_logging()

_FSYNC_EVERY = 100  # 每 N 条 fsync 一次


class CdcJournal:
    """按天追加 JSONL 的变更日志仓库"""

    def __init__(self, journal_dir: str):
        self.journal_dir = journal_dir
        self.checkpoint_path = os.path.join(journal_dir, "checkpoint.json")
        self._fh = None
        self._current_basename = None
        self._max_size = 50 * 1024 * 1024  # 单文件 50MB 后滚动
        self._since_fsync = 0
        self._write_lock = threading.Lock()  # P1 #7：防 to_thread 并发写交错
        self.last_id = self.load_checkpoint()  # 启动时恢复断点

    def _ensure_dir(self):
        os.makedirs(self.journal_dir, exist_ok=True)

    def _day_basename(self):
        return datetime.now().strftime("%Y%m%d") + ".jsonl"

    def _day_path(self):
        return os.path.join(self.journal_dir, self._day_basename())

    async def append(self, event: dict, event_id: int):
        """异步追加一条事件（线程池写文件，不阻塞事件循环）"""
        line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
        await asyncio.to_thread(self._write_line, line)
        self.last_id = event_id

    def _write_line(self, line: str):
        with self._write_lock:  # P1 #7：防 to_thread 并发写交错
            self._ensure_dir()
            basename = self._day_basename()
            path = os.path.join(self.journal_dir, basename)
            # 跨天/首次：先开新文件再关旧（P0 #19 原子切换，失败时旧句柄仍有效）
            if self._fh is None or self._current_basename != basename:
                new_fh = open(path, "a", encoding="utf-8")
                old_fh = self._fh
                self._fh = new_fh
                if old_fh:
                    old_fh.close()
                self._current_basename = basename
            # 单文件超限 -> 滚动（原子：先开新再关旧）
            if self._fh.tell() > self._max_size:
                seq = 1
                while seq < 1000:  # P2 #14：限制最大滚动序号，防无限增长
                    alt = os.path.join(self.journal_dir, f"{basename}.{seq}")
                    if not os.path.exists(alt):
                        break
                    seq += 1
                new_fh = open(alt, "a", encoding="utf-8")
                old_fh = self._fh
                self._fh = new_fh
                old_fh.close()
                self._current_basename = f"{basename}.{seq}"
            self._fh.write(line)
            # P1 #8：移除每条 flush，fsync 前统一 flush（fsync 每 100 条一次）
            self._since_fsync += 1
            if self._since_fsync >= _FSYNC_EVERY:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._since_fsync = 0

    async def save_checkpoint(self, last_id: int):
        """原子写 checkpoint（临时文件 + os.replace；记录当前 journal 文件名，P2 #18）"""
        self.last_id = last_id
        data = {
            "last_id": last_id,
            "journal": self._current_basename or self._day_basename(),
            "updated_at": datetime.now().isoformat(),
        }
        await asyncio.to_thread(self._write_checkpoint, data)

    def _write_checkpoint(self, data: dict):
        self._ensure_dir()
        tmp = self.checkpoint_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.checkpoint_path)

    def load_checkpoint(self) -> int:
        """读取上次处理到的最大事件 id（断点续传）"""
        if os.path.exists(self.checkpoint_path):
            try:
                with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                    return int(json.load(f).get("last_id", 0))
            except Exception as e:
                logger.warning(f"checkpoint 读取失败，从 0 开始: {e}")
        return 0

    def list_files(self):
        """列出落盘的日志文件信息（供 API 查看）"""
        self._ensure_dir()
        out = []
        for name in sorted(os.listdir(self.journal_dir)):
            p = os.path.join(self.journal_dir, name)
            if os.path.isfile(p) and not name.startswith("checkpoint"):
                out.append({"name": name, "size": os.path.getsize(p)})
        return out

    def close(self):
        if self._fh:
            try:
                os.fsync(self._fh.fileno())
            except Exception:
                pass
            self._fh.close()
            self._fh = None
