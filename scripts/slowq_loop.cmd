@echo off
"D:\×ÀÃæ\agent_gateway\.venv\Scripts\python.exe" "D:\×ÀÃæ\agent_gateway\scripts\slow_query_monitor.py" --loop --interval 300 >> "D:\×ÀÃæ\agent_gateway\slowq.log" 2>&1
