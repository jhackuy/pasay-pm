# uninstall-scheduled-task.ps1 — shortcut for the Uninstall flag.
[CmdletBinding()]
param()
& (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "install-scheduled-task.ps1") -Uninstall
