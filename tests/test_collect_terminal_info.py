"""终端信息采集脚本的离线测试:只测解析与整理逻辑,不实际调用 PowerShell。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import collect_terminal_info as ti  # noqa: E402


def test_as_list_normalizes_powershell_json_shapes():
    assert ti.as_list({"a": 1}) == [{"a": 1}]
    assert ti.as_list([{"a": 1}, "junk", {"b": 2}]) == [{"a": 1}, {"b": 2}]
    assert ti.as_list("") == [] and ti.as_list(None) == []


def test_normalize_adapters_marks_primary_and_virtual():
    raw = [
        {"Description": "Intel Ethernet", "MACAddress": "AA:BB:CC:DD:EE:FF",
         "IPAddress": ["192.168.1.10", "fe80::1"], "DefaultIPGateway": ["192.168.1.1"]},
        {"Description": "VMware Virtual Ethernet Adapter", "MACAddress": "00:50:56:00:00:01",
         "IPAddress": "192.168.56.1", "DefaultIPGateway": None},
    ]
    adapters = ti.normalize_adapters(raw, "192.168.1.10")
    assert adapters[0]["primary"] and not adapters[0]["virtual"]
    assert adapters[0]["mac"] == "AA-BB-CC-DD-EE-FF"
    assert adapters[0]["ipv4"] == ["192.168.1.10"] and adapters[0]["ipv6"] == ["fe80::1"]
    assert adapters[1]["virtual"] and not adapters[1]["primary"] and adapters[1]["ipv4"] == ["192.168.56.1"]


def make_info(**kw):
    info = {
        "collected_at": "2026-11-11T09:00:00", "hostname": "H",
        "os": {"name": "Win", "version": "10", "build": "1", "arch": "64"},
        "cpu": [{"name": "CPU", "processor_id": "BFEBFBFF"}], "baseboard": [], "bios_serial": "",
        "disks": [{"model": "NVMe", "serial": "", "size_gb": 512}],
        "adapters": ti.normalize_adapters(
            [{"Description": "Intel", "MACAddress": "AA:BB:CC:DD:EE:FF", "IPAddress": ["10.0.0.2"],
              "DefaultIPGateway": ["10.0.0.1"]}], "10.0.0.2"),
        "outbound_ip": "10.0.0.2", "public_ip": "", "python": "3.13", "vnpy": "4.5.0", "vnpy_ctp": "",
    }
    info.update(kw)
    return info


def test_warnings_cover_missing_ctp_serial_and_public_ip():
    notes = "\n".join(ti.warnings_for(make_info()))
    assert "vnpy_ctp" in notes and "序列号" in notes and "公网 IP" in notes


def test_warning_for_virtual_primary_adapter():
    info = make_info(adapters=ti.normalize_adapters(
        [{"Description": "WireGuard Tunnel", "MACAddress": "", "IPAddress": ["10.9.0.2"],
          "DefaultIPGateway": []}], "10.9.0.2"))
    assert "虚拟网卡" in "\n".join(ti.warnings_for(info))


def test_render_text_contains_mac_and_flags():
    text = ti.render_text(make_info())
    assert "AA-BB-CC-DD-EE-FF" in text and "[出网]" in text and "需要留意" in text


def test_run_powershell_returns_empty_when_command_fails(monkeypatch):
    def boom(*a, **k):
        raise OSError("no powershell")
    monkeypatch.setattr(ti.subprocess, "run", boom)
    assert ti.run_powershell("Get-Date") == []


def test_parse_target_variants():
    assert ti.parse_target("") == ("8.8.8.8", 80)
    assert ti.parse_target("tcp://1.2.3.4:30001") == ("1.2.3.4", 30001)
    assert ti.parse_target("1.2.3.4") == ("1.2.3.4", 80)


def test_warning_when_powershell_queries_all_failed():
    notes = "\n".join(ti.warnings_for(make_info(ps_ok=False)))
    assert "WMI" in notes
