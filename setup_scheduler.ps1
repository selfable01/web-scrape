# setup_scheduler.ps1
# Creates a Windows Task Scheduler task to run scraper.py daily at 11:00 AM.
#
# Run this from an ELEVATED (Administrator) PowerShell:
#   powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1

$TaskName   = "MomoPriceScraper"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$BatPath    = Join-Path $ScriptDir "run_scraper.bat"
$WorkingDir = $ScriptDir
$StartTime  = "11:00"

# Remove existing task if it already exists
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task '$TaskName'."
}

# Trigger: daily at 11:00 AM, no expiration
$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime

# Action: run the batch file
$action = New-ScheduledTaskAction `
    -Execute $BatPath `
    -WorkingDirectory $WorkingDir

# Settings: allow run on battery, wake to run, start if missed
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# Register the task (runs as current user, only when logged on)
Register-ScheduledTask `
    -TaskName $TaskName `
    -Trigger $trigger `
    -Action $action `
    -Settings $settings `
    -Description "Scrape momo HUEI YEH prices daily at 11:00 AM" `
    -RunLevel Highest

Write-Host ""
Write-Host "Task '$TaskName' created successfully."
Write-Host "  Schedule    : Daily at $StartTime"
Write-Host "  Wake PC     : Yes"
Write-Host "  Start missed: Yes (runs ASAP if PC was off at 11 AM)"
Write-Host "  Action      : $BatPath"
Write-Host ""
Write-Host "To verify: Open Task Scheduler > Task Scheduler Library > $TaskName"
Write-Host "To remove: Unregister-ScheduledTask -TaskName $TaskName"
