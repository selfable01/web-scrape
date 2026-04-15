# setup_scheduler.ps1
# Creates a Windows Task Scheduler task to run scraper.py daily.
# The task runs whether the user is logged on or not, and wakes the PC from sleep.
#
# Run this from an ELEVATED (Administrator) PowerShell:
#   powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1

$TaskName   = "MomoPriceScraper"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$BatPath    = Join-Path $ScriptDir "run_scraper.bat"
$WorkingDir = $ScriptDir
$StartTime  = "13:58"
$Username   = "$env:USERDOMAIN\$env:USERNAME"

# Remove existing task if it already exists
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task '$TaskName'."
}

# Prompt for password (needed to run whether logged on or not)
$cred = Get-Credential -UserName $Username -Message "Enter your Windows password for the scheduled task"

# Trigger: daily, no expiration
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

# Register the task — runs whether user is logged on or not
Register-ScheduledTask `
    -TaskName $TaskName `
    -Trigger $trigger `
    -Action $action `
    -Settings $settings `
    -User $cred.UserName `
    -Password $cred.GetNetworkCredential().Password `
    -Description "Scrape momo HUEI YEH prices daily at $StartTime" `
    -RunLevel Highest

# Enable wake timers in the active power plan
Write-Host ""
Write-Host "Enabling wake timers in power settings..."
powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP bd3b718a-0680-4d7d-8ab2-e1d2b4ac806d 1
powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP bd3b718a-0680-4d7d-8ab2-e1d2b4ac806d 1
powercfg /SETACTIVE SCHEME_CURRENT

Write-Host ""
Write-Host "Task '$TaskName' created successfully."
Write-Host "  Schedule         : Daily at $StartTime"
Write-Host "  Run as           : $Username"
Write-Host "  Run when logged  : Whether logged on or not"
Write-Host "  Wake PC from sleep: Yes"
Write-Host "  Start if missed  : Yes (runs ASAP if PC was off)"
Write-Host "  Wake timers      : Enabled"
Write-Host "  Action           : $BatPath"
Write-Host ""
Write-Host "To verify: Open Task Scheduler > Task Scheduler Library > $TaskName"
Write-Host "To remove: Unregister-ScheduledTask -TaskName $TaskName"
