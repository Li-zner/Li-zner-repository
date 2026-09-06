"""CDC 分区维护纯逻辑回归测试（2026-09-04）：

worker._maintain_partitions 的 DDL 生成与过期判定是纯函数，这里不依赖数据库：
1. _shift_month 跨年平移
2. _ensure_partition_statements 数量/命名/边界正确且幂等（IF NOT EXISTS）
3. _is_stale_month_partition 只认 cdc_events_YYYY_MM 命名（DEFAULT/恶意名不命中）
"""
import re
from datetime import datetime, timezone

from app.cdc.worker import (
    _ensure_partition_statements,
    _is_stale_month_partition,
    _partition_name,
    _shift_month,
    PARTITION_RETENTION_MONTHS,
    PARTITION_AHEAD_MONTHS,
)

_NAME_IN_DDL = re.compile(r"EXISTS (cdc_events_\d{4}_\d{2}) PARTITION")


def test_shift_month_crosses_year():
    dec = datetime(2025, 12, 1, tzinfo=timezone.utc)
    assert _shift_month(dec, 2).month == 2 and _shift_month(dec, 2).year == 2026
    jan = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _shift_month(jan, -1).month == 12 and _shift_month(jan, -1).year == 2025
    assert _shift_month(jan, -3) == datetime(2025, 10, 1, tzinfo=timezone.utc)


def test_ensure_partition_statements():
    now = datetime(2026, 9, 4, 10, 30, tzinfo=timezone.utc)
    stmts = _ensure_partition_statements(now)

    # [当前-3 .. 当前+2] 共 6 个月分区 + DEFAULT 兜底
    assert len(stmts) == PARTITION_RETENTION_MONTHS + PARTITION_AHEAD_MONTHS + 2
    assert stmts[-1].endswith("PARTITION OF cdc_events DEFAULT")

    # 命名与边界：最旧 = 当前-3 月（2026-06），最新 = 当前+2 月（2026-11）
    names = [_NAME_IN_DDL.search(s).group(1) for s in stmts[:-1]]
    assert names[0] == "cdc_events_2026_06"
    assert names[-1] == "cdc_events_2026_11"

    # 边界必须覆盖整个自然月且幂等
    assert "FOR VALUES FROM ('2026-06-01T00:00:00+00:00') TO ('2026-07-01T00:00:00+00:00')" in stmts[0]
    assert all("IF NOT EXISTS" in s for s in stmts)


def test_partition_name_format():
    assert _partition_name(datetime(2026, 9, 1, tzinfo=timezone.utc)) == "cdc_events_2026_09"


def test_stale_partition_detection():
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)  # stale 阈值 = 2026-06-01

    assert not _is_stale_month_partition("cdc_events_2026_06", now)  # 整月未过保留期（6月正好卡边界）
    assert _is_stale_month_partition("cdc_events_2026_05", now)      # 5月整月已超 3 个月
    assert _is_stale_month_partition("cdc_events_2025_01", now)      # 远期
    assert not _is_stale_month_partition("cdc_events_2026_09", now)  # 当前月
    assert not _is_stale_month_partition("cdc_events_default", now)  # DEFAULT 永不删
    # 恶意/异常名字一律不命中（名字来自 pg_class，落 SQL 前必须过白名单）
    assert not _is_stale_month_partition("cdc_events_2026_05; DROP TABLE cdc_events", now)
    assert not _is_stale_month_partition("cdc_events_20260_5", now)
    assert not _is_stale_month_partition("other_table_2026_05", now)
