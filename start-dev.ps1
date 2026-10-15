# PowerShell Start-Dev Services Script
# 路径以脚本所在目录为根（$PSScriptRoot），不写死本机绝对路径。

$Root = $PSScriptRoot
$env:DATA_DIR = Join-Path $Root "data"
$env:BUS_TERMINAL_DISABLE_STARTUP_TASKS = "true"
$env:BUS_TERMINAL_DISABLE_DOMAIN_ROTATION = "true"

# 1. 启动后端 (8001)
Write-Host "Starting Backend Service on 8001..."
$backendProcess = Start-Process -FilePath "python.exe" -ArgumentList "-m uvicorn main:app --host 0.0.0.0 --port 8001" -WorkingDirectory (Join-Path $Root "backend") -PassThru -WindowStyle Hidden

# 2. 启动前端 (3000)
Write-Host "Starting Frontend Vite Service on 3000..."
$frontendProcess = Start-Process -FilePath "cmd.exe" -ArgumentList "/c npx vite --host 0.0.0.0 --port 3000" -WorkingDirectory (Join-Path $Root "frontend") -PassThru -WindowStyle Hidden

Write-Host "Both Backend (8001) and Frontend (3000) started successfully!"
