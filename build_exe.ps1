$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $project
python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean --windowed --name '佳能打印管理' --collect-all webview --exclude-module PyQt5 --exclude-module PySide6 --add-data 'app/templates;app/templates' --add-data 'app/static;app/static' run.py
Write-Host "已生成: $project\dist\佳能打印管理\佳能打印管理.exe"
