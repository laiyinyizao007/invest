"""所有脚本在导入 vnpy.trader.database 之前先 import 本模块。

1. 工作目录固定为项目根目录,并确保 .vntrader/ 存在,
   vn.py 看到 cwd 下有 .vntrader 就会把数据库、配置放在项目内(而非用户主目录)。
2. 把时区统一为中国时间。vnpy_sqlite 读取时用 naive_dt.timestamp() 还原时间,
   该调用按**系统本地时区**解释,所以必须 DB_TZ == 系统时区,往返才正确;
   机器不在中国时区时(如系统为洛杉矶),K 线时间会整体偏移,
   依赖盘中时间的逻辑(如收盘前平仓)会出错。
   做法:进程内设 TZ=CST-8(Windows CRT 的 POSIX 写法,中国无夏令时),
   同时把 vn.py 的 database.timezone 设为 Asia/Shanghai。两者都必须先于 vnpy.trader.database 导入。
3. 新浪、东财等国内数据源直连即可,绕开系统代理。
"""
import os
import sys
from pathlib import Path
from collections.abc import Mapping


_STATE_NAME = ""
ISOLATED = False

if "vnpy.trader.database" in sys.modules:
    raise RuntimeError("bootstrap 必须先于 vnpy.trader.database 导入,否则时区设置不会生效")

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
(ROOT / ".vntrader").mkdir(exist_ok=True)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # 让脚本能 import risk / strategies

os.environ["TZ"] = "CST-8"
os.environ.setdefault("NO_PROXY", "*")
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

from vnpy.trader.setting import SETTINGS  # noqa: E402

SETTINGS["database.timezone"] = "Asia/Shanghai"


def isolate_state(name: str) -> bool:
    """将 vn.py 临时目录切换到项目根目录下的隔离目录。

    必须在导入会加载 `vnpy.trader.logger` 的模块之前调用；返回值也会暴露
    当前进程是否仍处于安全导入顺序，调用方可据此拒绝连接真实柜台。

    如果日志模块**已经被导入过**(同一个进程里先导入了别的工具),vn.py 的文件 sink
    早就按当时的 TEMP_DIR 建好了,之后再改 TEMP_DIR 对它无效——日志会继续写进
    旧目录(实盘目录)。这时把旧 sink 摘掉、按新的隔离目录重新挂上,
    隔离才是真的隔离,而不是名义上的。
    """
    global _STATE_NAME, ISOLATED
    if not name or Path(name).name != name:
        raise ValueError("隔离目录名必须是单层非空目录名")
    state_root = ROOT / name
    state_root.mkdir(parents=True, exist_ok=True)
    from vnpy.trader import utility
    utility.TEMP_DIR = state_root
    _STATE_NAME = name
    ISOLATED = _rebind_log_sink(state_root)
    return ISOLATED


def logger_log_dir() -> Path | None:
    """vn.py 的日志文件实际落在哪个目录(拿不到就返回 None)。

    vnpy.trader.logger 在**导入时**按当时的 TEMP_DIR 建好文件 sink,所以想知道
    "日志到底写去哪了"只能问 sink 本身,不能只看 TEMP_DIR。
    """
    if "vnpy.trader.logger" not in sys.modules:
        return None
    try:
        from vnpy.trader.logger import logger
    except Exception:      # noqa: BLE001  日志模块坏了不该拖垮整个流程
        return None
    for handler in getattr(logger._core, "handlers", {}).values():
        sink = getattr(handler, "_sink", None)
        path = getattr(sink, "_path", None)
        if path:
            return Path(path).parent
    return None


def _rebind_log_sink(state_root: Path) -> bool:
    """把 vn.py 的日志文件 sink 挪到隔离目录。返回是否成功。

    日志模块还没被导入时不用做任何事:它稍后建 sink 时会自己用新的 TEMP_DIR。
    已经导入过时,旧 sink 是按旧目录建好的,不挪就等于没隔离。
    """
    if "vnpy.trader.logger" not in sys.modules:
        return True
    try:
        import vnpy.trader.logger as vnpy_logger_mod
        from vnpy.trader.logger import logger
    except Exception:      # noqa: BLE001
        return False
    handlers = getattr(logger._core, "handlers", {})
    file_handlers = [(hid, h) for hid, h in handlers.items()
                     if getattr(getattr(h, "_sink", None), "_path", None)]
    if not file_handlers:
        return True
    fmt = getattr(vnpy_logger_mod, "format", None)
    log_dir = state_root / "log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    for handler_id, handler in file_handlers:
        old_path = Path(getattr(handler._sink, "_path"))
        # 先挂新的再摘旧的:挂失败时旧 sink 还在,日志不会凭空消失
        try:
            logger.add(sink=str(log_dir / old_path.name),
                       level=getattr(handler, "levelno", None) or "INFO",
                       format=fmt if isinstance(fmt, str) else "{message}")
        except Exception:      # noqa: BLE001  挪不动就如实说没成功
            return False
        logger.remove(handler_id)
    # 复核:剩下的文件 sink 必须**全部**落在隔离目录里。
    # 不复核的话,「挂上了但路径不对」会被当成隔离成功,日志(含账号与资金信息)
    # 继续写进实盘目录 —— 那比不隔离还糟:调用方以为已经隔离了。
    return _log_sinks_under(state_root)


def _log_sinks_under(state_root: Path) -> bool:
    """当前所有文件 sink 是否都落在 state_root 下面。"""
    try:
        from vnpy.trader.logger import logger
    except Exception:      # noqa: BLE001
        return False
    root = state_root.resolve()
    for handler in getattr(logger._core, "handlers", {}).values():
        path = getattr(getattr(handler, "_sink", None), "_path", None)
        if not path:
            continue
        try:
            if not Path(path).resolve().is_relative_to(root):
                return False
        except OSError:
            return False
    return True


def load_env(env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """加载项目 `.env`，保留调用进程已注入的环境变量优先级。

    `encoding="utf-8-sig"`:Windows 编辑器保存的 `.env` 常带 BOM。
    不剥掉的话,第一个键会变成 `﻿CTP_USERNAME`(BOM 粘在键名上),
    dotenv 解析出来的键名和别处(`dotenv_values(..., encoding="utf-8-sig")`)
    对不上 —— 同一份文件,两条解析路径给出两套键名,闸门看到的和柜台拿到的就不一样。
    """
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False, encoding="utf-8-sig")
    return os.environ if env is None else env
