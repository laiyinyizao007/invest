#!/bin/sh
# 在宿主机(经本机代理)下载研究沙盒镜像需要的 Linux 轮子到 docker/research/wheelhouse,构建镜像时离线安装。
# 原因:容器内直连 PyPI 很慢(实测约 1 MB/s),且容器访问不到宿主机上仅监听回环的代理。
# 用法(在仓库根目录): PROXY=http://127.0.0.1:7897 sh docker/research/fetch_wheels.sh
set -eu
PY="${PY:-.venv/Scripts/python.exe}"
OUT=docker/research/wheelhouse
mkdir -p "$OUT"
if [ -n "${PROXY:-}" ]; then
    export HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY"
fi
PLAT="--platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64 --platform any --python-version 3.13 --implementation cp --only-binary=:all:"
# 1) 回测链路的运行时依赖(解析传递依赖)
$PY -m pip download -r docker/research/requirements.txt -d "$OUT" $PLAT
# 2) vnpy 三件套不带依赖(避免拉入 PySide6 等 GUI 包)
$PY -m pip download -r docker/research/requirements-vnpy.txt --no-deps -d "$OUT" $PLAT
echo "wheelhouse ready: $(ls "$OUT" | wc -l) files"
