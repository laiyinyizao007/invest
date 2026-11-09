# 交易节点镜像(x86_64 Linux)。vnpy_ctp 在 PyPI 上没有 Linux 预编译包,需要从源码编译,所以安装了编译工具链。
FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY risk ./risk
COPY strategies ./strategies
COPY trading ./trading
COPY notify ./notify
COPY scripts ./scripts
COPY config ./config

# 非 root 运行;状态目录通过卷挂载持久化(见 docker-compose.yml)
RUN useradd --create-home --uid 1000 trader \
    && mkdir -p /app/.vntrader /app/data/state \
    && chown -R trader:trader /app
USER trader

# 运行时每 10 秒刷新 data/state/heartbeat;超过 120 秒没刷新视为不健康
HEALTHCHECK --interval=30s --timeout=5s --start-period=240s --retries=3 \
    CMD python -c "import os,sys,time; p='data/state/heartbeat'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<120 else 1)"

ENTRYPOINT ["python", "scripts/run_headless.py"]
