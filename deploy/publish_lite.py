# -*- coding: utf-8 -*-
"""agent_gateway lite 云端发布（单实例非滚动）。

用法: python gw_publish.py <本机镜像tar.gz> <远端目标tar>
流程: SSH 连接(读 deploy/.server.env 凭据) → SFTP 上传镜像 → docker load
      → docker compose up -d gateway(仅重建网关,pg/redis/nginx 不动)
      → /health 健康检查 → 容器内核验新代码 → 清理远端临时文件。
注意: 全程单条 SSH 连接,避免 fail2ban;密码不落脚本,只从 env 文件读。
"""
import os
import sys
import time

import paramiko

HOST = USER = PASSWORD = None
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "..", "..", "桌面", "agent_gateway", "deploy", ".server.env")
# 兜底:脚本被复制时也支持直接传第4参
if len(sys.argv) > 4:
    _, HOST = sys.argv[0], sys.argv[4]
with open(os.path.expanduser(r"D:\桌面\agent_gateway\deploy\.server.env",
                             ), encoding="utf-8") as f:
    for line in f:
        k, _, v = line.strip().partition("=")
        if not _:
            continue
        if k == "SERVER_HOST":
            HOST = v
        elif k == "SERVER_USER":
            USER = v
        elif k == "SERVER_PASSWORD":
            PASSWORD = v
if not (HOST and USER and PASSWORD):
    sys.exit("FATAL: 凭据不完整,检查 deploy/.server.env")

LOCAL_TGZ = r"C:\Users\PC\AppData\Local\Temp\gw_lite_0815af2.tar.gz"
REMOTE_TGZ = "/root/gw_lite_0815af2.tar.gz"
REPO = "/root/agent-gateway-full"
assert len(sys.argv) >= 1  # 路径已硬编码,不再依赖 argv(后台包装器传参不可靠)


def run(ssh, cmd, timeout=180):
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace").strip()
    err = stderr.read().decode("utf-8", "replace").strip()
    code = stdout.channel.recv_exit_status()
    return code, out, err


print(f"[1/6] 连接 {USER}@{HOST} ...", flush=True)
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20,
            banner_timeout=20, auth_timeout=20)

print(f"[2/6] SFTP 上传镜像 local={LOCAL_TGZ!r} remote={REMOTE_TGZ!r} "
      f"exists={os.path.exists(LOCAL_TGZ)}", flush=True)
sftp = ssh.open_sftp()
_t0 = time.time()
_sofar = [0]
sftp.put(LOCAL_TGZ, REMOTE_TGZ,
         callback=lambda sent, total: (_sofar.append(sent),
         (print(f"    上传 {sent * 100 // total}% ({sent >> 20}MB) "
                f"{time.time() - _t0:.0f}s", flush=True)
          if sent - _sofar[-2] > (32 << 20) else None)))
print(f"    上传完成 {time.time() - _t0:.0f}s", flush=True)

print("[3/6] docker load ...", flush=True)
code, out, err = run(ssh, f"docker load -i {REMOTE_TGZ}", timeout=300)
print(out or err, flush=True)
if code != 0:
    sys.exit(f"FATAL: docker load 失败 exit={code}: {err}")

print("[4/6] 重建网关容器(pg/redis/nginx 不动) ...", flush=True)
code, out, err = run(
    ssh,
    f"cd {REPO} && (docker compose -f deploy/docker-compose.lite.yml up -d gateway "
    f"|| docker-compose -f deploy/docker-compose.lite.yml up -d gateway)",
    timeout=180)
print(out or err, flush=True)
if code != 0:
    sys.exit(f"FATAL: compose up 失败 exit={code}: {err}")

print("[5/6] 健康检查(/health,最多 60s) ...", flush=True)
ok = False
for i in range(12):
    code, out, err = run(ssh, "curl -sf -m 4 http://localhost:10090/health")
    if code == 0:
        print(f"    OK({i + 1}次): {out}", flush=True)
        ok = True
        break
    time.sleep(5)
if not ok:
    print("    WARN: /health 未通过,docker logs:", flush=True)
    _, out, _ = run(ssh, "docker logs --tail 30 agent_gateway 2>&1")
    print(out, flush=True)

print("[6/6] 容器内核验新代码 + 清理 ...", flush=True)
for probe in ("ls app/core/place_extract.py app/payment/deferred.py",
              "grep -c 'RECALL_LIMIT' app/agents/tools.py"):
    code, out, err = run(ssh, f"docker exec agent_gateway sh -c '{probe}'")
    print(f"    [{'OK' if code == 0 else 'FAIL'}] {probe} -> {out or err}",
          flush=True)
run(ssh, f"rm -f {REMOTE_TGZ}")
_, out, _ = run(ssh, "docker ps --format '{{.Names}} {{.Image}} {{.Status}}' "
                    "| head -8")
print(out, flush=True)
ssh.close()
print("PUBLISH_DONE", flush=True)
