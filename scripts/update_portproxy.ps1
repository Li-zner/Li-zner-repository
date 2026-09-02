<#
.SYNOPSIS
  更新 netsh portproxy 规则，将 Windows 端口转发到 docker-desktop WSL2 VM
  解决 wslrelay 拦截端口导致 500 的问题

.DESCRIPTION
  WSL2 每次重启后 IP 会变化，导致旧的 portproxy 规则失效。
  此脚本自动获取当前 docker-desktop WSL2 VM 的 IP，并更新转发规则。

  使用场景：
    - WSL/Docker 重启后执行一次
    - 可添加到 Windows 任务计划程序开机启动
    - 或放入 WSL 启动脚本

  端口映射表：
    Host Port → WSL2 VM:Container Port
    10091    → WSL2_IP:10092     # gateway（主入口）
    10089    → WSL2_IP:10089     # gateway2（备用）
    10090    → WSL2_IP:10090     # nginx（负载均衡）

  注意：需要以管理员权限运行
#>

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  WSL2 PortProxy 自动更新脚本" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ── 1. 获取 docker-desktop WSL2 VM 的 IP ──
Write-Host "⏳ 获取 docker-desktop WSL2 IP..." -NoNewline

try {
    $wslIp = wsl -d docker-desktop -- ip addr show eth0 2>$null |
        Select-String -Pattern "inet (\d+\.\d+\.\d+\.\d+)" |
        ForEach-Object { $_.Matches.Groups[1].Value } |
        Select-Object -First 1
} catch {
    $wslIp = $null
}

if (-not $wslIp) {
    Write-Host " FAILED" -ForegroundColor Red
    Write-Host "❌ 无法获取 docker-desktop WSL2 IP。请确认 WSL2 和 Docker Desktop 正在运行。" -ForegroundColor Red
    Write-Host ""
    Write-Host "  手动检查命令: wsl -d docker-desktop -- ip addr show eth0" -ForegroundColor Gray
    exit 1
}

Write-Host " $wslIp" -ForegroundColor Green
Write-Host ""

# ── 2. 定义端口映射 ──
# 格式: @{HostPort; ContainerPort; Description}
$portMappings = @(
    @{ Host = "10092"; Container = "10092"; Desc = "gateway（主入口）" }
    @{ Host = "10089"; Container = "10089"; Desc = "gateway2（备用）" }
)

# 可选：如果要转发 nginx，取消下面注释
# $portMappings += @{ Host = "10090"; Container = "10090"; Desc = "nginx（负载均衡）" }

# ── 3. 清除旧的 portproxy 规则（仅清除我们管理的端口）──
Write-Host "⏳ 清除旧规则..." -NoNewline

foreach ($m in $portMappings) {
    $hostPort = $m.Host
    netsh interface portproxy delete v4tov4 listenport=$hostPort 2>$null | Out-Null
}

Write-Host " Done" -ForegroundColor Green

# ── 4. 添加新规则 ──
Write-Host ""
Write-Host "📋 添加端口转发规则:" -ForegroundColor Yellow

foreach ($m in $portMappings) {
    $hostPort = $m.Host
    $containerPort = $m.Container
    $desc = $m.Desc

    try {
        netsh interface portproxy add v4tov4 `
            listenaddress=0.0.0.0 `
            listenport=$hostPort `
            connectaddress=$wslIp `
            connectport=$containerPort 2>&1 | Out-Null

        Write-Host "  ✅ 0.0.0.0:$hostPort → $wslIp`:$containerPort  ($desc)" -ForegroundColor Green
    } catch {
        Write-Host "  ❌ 0.0.0.0:$hostPort → $wslIp`:$containerPort  (失败: $_)" -ForegroundColor Red
    }
}

# ── 5. 验证 ──
Write-Host ""
Write-Host "⏳ 验证规则..." -NoNewline
$rules = netsh interface portproxy show all 2>&1
Write-Host " Done" -ForegroundColor Green
Write-Host ""
Write-Host "当前 portproxy 规则:" -ForegroundColor Yellow
netsh interface portproxy show all

# ── 6. 连通性检查 ──
Write-Host ""
Write-Host "🔍 连通性检查:" -ForegroundColor Yellow

foreach ($m in $portMappings) {
    $hostPort = $m.Host
    $desc = $m.Desc

    try {
        $result = Test-NetConnection -ComputerName "127.0.0.1" -Port $hostPort -WarningAction SilentlyContinue -InformationLevel Quiet 2>$null
        if ($result) {
            Write-Host "  ✅ localhost:$hostPort → 可达  ($desc)" -ForegroundColor Green
        } else {
            Write-Host "  ⚠️  localhost:$hostPort → 不可达  ($desc)" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "  ❌ localhost:$hostPort → 测试失败  ($desc)" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  ✅ 完成！" -ForegroundColor Cyan
Write-Host "  当前 WSL2 IP: $wslIp" -ForegroundColor Cyan
Write-Host "  下次WSL重启后，请重新运行此脚本" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# ── 附：添加到 Windows 任务计划程序（可选）──
Write-Host ""
Write-Host "💡 建议：将此脚本添加到 Windows 任务计划程序，开机时自动运行" -ForegroundColor Gray
Write-Host "   或执行以下命令手动注册任务（需管理员）：" -ForegroundColor Gray
$scriptPath = $MyInvocation.MyCommand.Path
Write-Host "   schtasks /create /tn \"UpdateWSLPortProxy\" /tr \"powershell.exe -File '$PSCommandPath'\" /sc onstart /ru SYSTEM /f" -ForegroundColor Gray
