param([string]$Directory = (Resolve-Path "$PSScriptRoot/..").Path)
$python = Join-Path $Directory '.venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $python)) { throw 'Create the Windows virtual environment first.' }
$action = New-ScheduledTaskAction -Execute $python -Argument ('"' + (Join-Path $Directory 'laptop_daemon.py') + '"') -WorkingDirectory $Directory
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName 'ARGUS Laptop Daemon' -Action $action -Trigger $trigger -Settings $settings -Description 'Connect ARGUS in the signed-in desktop session.'
