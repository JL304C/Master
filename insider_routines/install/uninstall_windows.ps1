# uninstall_windows.ps1 — remove all Insider scheduled tasks.
$ErrorActionPreference = "SilentlyContinue"
$Folder = "\InsiderRoutines\"
foreach ($n in @("eddie","maggie","frank","maya","janet","sophie","ross")) {
    $name = "Insider-$n"
    if (Get-ScheduledTask -TaskName $name -TaskPath $Folder -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -TaskPath $Folder -Confirm:$false
        Write-Host "  - removed $Folder$name"
    }
}
Write-Host "All Insider tasks unregistered. Your scripts + state are untouched."
