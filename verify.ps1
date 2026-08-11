# game-update-watcher one-click verify wrapper
# Usage: powershell -ExecutionPolicy Bypass -File verify.ps1
# All logic lives in verify.py (PowerShell-parse-proof)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
python verify.py
exit $LASTEXITCODE
