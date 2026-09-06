#!/usr/bin/env python3
"""慢查询监控（pg_stat_statements）

用法：
  python scripts/slow_query_monitor.py --check [--threshold 1000] [--top 10]
      # 单次检查：发现慢查询打印并 exit 1（供 cron/告警）
  python scripts/slow_query_monitor.py --loop --interval 60
      # 周期循环记录（后台常驻，日志由调用方重定向）

前置：
  - postgres 已加载 shared_preload_libraries=pg_stat_statements（见
    scripts/enable_pg_stat_statements.sh），且已 CREATE EXTENSION
  - 通过 `docker exec postgres psql` 走本地 unix socket（trust），无密码
"""
import argparse
import subprocess
import sys
import time

_QUERY = """
SELECT query, calls, mean_exec_time, max_exec_time, rows
FROM pg_stat_statements
WHERE mean_exec_time > {thr}
ORDER BY mean_exec_time DESC
LIMIT {top}
"""


def _psql(sql: str) -> str:
    """在 postgres 容器内执行 SQL（本地 socket，trust 认证）"""
    r = subprocess.run(
        ["docker", "exec", "postgres", "psql",
         "-U", "agent_user", "-d", "agent_gateway",
         "-t", "-A", "-F", "|", "-c", sql],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        print(f"[slow_query] psql 失败: {r.stderr.strip()}", file=sys.stderr)
        return ""
    return r.stdout


def check(threshold_ms: int, top: int) -> int:
    """执行一次检查；返回 1 表示发现慢查询（供告警退出码）"""
    out = _psql(_QUERY.format(thr=threshold_ms, top=top))
    found = 0
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        q, calls, mean, mx, rows = parts[0], parts[1], parts[2], parts[3], parts[4]
        try:
            mean_ms = float(mean)
            max_ms = float(mx)
        except ValueError:
            continue
        found += 1
        print(f"[slow_query] mean={mean_ms:.0f}ms max={max_ms:.0f}ms "
              f"calls={calls} rows={rows} :: {q[:120]}")
    if found == 0:
        print(f"[slow_query] OK: 无 mean>{threshold_ms}ms 的慢查询（检查前 {top} 名）")
    return 1 if found else 0


def main():
    ap = argparse.ArgumentParser(description="pg_stat_statements 慢查询监控")
    ap.add_argument("--check", action="store_true", help="单次检查（默认行为，显式声明更清晰）")
    ap.add_argument("--threshold", type=int, default=1000, help="慢查询阈值（毫秒，默认 1000）")
    ap.add_argument("--top", type=int, default=10, help="最多展示条数")
    ap.add_argument("--loop", action="store_true", help="周期循环（配合 --interval）")
    ap.add_argument("--interval", type=int, default=60, help="循环间隔秒数")
    args = ap.parse_args()

    if args.loop:
        while True:
            try:
                check(args.threshold, args.top)
            except Exception as e:  # noqa: BLE001 - 监控脚本不因单次异常退出
                print(f"[slow_query] 检查异常: {e}", file=sys.stderr)
            time.sleep(args.interval)
    else:
        sys.exit(check(args.threshold, args.top))


if __name__ == "__main__":
    main()
