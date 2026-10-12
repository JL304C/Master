# schedule_windows.ps1 — register the 7 Insider agents with Windows Task Scheduler.
#
# Creates 7 scheduled tasks under the \InsiderRoutines\ folder.
# Idempotent: re-running removes existing tasks and recreates them.
# Works on built-in Windows PowerShell 5.1 and PowerShell 7.
#
# Agents log to insider_routines\.state\logs\.

$ErrorActionPreference = "Stop"

$Root   = Split-Path -Parent $PSScriptRoot
$Agents = Join-Path $Root "agents"
$Logs   = Join-Path (Join-Path $Root ".state") "logs"
$Folder = "\InsiderRoutines\"

$Python = $env:PYTHON
if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike "*WindowsApps*") { $Python = $cmd.Source }
}
if (-not $Python) {
    Write-Error "Python not found on PATH. Install Python 3.10+ from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')."
    exit 1
}
& $Python -c "import anthropic, dotenv" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "$Python is missing dependencies. Run: `"$Python`" -m pip install -r `"$Root\requirements.txt`""
    exit 1
}

New-Item -ItemType Directory -Force -Path $Logs | Out-Null

function Register-InsiderTask {
    param([string]$Name, $Trigger)

    $taskName = "Insider-$Name"
    if (Get-ScheduledTask -TaskName $taskName -TaskPath $Folder -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -TaskPath $Folder -Confirm:$false
    }

    $action = New-ScheduledTaskAction `
        -Execute $Python `
        -Argument "`"$Agents\$Name.py`"" `
        -WorkingDirectory $Root

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

    Register-ScheduledTask `
        -TaskName $taskName `
        -TaskPath $Folder `
        -Action $action `
        -Trigger $Trigger `
        -Settings $settings `
        -Description "Insider Routines - $Name" | Out-Null

    Write-Host "  OK   $Folder$taskName"
}

function Repeat-Every([TimeSpan]$Interval) {
    # [TimeSpan]::MaxValue is rejected on Windows 10/11; ten years is "forever".
    New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval $Interval `
        -RepetitionDuration (New-TimeSpan -Days 3650)
}

Write-Host "Registering Insider agents with Task Scheduler (python: $Python)..."

Register-InsiderTask -Name "eddie"  -Trigger (New-ScheduledTaskTrigger -Daily -At 06:00)
Register-InsiderTask -Name "maggie" -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 19:00)
Register-InsiderTask -Name "frank"  -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 08:00)
Register-InsiderTask -Name "maya"   -Trigger (Repeat-Every (New-TimeSpan -Hours 6))
Register-InsiderTask -Name "janet"  -Trigger (New-ScheduledTaskTrigger -Daily -At 17:00)
Register-InsiderTask -Name "sophie" -Trigger (Repeat-Every (New-TimeSpan -Minutes 30))
Register-InsiderTask -Name "ross"   -Trigger (Repeat-Every (New-TimeSpan -Minutes 30))

Write-Host ""
Write-Host "All 7 agents registered. Logs -> $Logs"
Write-Host "Inspect: Get-ScheduledTask -TaskPath '$Folder'"
Write-Host "Uninstall: powershell -ExecutionPolicy Bypass -File `"$Root\install\uninstall_windows.ps1`""
