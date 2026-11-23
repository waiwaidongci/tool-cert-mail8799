"""可安全重复执行的演示播种。

剧情：
- B2026-1123-01 销轴批次（QC-101 早/午两段、QC-204 一段）已导入并完成复判；
- B2026-1123-02 衬套批次刚导入，QC-305 的修正范围缺失，处于待核；
- QC-101 早班修正范围已被迟到记录更正一次（v1 留档、v2 当前）；
- 班报页面因此同时呈现 current、pending、superseded 三种状态。
"""

from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'

READINGS_B1 = """批次编号,产品,规格下限,规格上限,零件编号,读数,器具编号,测量时间
B2026-1123-01,销轴,9.90,10.10,P1,10.00,QC-101,2026-11-23 08:05:00
B2026-1123-01,销轴,9.90,10.10,P2,9.95,QC-101,2026-11-23 08:12:00
B2026-1123-01,销轴,9.90,10.10,P3,10.08,QC-101,2026-11-23 08:25:00
B2026-1123-01,销轴,9.90,10.10,P4,10.02,QC-101,2026-11-23 08:40:00
B2026-1123-01,销轴,9.90,10.10,P5,9.92,QC-101,2026-11-23 12:10:00
B2026-1123-01,销轴,9.90,10.10,P6,10.05,QC-101,2026-11-23 12:35:00
B2026-1123-01,销轴,9.90,10.10,P7,9.98,QC-204,2026-11-23 08:50:00
B2026-1123-01,销轴,9.90,10.10,P8,10.11,QC-204,2026-11-23 09:05:00
"""

READINGS_B2 = """批次编号,产品,规格下限,规格上限,零件编号,读数,器具编号,测量时间
B2026-1123-02,衬套,15.80,16.20,S1,16.00,QC-305,2026-11-23 10:02:00
B2026-1123-02,衬套,15.80,16.20,S2,15.92,QC-305,2026-11-23 10:18:00
B2026-1123-02,衬套,15.80,16.20,S3,16.15,QC-305,2026-11-23 10:41:00
"""

# 早班先到的初版范围（v1，之后被更正）
CORR_QC101_V1 = """器具编号,时段开始,时段结束,修正下限,修正上限,来源
QC-101,2026-11-23 08:00:00,2026-11-23 10:00:00,-0.08,0.08,早班初版
"""

# 迟到补录/更正后的完整修正范围
CORR_CURRENT = """器具编号,时段开始,时段结束,修正下限,修正上限,来源
QC-101,2026-11-23 08:00:00,2026-11-23 10:00:00,-0.12,0.12,校准实验室迟到更正
QC-101,2026-11-23 12:00:00,2026-11-23 14:00:00,-0.10,0.10,午班补录
QC-204,2026-11-23 08:00:00,2026-11-23 10:00:00,-0.06,0.14,班组自校记录
"""


def seed(service, db_path: Path | None = None):
    actor = '系统播种'
    existing = {b['code'] for b in service.list_batches()}

    if 'B2026-1123-01' not in existing:
        b1 = service.import_readings(READINGS_B1, 'demo_readings_b1.csv', actor)
        service.import_corrections(CORR_QC101_V1, 'demo_corr_v1.csv', actor)
        service.recompute_batch(b1, actor)          # 第一次复判（范围不全，部分待核）
        service.import_corrections(CORR_CURRENT, 'demo_corr_current.csv', actor)  # 迟到更正
        service.recompute_batch(b1, actor)          # 受影响批次重算 → current

    if 'B2026-1123-02' not in existing:
        service.import_readings(READINGS_B2, 'demo_readings_b2.csv', actor)
        # 故意不导入 QC-305 修正范围：批次保持待核

    # 落一份可下载的 CSV 模板到数据目录
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    for name, content in (
        ('template_readings.csv', READINGS_B1),
        ('template_corrections.csv', CORR_CURRENT),
    ):
        target = Path(db_path).parent / name
        if not target.exists():
            target.write_text(content, encoding='utf-8')
    return service


if __name__ == '__main__':
    import os
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.domain.database import Database
    from src.domain.service import RejudgmentService

    db_file = Path(os.environ.get('REJUDGE_DB', Path(__file__).resolve().parent.parent / 'data' / 'rejudgment.db'))
    svc = RejudgmentService(Database(db_file))
    seed(svc, db_file)
    print('演示数据已播种：', [b['code'] for b in svc.list_batches()])
