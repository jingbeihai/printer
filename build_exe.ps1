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

$assets = Join-Path $project 'app\static\desk'
$expected = Join-Path $project 'app\static\desk'
if (Test-Path -LiteralPath $assets) {
    $resolved = (Resolve-Path -LiteralPath $assets).Path
    if ($resolved -ne $expected -or
        !$resolved.StartsWith($project + [IO.Path]::DirectorySeparatorChar,
                              [StringComparison]::OrdinalIgnoreCase)) {
        throw '页面资源目录不在项目内，停止清理。'
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
Copy-Item -LiteralPath $frontend -Destination $assets -Recurse -Force

python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw '客户端依赖安装失败。' }
if (!(Test-Path -LiteralPath (Join-Path $project 'tools\SumatraPDF.exe'))) {
    python tools/fetch_sumatra.py
    if ($LASTEXITCODE -ne 0) { throw '打印组件下载失败。' }
}
python -m PyInstaller --noconfirm --clean --windowed --name '绍兴数知-印务台' --collect-all webview --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide2 --exclude-module PySide6 --add-data 'app/templates;app/templates' --add-data 'app/static;app/static' --add-data 'tools/SumatraPDF.exe;tools' --add-data 'tools/SumatraPDF-COPYING.txt;tools' run.py
if ($LASTEXITCODE -ne 0) { throw '客户端打包失败。' }
Write-Host "已生成: $project\dist\绍兴数知-印务台\绍兴数知-印务台.exe"
