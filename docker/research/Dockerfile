# 研究沙盒网关镜像:与交易镜像完全分离,不含 CTP 库、不含 .env、不含 notify/trading。
# 构建上下文是仓库根目录:docker build -f docker/research/Dockerfile -t invest-research .
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
# 依赖从本地 wheelhouse 离线安装(宿主机经代理预先下载:sh docker/research/fetch_wheels.sh)。
# vnpy 三件套 --no-deps,避免拉入 PySide6 等数百 MB 的 GUI 依赖(无头回测用不到)。
COPY docker/research/requirements.txt docker/research/requirements-vnpy.txt ./
COPY docker/research/wheelhouse /wheelhouse
RUN pip install --no-index --find-links=/wheelhouse -r requirements.txt     && pip install --no-index --find-links=/wheelhouse --no-deps -r requirements-vnpy.txt     && rm -rf /wheelhouse

# 只拷研究链路需要的代码;不拷 trading/notify/.env/data
COPY risk ./risk
COPY strategies ./strategies
COPY scripts ./scripts
COPY config ./config
COPY docker/research/entrypoint.sh /entrypoint.sh

RUN useradd --create-home --uid 1000 research \
    && mkdir -p /app/.vntrader /app/data/research /app/pending_strategies \
    && chown -R research:research /app \
    && chmod +x /entrypoint.sh
USER research

ENTRYPOINT ["/entrypoint.sh"]
