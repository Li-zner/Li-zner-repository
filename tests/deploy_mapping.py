"""Deploy law mapping files to Docker container"""
import subprocess, os

docker = r'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
container = 'agent_gateway'

base = r'D:\桌面\agent_gateway'
files = [
    (f'{base}\\app\\agents\\law_mapping.py', '/app/app/agents/law_mapping.py'),
    (f'{base}\\app\\agents\\tools.py', '/app/app/agents/tools.py'),
    (f'{base}\\app\\routes\\v2.py', '/app/app/routes/v2.py'),
]

for src, dst in files:
    r = subprocess.run([docker, 'cp', src, f'{container}:{dst}'], capture_output=True, text=True)
    print(f'Copy {os.path.basename(src)}: {"OK" if r.returncode == 0 else "FAIL - " + r.stderr}')

print('Restarting...')
r = subprocess.run([docker, 'restart', container], capture_output=True, text=True)
print(f'Restart: {"OK" if r.returncode == 0 else r.stderr}')

# Verify
r = subprocess.run([docker, 'exec', container, 'python', '-c',
    'import os; print("law_mapping.py:", os.path.exists("/app/app/agents/law_mapping.py")); print("tools.py:", os.path.exists("/app/app/agents/tools.py")); print("v2.py:", os.path.exists("/app/app/routes/v2.py"))'],
    capture_output=True, text=True)
print(r.stdout.strip())
if r.stderr:
    print('Stderr:', r.stderr.strip())
