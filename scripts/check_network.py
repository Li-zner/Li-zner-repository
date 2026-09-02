import subprocess, json

# Check Redis networks
result = subprocess.run(
    "docker inspect docker-redis-1 --format '{{json .NetworkSettings.Networks}}'",
    shell=True, capture_output=True, text=True
)
nets = json.loads(result.stdout.strip())
print("Redis networks:")
for k, v in nets.items():
    print(f"  {k}: ID={v['NetworkID'][:12]}")

# Check gateway_net
result = subprocess.run(
    "docker network inspect agent_gateway_gateway_net --format '{{json .Containers}}'",
    shell=True, capture_output=True, text=True
)
try:
    containers = json.loads(result.stdout.strip())
    print(f"\ngateway_net containers ({len(containers)}):")
    for cid, c in containers.items():
        print(f"  {c['Name']}")
except:
    print(f"\ngateway_net: {result.stdout[:200]}")

# Check docker_default
result = subprocess.run(
    "docker network inspect docker_default --format '{{json .Containers}}'",
    shell=True, capture_output=True, text=True
)
try:
    containers = json.loads(result.stdout.strip())
    print(f"\ndocker_default containers ({len(containers)}):")
    for cid, c in containers.items():
        print(f"  {c['Name']}")
except:
    print(f"\ndocker_default: {result.stdout[:200]}")
