# Windows PowerShell 5 reads this file as UTF-8 with BOM.
$ErrorActionPreference = 'Stop'
$directory = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $directory '印务台启动器.exe'
if (!(Test-Path -LiteralPath $launcher)) { throw '安装包缺少启动器。' }
$version = (Get-Content -LiteralPath (Join-Path $directory '版本.json') -Raw | ConvertFrom-Json)
$process = Start-Process -FilePath $launcher -ArgumentList @('--bootstrap', ('"' + $directory + '"'), '--version', $version.version, '--sequence', $version.sequence) -Wait -PassThru
if ($process.ExitCode -ne 0) { throw "客户端安装失败，退出码 $($process.ExitCode)" }
$installedLauncher = Join-Path $env:LOCALAPPDATA 'CanonPrintDesk\client\印务台启动器.exe'
if (!(Test-Path -LiteralPath $installedLauncher)) { throw '安装完成后未找到启动器。' }
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) '绍兴数知-印务台.lnk'))
$shortcut.TargetPath = $installedLauncher
$shortcut.WorkingDirectory = Split-Path -Parent $installedLauncher
$shortcut.Save()
$menu = Join-Path ([Environment]::GetFolderPath('Programs')) '绍兴数知-印务台.lnk'
$shortcut = $shell.CreateShortcut($menu)
$shortcut.TargetPath = $installedLauncher
$shortcut.WorkingDirectory = Split-Path -Parent $installedLauncher
$shortcut.Save()
