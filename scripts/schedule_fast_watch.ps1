# =============================================================================
# schedule_fast_watch.ps1  --  Windows Task Scheduler manager for the FAST
#                              position-watcher (price trip-wire).
#
# WHAT IT DOES
# ------------
# Registers a Scheduled Task "TradingFastWatch" that runs
#   python -m pipeline.fast_watch
# in the repo root, repeating every few MINUTES (default 3). Each run is one
# cheap pass: it polls the live price of every open position and, on a sharp
# adverse move, escalates to the full monitor review + writes an alert. This is
# the layer that would have caught the Fable collapse in minutes, not hours.
#
# WHY one-shot-every-3-min (not --loop): matches run_cycle's model (one pass per
# invocation), so a crash self-heals on the next tick instead of leaving a dead
# long-lived process. -WakeToRun + StartWhenAvailable make it fire even if the
# machine briefly slept; pair with disabling sleep (see -Action sleep-off).
#
# Per-user task: no admin elevation needed.
#
# USAGE
# -----
#   Install (default: every 3 minutes):   .\scripts\schedule_fast_watch.ps1
#   Custom interval:                       .\scripts\schedule_fast_watch.ps1 -IntervalMinutes 2
#   Status:                                .\scripts\schedule_fast_watch.ps1 -Action status
#   Run one pass now:                      .\scripts\schedule_fast_watch.ps1 -Action run
#   Remove:                                .\scripts\schedule_fast_watch.ps1 -Action uninstall
#   Disable PC sleep (keeps it watching):  .\scripts\schedule_fast_watch.ps1 -Action sleep-off
# =============================================================================

param(
    [ValidateSet("install", "uninstall", "status", "run", "sleep-off")]
    [string]$Action = "install",

    [int]$IntervalMinutes = 3
)

$TaskName = "TradingFastWatch"
$RepoRoot = Split-Path -Parent $PSScriptRoot

# ---------------------------------------------------------------------------
# Action: sleep-off  (disable standby/hibernate so the watcher keeps running)
# ---------------------------------------------------------------------------
if ($Action -eq "sleep-off") {
    Write-Host "Disabling sleep/hibernate so fast_watch keeps running 24/7..."
    # 0 = never. AC (plugged in) and DC (battery) both set.
    powercfg /change standby-timeout-ac 0
    powercfg /change standby-timeout-dc 0
    powercfg /change hibernate-timeout-ac 0
    powercfg /change hibernate-timeout-dc 0
    Write-Host "Done. Current sleep settings:"
    powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE | Select-String "Power Setting|Current"
    Write-Host ""
    Write-Host "NOTE: if you see 'Access is denied', run this one line in an ADMIN"
    Write-Host "PowerShell:  powercfg /change standby-timeout-ac 0; powercfg /change hibernate-timeout-ac 0"
    exit 0
}

# ---------------------------------------------------------------------------
# Action: status
# ---------------------------------------------------------------------------
if ($Action -eq "status") {
    try {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Write-Host "  Status : $($task.State)"
        $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
        Write-Host "  Last run   : $($info.LastRunTime)  (result $($info.LastTaskResult))"
        Write-Host "  Next run   : $($info.NextRunTime)"
    } catch {
        Write-Host "  Task '$TaskName' is not registered."
    }
    exit 0
}

# ---------------------------------------------------------------------------
# Action: uninstall
# ---------------------------------------------------------------------------
if ($Action -eq "uninstall") {
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "Task '$TaskName' removed."
    } catch {
        Write-Host "Could not remove task '$TaskName' (may not exist): $_"
    }
    exit 0
}

# ---------------------------------------------------------------------------
# Action: run
# ---------------------------------------------------------------------------
if ($Action -eq "run") {
    try {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Write-Host "Task '$TaskName' triggered. See alerts.log for any fires."
    } catch {
        Write-Host "Could not start '$TaskName' (install it first): $_"
        exit 1
    }
    exit 0
}

# ---------------------------------------------------------------------------
# Action: install
# ---------------------------------------------------------------------------
Write-Host "=== Fast-Watch Scheduler ==="
Write-Host "Task name : $TaskName"
Write-Host "Repo root : $RepoRoot"
Write-Host "Interval  : every $IntervalMinutes minute(s)"

try {
    $PythonExe = (Get-Command python -ErrorAction Stop).Source
} catch {
    Write-Host "ERROR: 'python' not found on PATH. Activate your venv or add Python to PATH."
    exit 1
}
Write-Host "Python    : $PythonExe"

$StartTime = (Get-Date).AddMinutes(1)

$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At $StartTime `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

$taskAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m pipeline.fast_watch" `
    -WorkingDirectory $RepoRoot

# WakeToRun: fire even if the machine briefly slept. ExecutionTimeLimit short —
# a pass should finish fast; kill a hung one so the next tick is clean.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Trigger $trigger `
        -Action $taskAction `
        -Settings $settings `
        -RunLevel Limited `
        -Description "Fast position-watcher: price trip-wire on open trades, every $IntervalMinutes min." `
        -Force `
        -ErrorAction Stop | Out-Null

    Write-Host ""
    Write-Host "Task '$TaskName' registered. First run: $($StartTime.ToString('HH:mm:ss'))"
    Write-Host "Also recommended:  .\scripts\schedule_fast_watch.ps1 -Action sleep-off"
    Write-Host "Alerts land in:    alerts.log"
} catch {
    Write-Host "ERROR: could not register task. Details: $_"
    Write-Host "Run the PS session as your OWN user (not Admin/SYSTEM); per-user tasks need no elevation."
    exit 1
}
