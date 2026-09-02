@echo off
rem agent_gateway ?????? Windows ???? agent_gateway_backup ???
wsl.exe -d Ubuntu -e bash -c "cd /mnt/d/??/agent_gateway && bash scripts/backup_db.sh"
