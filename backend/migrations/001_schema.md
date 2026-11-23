# 001 初始表结构

应用首次启动时由 SQLAlchemy 无关的标准库 `sqlite3` 直接建表（`CREATE TABLE IF NOT EXISTS`），WAL 模式并开启外键约束。读数、规格、修正量等精确数值以 TEXT 存 `Decimal` 字符串。

- `batches`：读数批次，按批次编号与内容 SHA-256 指纹去重，保存同批共用规格上下限。
- `pieces`：逐件读数，批次内零件编号、序号唯一。
- `measurements`：每件的器具编号与测量时间，一件一条。
- `corrections`：器具分时段修正范围，按「器具 + 时段 + 版本」留痕；`superseded_at` 非空即被更正的历史版本。
- `rejudgment_runs`：复判运行，状态 `running / current / pending / superseded / failed`。更正命中时旧运行置 `superseded` 并挂 `pending`，旧结果保留只供追查。
- `rejudgment_versions`：每次运行逐组（器具 + 修正时段）的最少/最多件数与见证取值；缺修正范围的组为 `pending`，极值字段为空。
- `audit_events`：登录以外的导入、补录、更正、复判开始/完成动作留痕。

升级字段需显式迁移；当前版本仅在表缺失时补建。
