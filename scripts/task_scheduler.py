"""
定时任务调度器 — 类闹钟，后台静默执行（零依赖，标准库实现）

功能：
  - 任务：名称 / 周期（daily / weekly / monthly）/ 时刻 / 所在文件夹 / 读写权限声明 / 执行命令
  - 调度：守护循环每分钟检查，到点后台执行（CREATE_NO_WINDOW，不打扰）
  - 日志：每次执行输出到 logs/tasks/<name>_<时间戳>.log
  - 权限：任务声明可读写目录；执行前校验脚本路径必须位于任务文件夹内

用法：
  python scripts/task_scheduler.py              启动调度守护（后台运行）
  python scripts/task_scheduler.py --add        交互创建任务
  python scripts/task_scheduler.py --list       列出任务与下次执行时间
  python scripts/task_scheduler.py --remove 名称 删除任务
  python scripts/task_scheduler.py --run-now 名称 立即执行一次（测试用）

任务存储：scripts/tasks.json（手工编辑亦可，格式见文件头注释）
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS_FILE = os.path.join(BASE_DIR, "scripts", "tasks.json")
LOG_DIR = os.path.join(BASE_DIR, "logs", "tasks")

# Windows 后台执行：不弹控制台窗口
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ============================================================
# 任务配置读写
# ============================================================
def load_tasks() -> list:
    if not os.path.exists(TASKS_FILE):
        return []
    with open(TASKS_FILE, "r", encoding="utf-8-sig") as f:  # utf-8-sig 容错 BOM（踩坑总汇 #8）
        return json.load(f)


def save_tasks(tasks: list):
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


# ============================================================
# 周期计算：下次执行时间
# ============================================================
def parse_time(hhmm: str) -> tuple:
    h, m = hhmm.strip().split(":")
    return int(h), int(m)


def next_run(task: dict, now: datetime = None) -> datetime:
    """计算任务下次执行时间（现在之后最近的一次）"""
    now = now or datetime.now()
    h, m = parse_time(task["schedule"]["time"])
    sched = task["schedule"]
    stype = sched["type"]

    if stype == "daily":
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return candidate if candidate > now else candidate + timedelta(days=1)

    if stype == "weekly":
        weekday = sched["weekday"]  # 0=周一 ... 6=周日
        days_ahead = (weekday - now.weekday()) % 7
        candidate = (now + timedelta(days=days_ahead)).replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    if stype == "monthly":
        day = sched["day"]  # 1-31；超过当月天数取当月最后一天
        import calendar
        last_day = calendar.monthrange(now.year, now.month)[1]
        target_day = min(day, last_day)
        candidate = now.replace(day=target_day, hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            # 下个月
            if now.month == 12:
                ny, nm = now.year + 1, 1
            else:
                ny, nm = now.year, now.month + 1
            last_day = calendar.monthrange(ny, nm)[1]
            candidate = datetime(ny, nm, min(day, last_day), h, m)
        return candidate

    raise ValueError(f"未知周期类型: {stype}")


# ============================================================
# 权限校验与执行
# ============================================================
def check_permissions(task: dict) -> str:
    """校验任务配置：目录存在 + 脚本路径在任务文件夹内。返回错误信息或空串"""
    folder = task.get("folder", ".")
    folder_abs = os.path.normpath(os.path.join(BASE_DIR, folder))
    if not os.path.isdir(folder_abs):
        return f"任务文件夹不存在: {folder}"

    command = task.get("command", [])
    if not command:
        return "command 为空"
    # 校验命令中的脚本/程序路径必须位于任务文件夹内（防越权执行）
    target = command[0]
    if target.endswith(".py") or target.endswith(".sh") or "/" in target or "\\" in target:
        target_abs = os.path.normpath(os.path.join(folder_abs, target))
        if not target_abs.startswith(folder_abs):
            return f"命令目标越权: {target}（必须在任务文件夹 {folder} 内）"
        if not os.path.exists(target_abs):
            return f"命令目标不存在: {target}"
    return ""


def run_task(task: dict) -> str:
    """后台执行任务（不打扰），返回日志文件路径"""
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, f"{task['name']}_{ts}.log")
    folder_abs = os.path.normpath(os.path.join(BASE_DIR, task.get("folder", ".")))
    command = task.get("command", [])

    with open(log_path, "w", encoding="utf-8") as logf:
        logf.write(f"[{datetime.now().isoformat(timespec='seconds')}] 任务启动: {task['name']}\n")
        logf.write(f"命令: {' '.join(command)}\n工作目录: {folder_abs}\n")
        logf.write("权限声明: 读=" + ",".join(task.get("permissions", {}).get("read", []))
                   + " 写=" + ",".join(task.get("permissions", {}).get("write", [])) + "\n")
        logf.write("-" * 60 + "\n")
        logf.flush()
        try:
            proc = subprocess.Popen(
                command, cwd=folder_abs,
                stdout=logf, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
            # 等待完成并把退出码写入日志（子进程独立运行，此处等待是为了记录结果）
            proc.wait(timeout=3600)
            logf.write("-" * 60 + f"\n[完成] 退出码: {proc.returncode}\n")
        except Exception as e:
            logf.write(f"[失败] {e}\n")
    return log_path


# ============================================================
# CLI
# ============================================================
def cmd_add():
    print("== 创建定时任务 ==")
    name = input("任务名称: ").strip()
    stype = input("周期 (daily/weekly/monthly) [daily]: ").strip() or "daily"
    hhmm = input("执行时刻 (HH:MM) [08:00]: ").strip() or "08:00"
    folder = input("任务所在文件夹 (相对项目根，如 scripts) [scripts]: ").strip() or "scripts"
    command = input("执行命令 (如: python backup_db.sh 或 python xxx.py) [python]: ").strip()
    if not command:
        print("命令不能为空")
        return

    schedule = {"type": stype, "time": hhmm}
    if stype == "weekly":
        wd = input("星期几 (0=周一 ... 6=周日) [0]: ").strip() or "0"
        schedule["weekday"] = int(wd)
    elif stype == "monthly":
        day = input("几号执行 (1-31) [1]: ").strip() or "1"
        schedule["day"] = int(day)

    read_dirs = input("可读目录 (逗号分隔，如 docs,scripts) []: ").strip()
    write_dirs = input("可写目录 (逗号分隔，如 backups,logs) []: ").strip()

    task = {
        "name": name,
        "enabled": True,
        "schedule": schedule,
        "folder": folder,
        "command": command.split(),
        "permissions": {
            "read": [d.strip() for d in read_dirs.split(",") if d.strip()],
            "write": [d.strip() for d in write_dirs.split(",") if d.strip()],
        },
    }
    err = check_permissions(task)
    if err:
        print(f"⚠ 配置校验失败: {err}")
        return
    tasks = load_tasks()
    tasks = [t for t in tasks if t["name"] != name]  # 同名覆盖
    tasks.append(task)
    save_tasks(tasks)
    print(f"✅ 任务已创建: {name}，下次执行: {next_run(task).strftime('%Y-%m-%d %H:%M')}")


def cmd_list():
    tasks = load_tasks()
    if not tasks:
        print("（暂无任务）")
        return
    print(f"{'名称':<16}{'周期':<10}{'时刻':<8}{'下次执行':<20}{'文件夹':<12}{'状态'}")
    for t in tasks:
        sched = t["schedule"]
        stype = sched["type"]
        if stype == "daily":
            detail = "每天"
        elif stype == "weekly":
            detail = f"周{sched['weekday']}"
        elif stype == "monthly":
            detail = f"{sched['day']}号"
        else:
            detail = stype
        nr = next_run(t).strftime("%m-%d %H:%M") if t.get("enabled", True) else "-"
        status = "启用" if t.get("enabled", True) else "停用"
        print(f"{t['name']:<16}{detail:<10}{sched['time']:<8}{nr:<20}{t.get('folder',''):<12}{status}")


def cmd_remove(name: str):
    tasks = load_tasks()
    tasks = [t for t in tasks if t["name"] != name]
    save_tasks(tasks)
    print(f"已删除任务: {name}")


def cmd_run_now(name: str):
    tasks = load_tasks()
    task = next((t for t in tasks if t["name"] == name), None)
    if not task:
        print(f"任务不存在: {name}")
        return
    err = check_permissions(task)
    if err:
        print(f"⚠ 校验失败: {err}")
        return
    log_path = run_task(task)
    print(f"✅ 已执行（后台）：{name}，日志: {log_path}")


# ============================================================
# 调度守护循环
# ============================================================
def scheduler_loop():
    """每分钟检查一次，到点后台执行"""
    print(f"调度器已启动，任务文件: {TASKS_FILE}（Ctrl+C 停止）")
    last_check = None
    while True:
        now = datetime.now()
        # 每分钟检查一次（秒归零后）
        if last_check is None or now.minute != last_check.minute:
            last_check = now
            for task in load_tasks():
                if not task.get("enabled", True):
                    continue
                try:
                    nr = next_run(task)
                    # 到点（误差 1 分钟内）→ 执行
                    if (now - nr).total_seconds() < 60 and (now - nr).total_seconds() >= 0:
                        print(f"[{now.strftime('%H:%M:%S')}] 触发任务: {task['name']}")
                        threading.Thread(target=run_task, args=(task,), daemon=True).start()
                except Exception as e:
                    print(f"[{now.strftime('%H:%M:%S')}] 任务 {task['name']} 计算失败: {e}")
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="定时任务调度器")
    parser.add_argument("--add", action="store_true", help="交互创建任务")
    parser.add_argument("--list", action="store_true", help="列出任务")
    parser.add_argument("--remove", metavar="名称", help="删除任务")
    parser.add_argument("--run-now", metavar="名称", help="立即执行一次")
    args = parser.parse_args()

    if args.add:
        cmd_add()
    elif args.list:
        cmd_list()
    elif args.remove:
        cmd_remove(args.remove)
    elif args.run_now:
        cmd_run_now(args.run_now)
    else:
        scheduler_loop()


if __name__ == "__main__":
    main()
