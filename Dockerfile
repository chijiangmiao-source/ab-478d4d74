# 海洋测绘航迹导出服务 —— 仅依赖 Python 标准库，无需 pip 安装
FROM python:3.11-slim

WORKDIR /app

COPY app/ ./app/
COPY web/ ./web/
COPY verify/ ./verify/
COPY tests/ ./tests/

ENV DATA_DIR=/data \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080 \
    LEASE_SECONDS=30 \
    HEARTBEAT_SECONDS=10 \
    POLL_SECONDS=0.5 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
VOLUME ["/data"]

# 默认启动 API + 一个同进程后台工作线程；compose 中可覆盖为 --no-worker
CMD ["python", "-m", "app.main"]
