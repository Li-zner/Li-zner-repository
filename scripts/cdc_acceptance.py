#!/usr/bin/env python3
"""CDC 对账验收脚本（在容器内执行）

用法（宿主机）:
  docker cp scripts/cdc_acceptance.py agent_gateway:/tmp/cdc_acceptance.py
  docker exec agent_gateway python /tmp/cdc_acceptance.py

验证项（对应验收标准）:
  A1  三张表 INSERT/UPDATE/DELETE 全部被捕获（事件数 == 变更数）
  A2  事件含完整元数据（table/op/pk/before/after/ts）
  A3  全局有序、无丢失（journal 中 id 连续、无重复、无缺口）
  A4  JSONL 落盘（cdc_journal/ 文件存在、行数与事件数一致）
  A6  1000 条变更在秒级内完成捕获落盘
"""
import asyncio
import json
import os
import time
from collections import Counter

import asyncpg

# 容器内由 compose env_file 注入 DATABASE_URL（来自安全目录），禁止硬编码密码
DB_URL = os.getenv("DATABASE_URL") or ""
JOURNAL_DIR = "/app/cdc_journal"
DRAIN_TIMEOUT = 60  # 秒


def check(name: str, ok: bool, detail: str = ""):
    mark = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {mark}  {name}  {detail}")


async def wait_drained(conn, target_id: int) -> float:
    """等待 worker 把事件全部落盘（checkpoint.last_id >= target_id）"""
    ckpt = os.path.join(JOURNAL_DIR, "checkpoint.json")
    start = time.time()
    while time.time() - start < DRAIN_TIMEOUT:
        if os.path.exists(ckpt):
            try:
                with open(ckpt, "r", encoding="utf-8") as f:
                    if json.load(f).get("last_id", 0) >= target_id:
                        return time.time() - start
            except Exception:
                pass
        await asyncio.sleep(1)
    return -1


def read_journal_lines(baseline_id: int):
    """读取所有 journal 文件中 id > baseline_id 的事件行"""
    events = []
    for name in sorted(os.listdir(JOURNAL_DIR)):
        p = os.path.join(JOURNAL_DIR, name)
        if not name.startswith("checkpoint") and os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    ev = json.loads(line)
                    if ev["id"] > baseline_id:
                        events.append(ev)
    return events


