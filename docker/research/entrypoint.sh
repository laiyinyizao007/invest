#!/bin/sh
# 研究沙盒网关入口。
# 1) 日线库是只读种子,复制一份到容器自己的 .vntrader(vn.py 会在其中写日志);
# 2) 监听地址取容器自己的 IP,而不是 0.0.0.0(配置校验禁止监听所有网卡);
# 3) 进程环境里不得有 CTP_* 变量(网关启动时再自检一次)。
set -eu

if [ -f /seed/database.db ]; then
    cp /seed/database.db /app/.vntrader/database.db
fi

IP="$(hostname -i | awk '{print $1}')"
python - "$IP" <<'PY'
import json, sys
cfg = json.load(open("/app/config/research_sandbox.json", encoding="utf-8"))
cfg["gateway"]["host"] = sys.argv[1]
json.dump(cfg, open("/tmp/research_sandbox.json", "w", encoding="utf-8"))
PY
export RESEARCH_SANDBOX_CONFIG=/tmp/research_sandbox.json

exec python scripts/research_gateway.py serve
