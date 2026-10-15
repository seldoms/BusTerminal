@echo off
rem 本地开发前端（127.0.0.1:3000），Vite 把 /api 代理到 127.0.0.1:8001。
rem 路径以脚本所在目录为根（%~dp0）。
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%frontend"
npm.cmd run dev -- --host 127.0.0.1 --port 3000 > "%ROOT%frontend\dev-frontend.log" 2> "%ROOT%frontend\dev-frontend.err.log"
