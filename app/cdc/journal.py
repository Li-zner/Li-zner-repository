"""CDC 变更日志落盘：JSONL 追加写 + 按日/按大小滚动 + 断点续传 checkpoint

落盘目录默认 cdc_journal/（docker-compose 挂载到宿主机 SSD），
写入使用 asyncio.to_thread 避免阻塞事件循环；每条 flush，
每 100 条 fsync 一次保证持久化（兼顾吞吐与耐用性）。
"""
import asyncio
import json
import os
import threading
from datetime import datetime, timezone

from ..core.logging import setup_logging

logger = setup_logging()

_FSYNC_EVERY = 100  # 每 N 条 fsync 一次


def parse_jsonb(value):
    """把 asyncpg 返回的 JSONB 值规范化为结构化对象。

    asyncpg 默认把 jsonb 列解码成 JSON 文本 str；若直接塞进事件，json.dumps 会双重编码
    （before/after 变成 \"{...}\" 字符串，而非嵌套对象）。此处 str -> dict/list 解析一次，
    已是对象则原样返回；解析失败（如纯文本）则回退为原值（P1 修复双重编码）。
    """
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def format_ts(dt):
    """把数据库时间规范化为 UTC ISO 字符串（末尾带 Z）。

    TIMESTAMPTZ 列返回 aware datetime（可能非 UTC 会话时区），naive 视为 UTC。
    统一成 \"YYYY-MM-DDTHH:MM:SS(.ffffff)Z\"，保证 journal 与 API 的 ts 一致（P1 #11）。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
                # 注意：此处不更新 _current_basename（仍为“当日文件名”）。
                # _current_basename 只用于“跨天/首开”判断；若改成 f"{basename}.{seq}"，
                # 下一笔校验 self._current_basename != basename 恒真，会反复重开超限的当日文件，
                # 导致每条写入都新建一个滚动文件（文件无限增殖）。保留当日名，写入自然落到滚动序号文件。
            self._fh.write(line)
            # P1 #8：移除每条 flush，fsync 前统一 flush（fsync 每 100 条一次）
            self._since_fsync += 1
            if self._since_fsync >= _FSYNC_EVERY:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._since_fsync = 0

    async def save_checkpoint(self, last_id: int):
        """原子写 checkpoint（临时文件 + os.replace；记录当前 journal 文件名，P2 #18）

        单调保护：多实例部署时，非 leader 实例的 last_id 停留在启动时的旧值，
        停机时若直接覆写会把已推进的 checkpoint 回退，导致重启后重放已处理事件（重复）。
        这里先读盘，仅当 last_id 不低于当前值才写入（P1）。
        """
        if last_id < self.load_checkpoint():
            logger.warning(f"checkpoint 回退被忽略: {last_id} < 当前值")
            return
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
                # 保留“从 0 开始”语义（#9 不改），但日志更清晰，便于排障 checkpoint 损坏
                logger.warning(
                    f"checkpoint 读取失败（{e}），将按 last_id=0 重放，可能产生重复事件；"
                    "请检查 checkpoint.json 是否损坏"
                )
        return 0

    def refresh_checkpoint(self) -> int:
        """从磁盘重读 checkpoint 的 last_id 并刷新 self.last_id。

        状态/查看接口（routes.py）每次调用刷新，避免实例在首次创建后 last_id 永久冻结
        （worker 另建实例写 checkpoint，本实例若不重读则永远显示旧值）。
        """
        self.last_id = self.load_checkpoint()
        return self.last_id

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
        # P1：与 _write_line 共用 _write_lock，避免停机时正在线程池中写文件、
        # 而 close() 并发关闭句柄造成的写入报错/文件损坏。
        with self._write_lock:
            if self._fh:
                try:
                    os.fsync(self._fh.fileno())
                except Exception as e:
                    logger.debug(f"journal fsync 失败（尽力而为）: {e}")
                self._fh.close()
                self._fh = None
