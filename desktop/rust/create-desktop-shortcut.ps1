$ErrorActionPreference = 'Stop'
$executable = Join-Path $PSScriptRoot 'AgentX.exe'
$icon = Join-Path $PSScriptRoot 'AgentX.ico'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf) -or -not (Test-Path -LiteralPath $icon -PathType Leaf)) {
    throw 'Extract the complete AgentX ZIP before creating the shortcut.'
}
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'AgentX Codex.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$arguments = ''
if ($shortcut.TargetPath -and [IO.Path]::GetFileName($shortcut.TargetPath) -eq 'AgentX.exe') {
    $arguments = $shortcut.Arguments
}
$shortcut.TargetPath = $executable
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.IconLocation = $icon + ',0'
$shortcut.Description = 'AgentX Codex - independent account tabs'
$shortcut.Save()
Write-Output ('Desktop shortcut ready: ' + $shortcutPath)
