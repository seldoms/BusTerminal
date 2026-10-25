# 个人环境构建备忘（非受支持的构建路径）

> **这不是 BusTerminal 受支持的构建方式。** 正式安装/构建请只用仓库根目录的标准
> `Dockerfile`（见 `docs/INSTALL.md` 与 README）。
>
> 本文件只是把原仓库根目录的 `Dockerfile.release` 内容原样归档留存：它曾是作者个人
> 机器上的变通方案——那台机器的 Docker Desktop 凭据助手异常，拉取基础镜像会挂住，
> 于是改用本机已有的应用镜像作底座、覆盖工作区代码。它依赖本机预先存在的
> `busterminal-busterminal:latest` 镜像并要求宿主先手动打包前端，换一台机器必然
> 构建失败，因此已从仓库根目录移出（Issue #2）。根目录 `.gitignore` 已忽略
> `Dockerfile.release`，个人仍可在本地保留同名文件自用，不要提交。
>
> 以下为原文件全文（围栏内，逐字保留，包括其中已过时的 `python:3.10-slim` /
> 版本号 v1.17 等历史表述）：

```dockerfile
# 版本化发布镜像（离线构建）
#
# 背景：Docker Desktop 凭据助手异常，docker pull node:22-alpine / python:3.10-slim 会挂住，
# 原 Dockerfile（多阶段联网构建）在这台机器上跑不通。这里改成以本机已有的应用镜像为底座，
# 直接覆盖当前工作区的后端代码与前端打包产物，效果等价且无需联网。
#
# 用法：
#   docker compose build busterminal
#   docker compose up -d --no-build
FROM busterminal-busterminal:latest

LABEL version="1.17" \
      description="BusTerminal 发布镜像 v1.17（含本地大模型翻译队列、只展示已处理条目）"

WORKDIR /app

ENV TZ=Asia/Shanghai

# 后端：用当前工作区版本覆盖镜像里的旧代码（与热挂载时期跑的是同一份源码）
COPY backend/ /app/
RUN rm -rf /app/__pycache__

# 前端：宿主已 npm run build，这里把打包产物放进镜像
COPY frontend/dist/ /app/static/

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=3)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```
