<#
.SYNOPSIS
Schedules the daily Bank of Ghana FX pull on Windows.

.DESCRIPTION
Registers a Task Scheduler task, for the current user, that runs
`python -m app.fx.pull` (app/fx/pull.py), so the rates in the database
stay current even while the service is off. Each run appends to
fx-pull.log in the project folder.

BoG publishes one fix per business day but gives no time for it: on
7 Oct 2026 it wasn't out at 12:25 GMT. And a laptop is often asleep at
any one time (the first version of this task ran at 18:00 only and
missed, asleep), so the task takes every chance it gets:
- at the Ghana times given (default 09:00, before the GSE opens, and
  hourly 16:00-22:00), or as soon as the PC is awake after one it missed;
- whenever you log on or unlock the PC.
That costs nothing: once today's fix is stored, a run ends without
contacting BoG. Ghana is on GMT all year; the times are converted to
this PC's time zone.

Run it again to change the times (it replaces the task); -Remove takes
it off. No administrator rights needed.

.EXAMPLE
.\scripts\register-fx-pull-task.ps1
.\scripts\register-fx-pull-task.ps1 -GhanaTimes 09:00,17:00,20:00
.\scripts\register-fx-pull-task.ps1 -Remove
#>
param(
    [string[]]$GhanaTimes = @("09:00", "16:00", "17:00", "18:00", "19:00", "20:00", "21:00", "22:00"),
    [string]$TaskName = "market-integration BoG FX pull",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed the scheduled task '$TaskName'."
    return
}

# The project folder: the one above this script's.
$root = Split-Path -Parent $PSScriptRoot
# pythonw: no console window popping up at each run.
$python = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) {
    throw "No virtual environment at $root\.venv. Set it up first (README, Setup)."
}

$user = "$env:USERDOMAIN\$env:USERNAME"

# Ghana time (GMT) to this PC's local time, for the daily triggers.
$daily = foreach ($t in $GhanaTimes) {
    $gmt = [datetime]::SpecifyKind([datetime]::Today.Add([timespan]::Parse($t)), [DateTimeKind]::Utc)
    New-ScheduledTaskTrigger -Daily -At $gmt.ToLocalTime()
}
# Logging on, and unlocking (no cmdlet parameter for that one: it's a
# session state change, 8 = unlock), both for this user only.
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$stateChange = Get-CimClass -Namespace ROOT\Microsoft\Windows\TaskScheduler -ClassName MSFT_TaskSessionStateChangeTrigger
$unlock = New-CimInstance -CimClass $stateChange -ClientOnly -Property @{ StateChange = 8; UserId = $user; Enabled = $true }
$triggers = @($daily) + $logon + $unlock

$action = New-ScheduledTaskAction -Execute $python -Argument "-m app.fx.pull --log fx-pull.log" -WorkingDirectory $root
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Force `
    -Description "Pulls the Bank of Ghana interbank FX rates into $root\market_data.db (app/fx/pull.py)." | Out-Null

$local = ($daily | ForEach-Object { ([datetime]$_.StartBoundary).ToString("HH:mm") }) -join ", "
Write-Host "Scheduled '$TaskName': daily at $($GhanaTimes -join ', ') Ghana time ($local here), and at log on and unlock."
Write-Host "Log: $root\fx-pull.log. Run it now with: Start-ScheduledTask -TaskName '$TaskName'"
