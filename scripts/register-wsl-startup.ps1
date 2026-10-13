param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string]$Distribution,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$taskName = "rbridge - $Distribution"
if ($Remove) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Output 'Startup task removed; WSL services were not uninstalled.'
    return
}

& "$env:SystemRoot\System32\wsl.exe" -d $Distribution -u root --exec test -x /usr/local/sbin/rbridge-keepalive
if ($LASTEXITCODE -ne 0) {
    throw 'Deploy the client role inside the selected WSL distribution first.'
}
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\wsl.exe" `
    -Argument "-d $Distribution -u root --exec /usr/local/sbin/rbridge-keepalive"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description 'Maintain the FRP SSH tunnel inside WSL.' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output 'Startup task registered for the current Windows account and started.'
