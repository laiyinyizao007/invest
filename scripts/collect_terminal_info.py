"""采集期货公司穿透式监管"终端信息采集表"需要的本机信息(仅 Windows,在**实际运行交易程序的那台机器**上执行)。

为什么要在运行机上跑、而且不能进容器:穿透式监管要求上报的是真实运行环境的 IP/MAC/硬盘序列号等。
在别的机器(或 Docker 容器)里采到的是别的设备的信息,和 CTP 上报的对不上,会被期货公司驳回重测。

用法(在运行机上,项目根目录):
    python scripts/collect_terminal_info.py                 # 文本输出,可直接照着填表
    python scripts/collect_terminal_info.py --json          # JSON 输出
    python scripts/collect_terminal_info.py --public-ip     # 额外查询公网 IP(会访问第三方服务)
    python scripts/collect_terminal_info.py --out info.txt  # 同时写入文件

只读、不改任何系统设置。除 --public-ip 外不联网(出网网卡的判断用 UDP connect,不会发出任何数据包)。
采集表里要填的字段以期货公司给的表格为准,这里尽量多采,多余的不用填。
"""
import argparse
import json
import platform
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime

POWERSHELL_TIMEOUT = 30
# 公网 IP 查询服务:只返回一行 IP 文本。需要时由 --public-ip 显式开启。
PUBLIC_IP_URL = "https://api.ipify.org"

# 每项查询:字段名 → PowerShell 命令(输出 JSON)。查询失败只记录空值,不影响其它项。
PS_QUERIES = {
    "os": "Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture,CSName",
    "cpu": "Get-CimInstance Win32_Processor | Select-Object Name,ProcessorId",
    "baseboard": "Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product,SerialNumber",
    "bios": "Get-CimInstance Win32_BIOS | Select-Object SerialNumber",
    "computer": "Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,TotalPhysicalMemory",
    "disks": "Get-CimInstance Win32_DiskDrive | Select-Object Model,SerialNumber,Size",
    "adapters": ("Get-CimInstance Win32_NetworkAdapterConfiguration -Filter 'IPEnabled=true' "
                 "| Select-Object Description,MACAddress,IPAddress,DefaultIPGateway"),
}

# 名称里带这些字样的多半是虚拟网卡(VPN/虚拟机/容器),登记终端时通常不该用它们的 MAC
VIRTUAL_HINTS = ("virtual", "vmware", "hyper-v", "vethernet", "vpn", "tap-", "wintun", "wireguard",
                 "tailscale", "zerotier", "tunnel", "loopback", "docker", "wsl", "bluetooth")


def as_list(value) -> list[dict]:
    """PowerShell 的 ConvertTo-Json 对单个对象输出字典、对多个输出数组、对空输出空串,统一成列表。"""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def run_powershell(command: str) -> list[dict]:
    """执行一条 PowerShell 查询并解析 JSON。失败返回空列表(并不抛出)。"""
    script = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
              f"{command} | ConvertTo-Json -Compress")
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, timeout=POWERSHELL_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    text = proc.stdout.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return []
    try:
        return as_list(json.loads(text))
    except json.JSONDecodeError:
        return []


def parse_target(target: str) -> tuple[str, int]:
    """`tcp://1.2.3.4:30001` / `1.2.3.4:30001` / `1.2.3.4` → (host, port);空串用互联网探测地址。"""
    text = (target or "").strip().split("://", 1)[-1].split("/", 1)[0]
    if not text:
        return "8.8.8.8", 80
    host, _, port = text.partition(":")
    return host, int(port) if port.isdigit() else 80


