' 以管理员身份启动记事本编辑 Agent 网关配置 .env 文件
Set UAC = CreateObject("Shell.Application")
UAC.ShellExecute "notepad.exe", "\\wsl.localhost\Ubuntu\etc\agent_gateway\.env", "", "runas", 1
