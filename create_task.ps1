# Run this script with: powershell -ExecutionPolicy Bypass -File create_task.ps1
# Must be run as Administrator

$taskName = "ICT_Trading_Bot"
$pythonExe = "C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\.venv\Scripts\python.exe"
$scriptPath = "run_live.py"
$workDir = "C:\Users\Jeeva\OneDrive\Documents\Trading_Automation\backtest_engine"

# Check admin
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Elevating to Administrator..." -ForegroundColor Yellow
    Start-Process powershell -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Wait
    exit
}

Write-Host "Creating scheduled task: $taskName" -ForegroundColor Cyan

# Remove existing task
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# Create action
$action = New-ScheduledTaskAction -Execute $pythonExe -Argument $scriptPath -WorkingDirectory $workDir

# Create trigger (Mon-Fri at 12:00 PM)
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "12:00PM"

# Settings
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)

# Register
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force

if ($?) {
    Write-Host "`nSUCCESS! Task '$taskName' created." -ForegroundColor Green
    Write-Host "`nTo run now:   schtasks /run /tn `"$taskName`"" -ForegroundColor White
    Write-Host "To check:     schtasks /query /tn `"$taskName`"" -ForegroundColor White
} else {
    Write-Host "`nFAILED to create task." -ForegroundColor Red
}

Write-Host "`nPress any key to exit..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
