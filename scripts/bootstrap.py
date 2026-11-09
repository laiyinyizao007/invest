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

if "vnpy.trader.database" in sys.modules:
    raise RuntimeError("bootstrap 必须先于 vnpy.trader.database 导入,否则时区设置不会生效")

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
(ROOT / ".vntrader").mkdir(exist_ok=True)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # 让脚本能 import risk / strategies

os.environ["TZ"] = "CST-8"
os.environ.setdefault("NO_PROXY", "*")

from vnpy.trader.setting import SETTINGS  # noqa: E402

SETTINGS["database.timezone"] = "Asia/Shanghai"
