param(
    [string]$Name = "AutoClicker"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "py was not found. Please install Python and make sure the py launcher is available."
}

py -m PyInstaller --noconfirm --clean --onefile --noconsole `
    --name $Name `
    --icon "assets\app.ico" `
    --add-data "assets;assets" `
    --distpath "dist" `
    --workpath "build" `
    --specpath "." `
    "main.py"

Write-Host "Build complete: dist\$Name.exe"