def outbound_ip(target: str = "") -> str:
    """本机访问目标(默认互联网 8.8.8.8;可传期货公司前置地址)时使用的本地 IP。
    UDP connect 只做路由选择,不会发送任何数据。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(parse_target(target))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def is_virtual_adapter(description: str) -> bool:
    lowered = (description or "").lower()
    return any(hint in lowered for hint in VIRTUAL_HINTS)


def normalize_adapters(raw: list[dict], primary_ip: str) -> list[dict]:
    """整理网卡列表:IPv4/IPv6 分开、标记虚拟网卡与"出网网卡"。"""
    adapters = []
    for item in raw:
        addresses = item.get("IPAddress") or []
        if isinstance(addresses, str):
            addresses = [addresses]
        ipv4 = [a for a in addresses if "." in a]
        ipv6 = [a for a in addresses if ":" in a]
        description = item.get("Description") or ""
        gateway = item.get("DefaultIPGateway") or []
        if isinstance(gateway, str):
            gateway = [gateway]
        adapters.append({
            "description": description,
            "mac": (item.get("MACAddress") or "").replace(":", "-"),
            "ipv4": ipv4,
            "ipv6": ipv6,
            "gateway": gateway,
            "virtual": is_virtual_adapter(description),
            "primary": bool(primary_ip) and primary_ip in ipv4,
        })
    return adapters


def machine_info(item: dict) -> dict:
    """整机信息:厂商、型号、内存(GB)。程序化测试报告的「测试机型号/内存」栏用。"""
    total = int(item.get("TotalPhysicalMemory") or 0)
    return {"manufacturer": (item.get("Manufacturer") or "").strip(), "model": (item.get("Model") or "").strip(),
            "memory_gb": round(total / 1024 ** 3)}


def package_version(name: str) -> str:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return ""


def fetch_public_ip(timeout: float = 8.0) -> str:
    try:
        with urllib.request.urlopen(PUBLIC_IP_URL, timeout=timeout) as resp:
            return resp.read().decode("ascii", errors="replace").strip()
    except Exception:
        return ""


def collect(with_public_ip: bool = False, target: str = "") -> dict:
    """采集全部信息,返回可直接序列化的字典。"""
    raw = {key: run_powershell(cmd) for key, cmd in PS_QUERIES.items()}
    primary = outbound_ip(target)
    os_info = (raw["os"] or [{}])[0]
    return {
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "hostname": socket.gethostname(),
        "os": {
            "name": os_info.get("Caption", ""),
            "version": os_info.get("Version", ""),
            "build": os_info.get("BuildNumber", ""),
            "arch": os_info.get("OSArchitecture", "") or platform.machine(),
        },
        "cpu": [{"name": c.get("Name", ""), "processor_id": c.get("ProcessorId", "")} for c in raw["cpu"]],
        "baseboard": [{"manufacturer": b.get("Manufacturer", ""), "product": b.get("Product", ""),
                       "serial": (b.get("SerialNumber") or "").strip()} for b in raw["baseboard"]],
        "bios_serial": ((raw["bios"] or [{}])[0].get("SerialNumber") or "").strip(),
        "machine": machine_info((raw["computer"] or [{}])[0]),
        "disks": [{"model": d.get("Model", ""), "serial": (d.get("SerialNumber") or "").strip(),
                   "size_gb": round(int(d.get("Size") or 0) / 1024 ** 3)} for d in raw["disks"]],
        "adapters": normalize_adapters(raw["adapters"], primary),
        "outbound_ip": primary,
        "route_target": target or "互联网(8.8.8.8)",
        "ps_ok": bool(raw["os"]) and bool(raw["adapters"]),
        "public_ip": fetch_public_ip() if with_public_ip else "",
        "python": platform.python_version(),
        "vnpy": package_version("vnpy"),
        "vnpy_ctp": package_version("vnpy_ctp"),
    }


def warnings_for(info: dict) -> list[str]:
    """采集结果里需要人工留意的地方。"""
    notes = []
    if not info.get("ps_ok", True):
        notes.append("PowerShell/WMI 查询失败(被策略禁用、超时或无权限):下面的空字段不是「没有」,请换管理员权限重试或手工填写。")
    adapters = info["adapters"]
    if not any(a["primary"] for a in adapters):
        notes.append("没能判断出出网网卡(可能没联网);请人工确认登记哪块网卡。")
    if len(adapters) > 1:
        notes.append(f"发现 {len(adapters)} 块启用 IP 的网卡。登记终端用**实际访问期货公司前置的那块**"
                     "(标了「出网」;默认按访问互联网判断,已有前置地址时请加 --target 重新判断),不要填虚拟网卡/VPN 网卡。")
    primary = [a for a in adapters if a["primary"]]
    if primary and primary[0]["virtual"]:
        notes.append("出网网卡看起来是虚拟网卡(VPN/虚拟机)。穿透数据里的 MAC 会是它的,换 VPN 或重装会变。")
    if any(not d["serial"] for d in info["disks"]):
        notes.append("有硬盘读不到序列号(虚拟机/部分 NVMe 常见),采集表的硬盘字段请按期货公司要求处理。")
    if not info["vnpy_ctp"]:
        notes.append("这台机器没有安装 vnpy_ctp,采集表里的\"软件版本\"项请先装好再核对。")
    if not info["public_ip"]:
        notes.append("未查询公网 IP(加 --public-ip 查询)。家用宽带公网 IP 可能会变,先问期货公司是否要求固定。")
    return notes


def render_text(info: dict) -> str:
    """生成给人看的文本,字段顺序贴近常见的终端信息采集表。"""
    lines = [f"终端信息采集结果({info['collected_at']})", "=" * 48,
             f"主机名:{info['hostname']}",
             f"操作系统:{info['os']['name']} {info['os']['version']}(build {info['os']['build']},{info['os']['arch']})"]
    machine = info.get("machine") or {}
    if machine:
        lines.append(f"整机:{machine.get('manufacturer', '')} {machine.get('model', '')}(内存 {machine.get('memory_gb', 0)} GB)")
    for cpu in info["cpu"]:
        lines.append(f"CPU:{cpu['name']}(ID {cpu['processor_id']})")
    for board in info["baseboard"]:
        lines.append(f"主板:{board['manufacturer']} {board['product']}(序列号 {board['serial'] or '读不到'})")
    lines.append(f"BIOS 序列号:{info['bios_serial'] or '读不到'}")
    for disk in info["disks"]:
        lines.append(f"硬盘:{disk['model']} {disk['size_gb']}GB(序列号 {disk['serial'] or '读不到'})")
    lines.append("网卡:")
    for adapter in info["adapters"]:
        tags = [t for t, on in (("出网", adapter["primary"]), ("疑似虚拟", adapter["virtual"])) if on]
        tag = f" [{'/'.join(tags)}]" if tags else ""
        lines.append(f"  - {adapter['description']}{tag}")
        lines.append(f"    MAC:{adapter['mac'] or '无'}  IPv4:{', '.join(adapter['ipv4']) or '无'}"
                     f"  网关:{', '.join(adapter['gateway']) or '无'}")
    lines.append(f"出网内网 IP:{info['outbound_ip'] or '未知'}(路由目标:{info.get('route_target', '互联网')})")
    lines.append(f"公网 IP:{info['public_ip'] or '(未查询)'}")
    lines.append(f"软件环境:Python {info['python']}  vnpy {info['vnpy'] or '未装'}  vnpy_ctp {info['vnpy_ctp'] or '未装'}")
    notes = warnings_for(info)
    if notes:
        lines.append("")
        lines.append("需要留意:")
        lines.extend(f"  ! {n}" for n in notes)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本")
    parser.add_argument("--public-ip", action="store_true", help="额外查询公网 IP(会访问第三方服务 api.ipify.org)")
    parser.add_argument("--target", default="", help="判断出网网卡所用的目标,如期货公司交易前置 1.2.3.4:30001(默认按互联网出口,不发包)")
    parser.add_argument("--out", default="", help="同时把结果写入这个文件(UTF-8)")
    args = parser.parse_args(argv)
    # 非 UTF-8 控制台(管道/英文系统)下中文输出不应让脚本崩溃
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")

    if sys.platform != "win32":
        print("本脚本只支持 Windows(运行交易程序的那台机器)。", file=sys.stderr)
        return 2

    info = collect(with_public_ip=args.public_ip, target=args.target)
    text = json.dumps(info, ensure_ascii=False, indent=2) + "\n" if args.json else render_text(info)
    print(text, end="")
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
