# NOTICE:派生自 vnpy_webtrader

`dashboard/app.py` 的应用骨架(FastAPI 应用的组织方式、静态页托管的写法)派生自 **vnpy_webtrader 1.1.0** 的 `web.py`。
上游以 MIT 许可证发布,按其要求在此保留版权声明与许可证全文。

## 上游信息

| 项 | 值 |
|---|---|
| 项目 | vnpy_webtrader(VeighNa 官方 Web 服务模块) |
| 源码 | https://github.com/vnpy/vnpy_webtrader |
| 版本 | 1.1.0 |
| 安装包 SHA-256 | `cf5b81268767ca20069b2073a32abacfe2eac21c3b09486dec47abc171ff0b16`(`vnpy_webtrader-1.1.0-py3-none-any.whl`) |
| 版权人 | Xiaoyou Chen |
| 核对日期 | 2026-11-12 |

## 对上游的改动(相对 1.1.0)

| 上游内容 | 处置 |
|---|---|
| FastAPI 应用骨架、`GET /` 托管静态页 | 保留并改造:静态托管只服务构建清单内的文件,带 SPA 回退 |
| `to_dict` 数据对象序列化 | 不用:响应走区块外壳与净化出口 |
| JWT 登录(`POST /token`、`SECRET_KEY = "test"`、passlib、python-jose、python-multipart) | **删除**,改为令牌文件 + 常量时间比较(`dashboard/auth.py`) |
| `POST /tick/{vt_symbol}`(订阅行情) | **删除** |
| `POST /order`、`DELETE /order/{id}` | **删除,永不恢复** |
| `GET /tick` `/order` `/trade` `/position` `/account` `/contract` | 不用:阶段②改为读快照文件的 `GET /api/live` |
| WebSocket `/ws/`、广播、`rpc_callback`、`RpcClient` | **删除** |
| `engine.py` 的 `WebEngine` | 不引入,不安装进交易进程 |
| `ui/widget.py`、`static/index.html`、图标 | 不引入 |
| 对 `vnpy.*` 的全部 import | **删除** |

逐项理由见 `docs/adr/20261112-status-dashboard.md` 的『fork 范围』一节。

## 许可证全文(上游)

```
The MIT License (MIT)

Copyright (c) 2015-present, Xiaoyou Chen

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 上游更新

不跟踪上游。升级 `vnpy` 大版本时顺带复查上游是否有安全修复,并更新本文件的版本与 SHA-256 记录。