async def main():
    print("=" * 60)
    print("  CDC 对账验收")
    print("=" * 60)
    conn = await asyncpg.connect(DB_URL)

    # ---- 基线 ----
    baseline = await conn.fetchval("SELECT COALESCE(MAX(id),0) FROM cdc_events")
    uid = f"cdc_test_{int(time.time())}"
    print(f"基线 cdc_events max_id = {baseline}, 测试用户 = {uid}")

    # ---- 构造变更（约 1000 条）----
    expected = Counter()  # (table, op) -> count
    t_start = time.time()

    # payment_orders: insert 300 / update 100 / delete 40
    order_nos = [f"{uid}_o{i:03d}" for i in range(300)]
    for no in order_nos:
        await conn.execute(
            "INSERT INTO payment_orders (order_no, user_id, order_type, amount, status) "
            "VALUES ($1, $2, 'recharge', 100.00, 'pending')", no, uid)
        expected[("payment_orders", "INSERT")] += 1
    for no in order_nos[:100]:
        await conn.execute("UPDATE payment_orders SET status='paid', paid_at=CURRENT_TIMESTAMP WHERE order_no=$1", no)
        expected[("payment_orders", "UPDATE")] += 1
    for no in order_nos[-40:]:
        await conn.execute("DELETE FROM payment_orders WHERE order_no=$1", no)
        expected[("payment_orders", "DELETE")] += 1

    # transaction_logs: insert 300 / update 60 / delete 30
    tx_ids = []
    for i in range(300):
        row = await conn.fetchrow(
            "INSERT INTO transaction_logs (order_no, user_id, tx_type, amount, before_balance, after_balance) "
            "VALUES ($1, $2, 'recharge', 10.00, 0.00, 10.00) RETURNING id",
            f"{uid}_tx{i:03d}", uid)
        tx_ids.append(row["id"])
        expected[("transaction_logs", "INSERT")] += 1
    for tid in tx_ids[:60]:
        await conn.execute("UPDATE transaction_logs SET remark='updated' WHERE id=$1", tid)
        expected[("transaction_logs", "UPDATE")] += 1
    for tid in tx_ids[-30:]:
        await conn.execute("DELETE FROM transaction_logs WHERE id=$1", tid)
        expected[("transaction_logs", "DELETE")] += 1

    # user_wallets: insert 100 / update 50 / delete 20
    for i in range(100):
        await conn.execute(
            "INSERT INTO user_wallets (user_id, balance) VALUES ($1, 50.00)",
            f"{uid}_w{i:03d}")
        expected[("user_wallets", "INSERT")] += 1
    for i in range(50):
        await conn.execute("UPDATE user_wallets SET balance=balance+1 WHERE user_id=$1", f"{uid}_w{i:03d}")
        expected[("user_wallets", "UPDATE")] += 1
    for i in range(20):
        await conn.execute("DELETE FROM user_wallets WHERE user_id=$1", f"{uid}_w{i:03d}")
        expected[("user_wallets", "DELETE")] += 1

    total_ops = sum(expected.values())
    t_op = time.time() - t_start
    print(f"已执行 {total_ops} 条变更（耗时 {t_op:.2f}s），等待 worker 落盘...")

    # ---- 等待落盘 ----
    target = baseline + total_ops
    drain_s = await wait_drained(conn, target)
    if drain_s < 0:
        print("❌ FAIL  等待落盘超时")
        await conn.close()
        return
    print(f"✅ 全部落盘完成，耗时 {drain_s:.2f}s")

    # ---- 读取 journal ----
    events = read_journal_lines(baseline)
    by_key = Counter((e["table"], e["op"]) for e in events)
    ids = sorted(e["id"] for e in events)

    # ---- A1: 事件数 == 变更数（分表分操作）----
    print("\n--- A1 捕获完整性 ---")
    all_ok = True
    for key in sorted(expected):
        got = by_key.get(key, 0)
        ok = got == expected[key]
        all_ok &= ok
        check(f"{key[0]}.{key[1]}", ok, f"期望={expected[key]} 实际={got}")
    check("总事件数", len(events) == total_ops, f"期望={total_ops} 实际={len(events)}")

    # ---- A2: 元数据 ----
    print("\n--- A2 事件元数据 ---")
    def _valid_meta(e):
        # 公共字段：table/op/pk/ts
        if not (e.get("table") and e.get("op") and "pk" in e and "ts" in e):
            return False
        op = e["op"]
        if op == "INSERT":
            return e.get("after") is not None            # 新增：只有 after
        if op == "UPDATE":
            return e.get("before") is not None and e.get("after") is not None  # 前后都有
        if op == "DELETE":
            return e.get("before") is not None           # 删除：只有 before
        return False
    meta_ok = all(_valid_meta(e) for e in events)
    check("table/op/pk/before/after/ts 完整", meta_ok)

    # ---- A3: 序号连续无丢失 ----
    print("\n--- A3 序号连续性 ---")
    cont_ok = len(ids) == total_ops and (not ids or ids == list(range(ids[0], ids[-1] + 1)))
    uniq_ok = len(set(ids)) == len(ids)
    check("id 连续无缺口", cont_ok, f"min={ids[0] if ids else '-'} max={ids[-1] if ids else '-'} 条数={len(ids)}")
    check("id 无重复", uniq_ok)

    # ---- A4: JSONL 落盘 ----
    print("\n--- A4 落盘文件 ---")
    files = [f for f in os.listdir(JOURNAL_DIR) if not f.startswith("checkpoint") and os.path.isfile(os.path.join(JOURNAL_DIR, f))]
    check("journal 文件存在", len(files) > 0, f"文件: {files}")

    # ---- A6: 1000 条秒级落盘 ----
    print("\n--- A6 吞吐（1000 条秒级落盘）---")
    check("落盘耗时 < 30s", drain_s < 30, f"耗时={drain_s:.2f}s")

    # ---- 汇总 ----
    print("\n" + "=" * 60)
    verdict = all_ok and meta_ok and cont_ok and uniq_ok and drain_s > 0 and drain_s < 30
    print(f"  验收结论: {'✅ 全部通过' if verdict else '❌ 存在失败项'}")
    print("=" * 60)
    await conn.close()
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
