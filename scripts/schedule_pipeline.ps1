# =============================================================================
# schedule_pipeline.ps1  --  Windows Task Scheduler manager for the trading
#                             pipeline cycle runner.
#
# WHAT IT DOES
# ------------
# Registers (or manages) a Scheduled Task named "TradingPipelineCycle" that
# runs  python -m pipeline.run_cycle  in the repo root directory, repeating
# on a fixed interval (default: every 30 minutes), indefinitely.
#
# The task runs in the CURRENT USER context so that locally-running services
# (Ollama on localhost, environment variables set in the user profile, etc.)
# are reachable from the pipeline process.
#
# Per-user scheduled tasks do NOT require administrator elevation.
# If registration fails, check that you are running as your own user (not as
# SYSTEM or a service account), and that Task Scheduler is not blocked by
# Group Policy.
#
# USAGE
# -----
#   Install (default: every 30 minutes):
#       .\scripts\schedule_pipeline.ps1
#       .\scripts\schedule_pipeline.ps1 -Action install -IntervalMinutes 30
#
#   Install with a custom interval (e.g. every 15 minutes):
#       .\scripts\schedule_pipeline.ps1 -Action install -IntervalMinutes 15
#
#   Check status:
#       .\scripts\schedule_pipeline.ps1 -Action status
#
#   Trigger one run now (task must already be installed):
#       .\scripts\schedule_pipeline.ps1 -Action run
#
#   Remove the task:
#       .\scripts\schedule_pipeline.ps1 -Action uninstall
#
# =============================================================================

param(
    [ValidateSet("install", "uninstall", "status", "run")]
    [string]$Action = "install",

    [int]$IntervalMinutes = 30
)

$TaskName = "TradingPipelineCycle"

# Resolve the repo root as the parent of this script's directory
$RepoRoot = Split-Path -Parent $PSScriptRoot

# ---------------------------------------------------------------------------
# Action: status
# ---------------------------------------------------------------------------
if ($Action -eq "status") {
    Write-Host "Checking task: $TaskName"
    try {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Write-Host "  Status     : $($task.State)"
        Write-Host "  Description: $($task.Description)"
        try {
            $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
            Write-Host "  Last run   : $($info.LastRunTime)"
            Write-Host "  Last result: $($info.LastTaskResult)"
            Write-Host "  Next run   : $($info.NextRunTime)"
        } catch {
            Write-Host "  (could not retrieve task info: $_)"
        }
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
        Write-Host "Task '$TaskName' has been removed."
    } catch {
        Write-Host "Could not remove task '$TaskName': $_"
        Write-Host "It may not have been registered."
    }
    exit 0
}

# ---------------------------------------------------------------------------
# Action: run  (trigger one immediate execution of the already-registered task)
# ---------------------------------------------------------------------------
if ($Action -eq "run") {
    try {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Write-Host "Task '$TaskName' triggered. Check Task Scheduler or pipeline_cycle.log for output."
    } catch {
        Write-Host "Could not start task '$TaskName': $_"
        Write-Host "Make sure you have installed it first with: .\scripts\schedule_pipeline.ps1 -Action install"
        exit 1
    }
    exit 0
}

# ---------------------------------------------------------------------------
# Action: install
# ---------------------------------------------------------------------------

Write-Host "=== Trading Pipeline Scheduler ==="
Write-Host "Task name       : $TaskName"
Write-Host "Repo root       : $RepoRoot"
Write-Host "Interval        : every $IntervalMinutes minute(s)"

# Resolve the python executable
try {
    $PythonExe = (Get-Command python -ErrorAction Stop).Source
} catch {
    Write-Host ""
    Write-Host "ERROR: 'python' not found on PATH."
    Write-Host "Activate your virtual environment or add Python to PATH, then re-run."
    exit 1
}
Write-Host "Python          : $PythonExe"

# Build the scheduled task components
$StartTime = (Get-Date).AddMinutes(1)

$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At $StartTime `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

$taskAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m pipeline.run_cycle" `
    -WorkingDirectory $RepoRoot

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

# Register the task
try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Trigger $trigger `
        -Action $taskAction `
        -Settings $settings `
        -RunLevel Limited `
        -Description "Runs the trading-pipeline cycle (collect, news, scan, thesis) every $IntervalMinutes minutes." `
        -Force `
        -ErrorAction Stop | Out-Null

    Write-Host ""
    Write-Host "Task '$TaskName' registered successfully."
    Write-Host "First run scheduled at: $($StartTime.ToString('yyyy-MM-dd HH:mm:ss'))"
    Write-Host ""
    Write-Host "Useful commands:"
    Write-Host "  Check status : .\scripts\schedule_pipeline.ps1 -Action status"
    Write-Host "  Trigger now  : .\scripts\schedule_pipeline.ps1 -Action run"
    Write-Host "  Remove task  : .\scripts\schedule_pipeline.ps1 -Action uninstall"
    Write-Host "  View log     : Get-Content pipeline_cycle.log -Tail 20"
} catch {
    Write-Host ""
    Write-Host "ERROR: Could not register the scheduled task."
    Write-Host "Details: $_"
    Write-Host ""
    Write-Host "If you see 'Access is denied':"
    Write-Host "  - Make sure you are running this PS session as YOUR OWN USER,"
    Write-Host "    NOT as Administrator or SYSTEM."
    Write-Host "  - Per-user scheduled tasks do NOT require elevation."
    Write-Host "  - If Group Policy blocks Task Scheduler, contact your admin."
    exit 1
}
