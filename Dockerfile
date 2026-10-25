FROM node:22-alpine AS frontend-builder

WORKDIR /frontend

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY backend/requirements.txt backend/requirements.lock ./
# 国内网络直连 pypi.org 会 SSL EOF，可在构建时换源：
#   docker build -f Dockerfile -t busterminal:v1.18 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
# 依赖按 requirements.lock 全量锁定安装，保证每次构建装到同一套已验证版本（见 Issue #1）。
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --disable-pip-version-check \
      --index-url ${PIP_INDEX_URL} --timeout 60 --retries 3 -r requirements.lock

COPY backend/ .

COPY --from=frontend-builder /frontend/dist ./static/

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=3)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
