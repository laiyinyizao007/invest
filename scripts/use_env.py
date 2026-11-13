"""在多套 CTP 环境档案之间切换:把 `.env.<名称>` 复制为 `.env`。

用法:
    python scripts/use_env.py            # 列出档案并标出当前使用的
    python scripts/use_env.py simnow     # 切到 SimNow(.env.simnow)
    python scripts/use_env.py guoxin     # 切到国信测试(.env.guoxin)

只展示 环境/BrokerID/前置地址,不打印账号、密码、授权码。
切换前若 `.env` 与任何档案都不一致(手改过),先存为 `.env.prev` 再覆盖,避免丢失。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOW = ("CTP_ENVIRONMENT", "CTP_BROKER_ID", "CTP_TD_ADDRESS", "CTP_MD_ADDRESS")


def profiles() -> dict[str, Path]:
    return {p.name[5:]: p for p in sorted(ROOT.glob(".env.*"))
            if p.name[5:] not in ("example", "prev") and not p.name.endswith(".bak")}


def summary(path: Path) -> str:
    kv = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() in SHOW:
            kv[k.strip()] = v.strip()
    return "  ".join(f"{k[4:]}={kv.get(k, '?')}" for k in SHOW)


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    profs = profiles()
    env = ROOT / ".env"
    current = next((n for n, p in profs.items() if env.exists() and p.read_bytes() == env.read_bytes()), None)
    if not argv:
        for n, p in profs.items():
            print(f"{'*' if n == current else ' '} {n:8s} {summary(p)}")
        if env.exists() and current is None:
            print("! 当前 .env 与任何档案都不一致(手改过)")
        return 0
    name = argv[0]
    if name not in profs:
        print(f"没有档案 .env.{name};可用:{', '.join(profs) or '无'}")
        return 2
    if env.exists() and current is None:
        shutil.copy2(env, ROOT / ".env.prev")
        print("当前 .env 不属于任何档案,已另存为 .env.prev")
    shutil.copy2(profs[name], env)
    print(f"已切换到 {name}:{summary(env)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
