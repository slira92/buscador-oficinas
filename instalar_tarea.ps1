# Programa el buscador de oficinas en el Programador de tareas de Windows.
# Todos los dias a las 09:30, 13:30 y 17:30 (cada 4 h por 8 h). Despierta el PC si esta suspendido y enchufado.
#
#   Instalar:     powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1
#   Desinstalar:  powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1 -Desinstalar
param(
    [int]$CadaHoras = 4,
    [string]$Desde = "09:30",
    [int]$Horas = 8,
    [switch]$Desinstalar
)
$nombre = "Buscador Oficinas"
if ($Desinstalar) {
    Unregister-ScheduledTask -TaskName $nombre -Confirm:$false
    Write-Host "Tarea '$nombre' eliminada."
    return
}
$dir = $PSScriptRoot
$pyw = (& py -c "import sys, pathlib; print(pathlib.Path(sys.executable).with_name('pythonw.exe'))").Trim()
if (-not (Test-Path $pyw)) { $pyw = (Get-Command pyw -ErrorAction Stop).Source }
$accion = New-ScheduledTaskAction -Execute $pyw -Argument "`"$dir\buscador_oficinas.py`"" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At $Desde
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $Desde -RepetitionInterval (New-TimeSpan -Hours $CadaHoras) -RepetitionDuration (New-TimeSpan -Hours $Horas)).Repetition
$opciones = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $nombre -Action $accion -Trigger $trigger -Settings $opciones -Description "Busca oficinas en Portal Inmobiliario y avisa las que calzan" -Force | Out-Null
Write-Host "Tarea '$nombre' instalada: todos los dias cada $CadaHoras h desde $Desde por $Horas h."
