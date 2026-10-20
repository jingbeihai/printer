$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $project
$builtAt = (Get-Date).ToUniversalTime()
Push-Location (Join-Path $project 'prototype')
try {
    $ErrorActionPreference = 'Continue'
    $buildOutput = & npm.cmd run build 2>&1
    $ErrorActionPreference = 'Stop'
    $buildText = $buildOutput -join "`n"
    $frontend = Join-Path $project 'prototype\dist\client'
    $index = Join-Path $frontend 'index.html'
    if (!(Test-Path -LiteralPath $index) -or
        (Get-Item -LiteralPath $index).LastWriteTimeUtc -lt $builtAt.AddSeconds(-2) -or
        !$buildText.Contains('Build complete.')) {
        throw "业务页面构建失败：`n$buildText"
    }
    # 当前 Windows Node 在完整生成页面后可能以 UV_HANDLE_CLOSING 退出。
    if ($LASTEXITCODE -ne 0 -and !$buildText.Contains('UV_HANDLE_CLOSING')) {
        throw "业务页面构建异常退出：`n$buildText"
    }
} finally {
    $ErrorActionPreference = 'Stop'
    Pop-Location
}

$frontendData = "$frontend;app/static/desk"

python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw '客户端依赖安装失败。' }
if (!(Test-Path -LiteralPath (Join-Path $project 'tools\SumatraPDF.exe'))) {
    python tools/fetch_sumatra.py
    if ($LASTEXITCODE -ne 0) { throw '打印组件下载失败。' }
}
python -m PyInstaller --noconfirm --clean --windowed --name '绍兴数知-印务台' --collect-all webview --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide2 --exclude-module PySide6 --add-data 'app/templates;app/templates' --add-data $frontendData --add-data 'app/update-channel.json;app' --add-data 'tools/SumatraPDF.exe;tools' --add-data 'tools/SumatraPDF-COPYING.txt;tools' run.py
if ($LASTEXITCODE -ne 0) { throw '客户端打包失败。' }
python -m PyInstaller --noconfirm --clean --onefile --windowed --name '印务台启动器' --distpath 'dist\_launcher' --workpath 'build\_launcher' --add-data 'app/update-channel.json;app' run_updater.py
if ($LASTEXITCODE -ne 0) { throw '更新启动器打包失败。' }
Copy-Item -LiteralPath 'dist\_launcher\印务台启动器.exe' -Destination 'dist\绍兴数知-印务台\印务台启动器.exe' -Force
Copy-Item -LiteralPath 'install_client.ps1' -Destination 'dist\绍兴数知-印务台\install_client.ps1' -Force
Copy-Item -LiteralPath 'install_client.ps1' -Destination 'dist\绍兴数知-印务台\安装客户端.ps1' -Force
Copy-Item -LiteralPath '安装客户端.cmd' -Destination 'dist\绍兴数知-印务台\安装客户端.cmd' -Force
python -c "import json; from pathlib import Path; from app.update_client import VERSION, SEQUENCE; Path('dist/绍兴数知-印务台/版本.json').write_text(json.dumps({'version': VERSION, 'sequence': SEQUENCE}), encoding='utf-8')"
if ($LASTEXITCODE -ne 0) { throw '写入版本信息失败。' }
Write-Host "已生成: $project\dist\绍兴数知-印务台\绍兴数知-印务台.exe"
