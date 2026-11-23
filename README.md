# 计量室超差器具零件复判班报

导入逐件读数、规格上下限与器具分时段修正范围，按「同批同器具同一时段共用一个修正量」复判超差器具测过的零件，算出**可同时合格的最少与最多件数**，并各给出一个能达到该件数的修正量取值。迟到修正记录按测量时段补入或更正；更正与复判并发时，旧结果只供追查，当前判定等待受影响批次重算。

澄岭精密零件厂 · 质量计量室

## 业务口径

- 每件读数 `xᵢ`、规格 `[L, U]`，修正后读数为 `xᵢ + c`，该件可接受的修正区间为 `[L − xᵢ, U − xᵢ]`（闭区间）。
- 修正量 `c` 只能在器具该测量时段的修正范围 `[lo, hi]` 内取值。
- 同批、同器具、同一修正时段内的所有零件共用一个 `c`，求一个 `c` 使合格件数分别达到**可能的最大值**与**可能的最小值**，并各返回一个见证取值。
- 算法用扫描线（事件点 + 相邻事件点开区间，开区间内覆盖数恒定、取中点为见证点），全程 `Decimal` 精确运算；并以 400 组随机实例对暴力枚举交叉验证。
- 修正范围按「器具 + 时段」维护版本：新时段为补录；同器具同时段且范围不同为更正，旧版本标记失效并留档，完全相同的迟到记录幂等忽略。
- 缺修正范围的器具时段标记**待核**，批次结论为待核；补入后受影响批次自动回到待重算。
- 更正与复判并发：所有写操作经 `BEGIN IMMEDIATE` 串行化；更正提交即把受影响批次的当前运行置 `superseded` 并挂起 `pending`，旧版本结果保留只供追查，重算后才产生新的当前判定。

## 运行

仅需 Python 3.11，**无第三方依赖**（标准库 `http.server` + `sqlite3`）。

```bash
cd backend
python3 -m src.main
```

浏览器访问 `http://127.0.0.1:19283`。可选环境变量见 `.env.example`：`REJUDGE_DB`、`REJUDGE_HOST`、`REJUDGE_PORT`、`REJUDGE_SECRET`、`REJUDGE_SEED`。

单独播种/重置演示数据（可安全重复执行）：

```bash
python3 -m scripts.seed_demo
```

| 角色 | 演示账号 / 密码 | 权限 |
|---|---|---|
| 复核主管 | `admin` / `admin-demo-2611` | 全部操作，含审计查看 |
| 分析员 | `analyst` / `analyst-demo-2611` | 导入、更正、复判、查看 |
| 只读查看 | `viewer` / `viewer-demo-2611` | 查看班报与结果 |

## CSV 格式

读数批次（UTF-8，同批共用规格上下限）：

```
批次编号,产品,规格下限,规格上限,零件编号,读数,器具编号,测量时间
B2026-1123-01,销轴,9.90,10.10,P1,10.00,QC-101,2026-11-23 08:05:00
```

修正范围（迟到记录按器具+时段补入或更正）：

```
器具编号,时段开始,时段结束,修正下限,修正上限,来源
QC-101,2026-11-23 08:00:00,2026-11-23 10:00:00,-0.12,0.12,校准实验室迟到更正
```

时间支持 `YYYY-MM-DD HH:MM:SS`、`YYYY-MM-DDTHH:MM:SS`、`YYYY-MM-DD`。字段校验错误带 CSV 行号返回。

## 页面与接口

- **班报总览**：按批次卡片显示当前/待核状态；点开看每个器具时段的分组、修正范围、最少/最多件数与见证取值、逐件读数。
- **导入与更正**：粘贴或选择 CSV 上传；展示现行与历史修正范围版本。
- **运行留痕**：每次复判一条运行记录，`current / pending / superseded / running / failed`，旧结果保留可追查。
- **审计记录**：导入、补录、更正、复判开始/完成全程留痕（仅主管）。

REST 接口（`Bearer` 令牌）：`POST /api/login`、`POST /api/logout`、`GET /api/me`、`GET /api/batches`、`GET /api/batches/{id}/detail`、`POST /api/import/readings`、`POST /api/import/corrections`、`POST /api/batches/{id}/recompute`、`GET /api/runs`、`GET /api/corrections`、`GET /api/audit`、`GET /healthz`。

## 测试

```bash
cd backend
python3 -m unittest discover -s tests -v
```

| 测试文件 | 覆盖 |
|---|---|
| `test_interval.py` | 极值算法固定用例、闭区间端点、零覆盖区、点范围，及 400 组随机暴力交叉验证 |
| `test_service.py` | 导入→待核→补录→复判→迟到更正→重算全链路、幂等、去重、部分待核 |
| `test_concurrency.py` | 30 个并发更正/复判动作的多线程竞争、串行化、版本留痕、见证值复核 |
| `test_api.py` | 真实 HTTP 端到端：登录鉴权、角色限制、CSV 行号错误、完整业务流 |

## 结构

```
backend/
├── src/
│   ├── main.py                 入口、账号与装配
│   ├── domain/
│   │   ├── interval.py         极值扫描线算法（Decimal）
│   │   ├── importer.py         CSV 解析与字段校验
│   │   ├── database.py         SQLite 表结构与连接（WAL）
│   │   └── service.py          导入、补录/更正、复判、并发与版本留痕
│   └── web/
│       ├── auth.py             HMAC 令牌与账号
│       ├── server.py           HTTP 路由与静态资源
│       └── static/             班报页面（原生 HTML/CSS/JS）
├── scripts/seed_demo.py        幂等演示数据
└── tests/                      单元、服务、并发、端到端测试
```

数据存于 SQLite（默认 `backend/data/rejudgment.db`，WAL 模式）。删除该文件后重启即可从零开始。
