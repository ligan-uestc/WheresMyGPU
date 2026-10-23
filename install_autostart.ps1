<#
.SYNOPSIS
    为 WheresMyGPU 注册 / 卸载「登录时自动启动」的计划任务。

.DESCRIPTION
    注册后，每次登录 Windows 都会在后台静默启动 WheresMyGPU，日志写入 logs\。
    建议以管理员身份运行，这样任务在任何用户登录时都可用。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1
    注册并立即启动

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1 -Remove
    卸载计划任务
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    [string]$TaskName = "WheresMyGPU",
    [string]$ConfigPath,
    [int]$IntervalSeconds = 0,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ConfigPath) { $ConfigPath = Join-Path $baseDir "config.yaml" }

function Write-Step($msg) { Write-Host "  $msg" }
function Write-Ok($msg)   { Write-Host "  $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "  $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "=============================================================="
Write-Host "  WheresMyGPU - 开机自启配置"
Write-Host "=============================================================="
Write-Host ""

# ---------------------------------------------------------------- 卸载
if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Warn2 "没有找到名为 '$TaskName' 的计划任务，无需卸载。"
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Ok "已卸载计划任务 '$TaskName'。"
    exit 0
}

# ---------------------------------------------------------------- 前置检查
if (-not (Test-Path $ConfigPath)) {
    Write-Err "找不到配置文件: $ConfigPath"
    Write-Step "请先运行:  python -m wheresmygpu --init"
    exit 1
}
Write-Ok "配置文件: $ConfigPath"

$pyExe = $null
$candidates = @(
    "D:\anaconda_root\python.exe",
    (Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
) | Where-Object { $_ -and (Test-Path $_) }

if ($candidates.Count -gt 0) { $pyExe = $candidates[0] }
if (-not $pyExe) {
    Write-Err "找不到 Python 解释器。"
    exit 1
}
Write-Ok "Python: $pyExe"
Write-Ok "工作目录: $baseDir"

# ---------------------------------------------------------------- 构造动作
$argList = @("-m", "wheresmygpu", "--config", "`"$ConfigPath`"")
if ($IntervalSeconds -gt 0) { $argList += @("--interval", "$IntervalSeconds") }

$action = New-ScheduledTaskAction `
    -Execute $pyExe `
    -Argument ($argList -join " ") `
    -WorkingDirectory $baseDir

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# ---------------------------------------------------------------- 注册
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Step "已存在同名任务，先移除旧的…"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "登录时自动启动 WheresMyGPU，持续监控 GPU 空闲并提醒。" | Out-Null

Write-Ok "已注册计划任务 '$TaskName'（登录时自动启动）"

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-Ok "已启动，当前状态: $state"
}

Write-Host ""
Write-Host "--------------------------------------------------------------"
Write-Host "  常用操作"
Write-Host "--------------------------------------------------------------"
Write-Step "查看状态:  Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Step "手动启动:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Step "停止:      Stop-ScheduledTask  -TaskName '$TaskName'"
Write-Step "卸载:      powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1 -Remove"
Write-Step "查看日志:  Get-Content '$baseDir\logs\wheresmygpu-*.log' -Tail 50 -Wait"
Write-Host ""
Write-Warn2 "提示：请先确认 config.yaml 已填好，并用 --check 验证过能连上服务器。"
Write-Host ""