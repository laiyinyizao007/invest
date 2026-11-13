import sys, os
sys.path.insert(0, r"C:\Dev\projects\invest-14\scripts")
sys.path.insert(0, r"C:\Dev\projects\invest-14")
import vnpy.trader.logger as L
from vnpy.trader.logger import logger
print("handlers:", logger._core.handlers)
for hid, h in logger._core.handlers.items():
    print(" handler", hid, type(h).__name__, "levelno=", getattr(h,"levelno",None), "sink._path=", getattr(getattr(h,"_sink",None),"_path",None))
import bootstrap
print("log dir BEFORE isolate:", bootstrap.logger_log_dir())
ok = bootstrap.isolate_state(".vntrader_isotest")
print("ISOLATED:", ok)
print("log dir AFTER isolate:", bootstrap.logger_log_dir())
logger.info("ISOLATION-PROBE-MARKER")
for hid,h in logger._core.handlers.items():
    print(" after handler", hid, getattr(getattr(h,"_sink",None),"_path",None))
