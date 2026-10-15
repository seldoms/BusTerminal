@echo off
rem 本地开发后端（127.0.0.1:8001）。
rem 路径一律以脚本所在目录为根（%~dp0），不写死任何本机绝对路径。
setlocal
set "ROOT=%~dp0"

set DATA_DIR=%ROOT%data
set BUS_TERMINAL_DISABLE_STARTUP_TASKS=true
set BUS_TERMINAL_DISABLE_DOMAIN_ROTATION=true
set BUS_TERMINAL_ALLOW_SCHEMA_MIGRATION=true

rem 本地开发库 = 本机 bus_postgres 容器：docker compose start bus_postgres
rem 口令从被 gitignore 的 .env 读取，保证凭据不出现在被跟踪的文件里。
if exist "%ROOT%.env" (
  for /f "usebackq tokens=1,* delims==" %%a in ("%ROOT%.env") do if "%%a"=="BUS_PG_PASSWORD" set BUS_PG_PASSWORD=%%b
) else (
  echo [warn] 未找到 %ROOT%.env，将不带 BUS_PG_PASSWORD 启动。
)
set DATABASE_URL=postgresql://busterminal:%BUS_PG_PASSWORD%@127.0.0.1:15433/busterminal

cd /d %ROOT%backend
python.exe -m uvicorn main:app --host 127.0.0.1 --port 8001 > "%ROOT%backend\dev-backend.log" 2> "%ROOT%backend\dev-backend.err.log"
