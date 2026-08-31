@echo off
"C:\Program Files\Docker\Docker\resources\bin\docker.exe" cp "D:\桌面\agent_gateway\app\agents\law_mapping.py" agent_gateway:/app/app/agents/law_mapping.py
"C:\Program Files\Docker\Docker\resources\bin\docker.exe" cp "D:\桌面\agent_gateway\app\agents\tools.py" agent_gateway:/app/app/agents/tools.py
"C:\Program Files\Docker\Docker\resources\bin\docker.exe" cp "D:\桌面\agent_gateway\app\routes\v2.py" agent_gateway:/app/app/routes/v2.py
"C:\Program Files\Docker\Docker\resources\bin\docker.exe" restart agent_gateway
"C:\Program Files\Docker\Docker\resources\bin\docker.exe" exec agent_gateway ls -la /app/app/agents/law_mapping.py
