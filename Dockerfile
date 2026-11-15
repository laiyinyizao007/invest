# 交易节点镜像(x86_64 Linux)。PyPI 上 vnpy_ctp 只有 Windows 预编译包,Linux 下需要编译 pybind11 扩展模块,所以装了编译工具链。
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

# vnpy_ctp 的 toUtf() 硬编码 std::locale("zh_CN.GB18030");slim 镜像默认没有这个 locale,
# CTP 回调线程构造失败会抛 std::runtime_error -> std::terminate -> 整个进程 abort(无 CTP 错误日志)。
# 这一层放在 pip 之后:改动前面的层会让 pip 重下依赖(容器网络慢,pip 那层要几分钟),
# 放在后面则可以复用已有缓存,只需重新下 locales 这一个小包。
RUN apt-get update \
    && apt-get install -y --no-install-recommends locales \
    && localedef -i zh_CN -f GB18030 zh_CN.GB18030 \
    && rm -rf /var/lib/apt/lists/*

COPY risk ./risk
COPY strategies ./strategies
COPY trading ./trading
COPY notify ./notify
COPY scripts ./scripts
COPY config ./config
COPY ctp_tools ./ctp_tools
COPY evidence ./evidence

# 非 root 运行;状态目录通过卷挂载持久化(见 docker-compose.yml)
RUN useradd --create-home --uid 1000 trader \
    && mkdir -p /app/.vntrader /app/data/state \
    && chown -R trader:trader /app
USER trader

# 运行时每 10 秒刷新 data/state/heartbeat;超过 120 秒没刷新视为不健康
HEALTHCHECK --interval=30s --timeout=5s --start-period=240s --retries=3 \
    CMD python -c "import os,sys,time; p='data/state/heartbeat'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<120 else 1)"

ENTRYPOINT ["python", "scripts/run_headless.py"]
