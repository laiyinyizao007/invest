"""框架侧:把候选进程给回的**信号序列**喂给框架自己的回测模板(Issue #29 S1 · P2 T2.2)。

候选只产出「每根 K 线的入场意图」;出场(ATR 移动止损)、1 手、单笔风险预算、当日连亏熔断、成交价与成本
全部由**框架自己的** `TrendAtr15mStrategy` 家族模板决定——候选无从改写,也不在这个进程里。

设计选择(都是为了让评估链路**一行不改**):
- `SignalReplay` 继承 `TrendAtr15mStrategy`,只重写**候选本来就被允许重写**的 `entry_signal`;
  vn.py 回测引擎在每根 K 线前会先设置 `engine.datetime = bar.datetime`,所以这里按它取当根的信号,
  不需要碰 `on_bar` 或任何私有成员。
- 旧模板只在**空仓**时才调用 `entry_signal`(持仓走 `_manage_position`);回放沿用这条:
  信号序列里持仓期间的值根本不会被取到。对**无状态**候选,这与旧路径逐根等价;
  对依赖「被调用了几次」「`self.pos`」的有状态候选不等价——ADR 已知局限。
- runner 与旧路径同签名 `runner(strategy_class, setting, bars, cost_multiplier)`,
  `select_params` / `run_gate` / 随机对照 / `ce.evaluate` 不用改:候选类参数换成代理类,
  遇到代理类就走「起候选进程取信号 → 回放」,遇到别的类(随机对照)原样走引擎。
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable

import research_candidate_process as rcp
from research_candidate_spec import CandidateSpec, framework_setting


class SignalSeriesError(ValueError):
    """信号序列无法与 K 线对齐(时间戳重复)。fail-closed:按时间戳取信号,重复就会错位。"""


def make_replay_class(base: type, signals_by_datetime: dict) -> type:
    """造一个按时间戳回放信号的模板子类。`base` 是框架模板(`TrendAtr15mStrategy`)。"""

    class SignalReplay(base):                       # type: ignore[valid-type, misc]
        _signals = signals_by_datetime

        def entry_signal(self) -> int:
            return self._signals.get(self.cta_engine.datetime, 0)

    return SignalReplay


def fingerprint(bars) -> str:
    """K 线内容指纹(时间戳 + OHLC + 成交量/成交额/持仓量:候选侧 ArrayManager 能看到的全部字段)。
    缓存键用它,而不是「首末时间戳 + 根数」:
    首末与根数相同、中间不同的两组 K 线不得共用一份信号。"""
    digest = hashlib.sha1()
    for bar in bars:
        digest.update((f"{bar.datetime}|{bar.open_price}|{bar.high_price}|{bar.low_price}|{bar.close_price}"
                       f"|{bar.volume}|{bar.turnover}|{bar.open_interest};").encode())
    return digest.hexdigest()


def signals_to_map(bars, signals) -> dict:
    """只存非零信号。信号与 K 线必须**严格对齐**,否则一律拒绝(不截断、不补零):
    长度不等(zip 会静默截断)、取值域越界、时间戳缺失/重复(重复会让「按时间戳取值」与「按序号取值」分叉)。"""
    if len(signals) != len(bars):
        raise SignalSeriesError(f"信号序列长度 {len(signals)} 与 K 线根数 {len(bars)} 不符")
    if any(int(s) not in (-1, 0, 1) for s in signals):
        raise SignalSeriesError("信号取值越界(只允许 -1/0/1)")
    stamps = [b.datetime for b in bars]
    if any(s is None or s != s for s in stamps):
        raise SignalSeriesError("K 线缺少时间戳,无法对齐候选信号")
    if len(set(stamps)) != len(stamps):
        raise SignalSeriesError("K 线时间戳有重复,无法按时间戳对齐候选信号")
    return {dt: int(s) for dt, s in zip(stamps, signals) if s}


def make_signal_runner(*, source: str, spec: CandidateSpec, proxy: type, base: type,
                       engine: Callable, check_setting: Callable[[dict], None],
                       framework_parameters, timeout: float = 600.0, interval: str = "15m") -> Callable:
    """造新路径的 runner。

    - `proxy`:`research_candidate_spec.proxy_class(spec)` 造出的代理类,只用来**认出**「这是候选」;
    - `engine`:旧路径同一个 `research_gate.engine_runner(key)`(真跑 vn.py 引擎);
    - `check_setting`:对**交给框架引擎的有效设置**再过一遍受保护参数检查(类体里写 `risk_budget = 1e9` 也不得生效);
    - 缓存键 = (参数, K 线内容指纹):成本翻倍压力测试与基础回放是同一份信号,只起一次进程。
    """
    cache: dict[str, list[int]] = {}
    fingerprints: dict[int, tuple[list, str]] = {}      # id(bars) → (bars 本身的引用, 指纹):同一份切片只算一次

    def runner(strategy_class: type, setting: dict, bars: list, cost_multiplier: float) -> dict:
        if strategy_class is not proxy:
            return engine(strategy_class, setting, bars, cost_multiplier)
        known = fingerprints.get(id(bars))
        if known is None or known[0] is not bars:
            known = (bars, fingerprint(bars))
            fingerprints[id(bars)] = known
        key = json.dumps([sorted(setting.items()), known[1]], default=str)
        signals = cache.get(key)
        if signals is None:
            signals = rcp.run_candidate_signals(source, spec.class_name, dict(setting), bars,
                                                interval=interval, timeout=timeout)
            cache[key] = signals
        engine_setting = framework_setting(spec, setting, framework_parameters)
        check_setting(engine_setting)
        replay = make_replay_class(base, signals_to_map(bars, signals))
        return engine(replay, engine_setting, bars, cost_multiplier)

    runner.cache = cache                              # 测试里数「起了几次进程」用
    return runner
