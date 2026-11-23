"""并发测试：更正与复判在多线程下同时发生。

验收：
- 无数据库锁错误、无丢失更新；
- 最终当前结果基于最新修正范围；
- 被并发更正命中的旧运行一律 superseded，当前批次处于待重算或已重算状态；
- 每个版本的见证值都能实际达到所声称的件数。
"""

import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path

from src.domain.database import Database
from src.domain.service import RejudgmentService

READING_CSV = """批次编号,产品,规格下限,规格上限,零件编号,读数,器具编号,测量时间
B-CONC,轴套,5.00,5.20,P1,5.10,QC-900,2026-11-23 08:05:00
B-CONC,轴套,5.00,5.20,P2,5.05,QC-900,2026-11-23 08:05:00
B-CONC,轴套,5.00,5.20,P3,5.15,QC-900,2026-11-23 08:05:00
B-CONC,轴套,5.00,5.20,P4,5.12,QC-900,2026-11-23 08:05:00
"""
CORRECTION_V1 = """器具编号,时段开始,时段结束,修正下限,修正上限,来源
QC-900,2026-11-23 08:00:00,2026-11-23 10:00:00,-0.05,0.05,v1
"""
CORRECTION_V2 = """器具编号,时段开始,时段结束,修正下限,修正上限,来源
QC-900,2026-11-23 08:00:00,2026-11-23 10:00:00,-0.15,0.15,v2
"""
CORRECTION_V3 = """器具编号,时段开始,时段结束,修正下限,修正上限,来源
QC-900,2026-11-23 08:00:00,2026-11-23 10:00:00,-0.20,0.20,v3
"""


class ConcurrencyTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'c.db')
        self.svc = RejudgmentService(self.db)
        self.batch_id = self.svc.import_readings(READING_CSV, 'r.csv', 'setup')
        self.svc.import_corrections(CORRECTION_V1, 'c1.csv', 'setup')
        self.errors = []

    def tearDown(self):
        self.tmp.cleanup()

    def test_correction_vs_recompute_race(self):
        barrier = threading.Barrier(2)

        def recompute_worker():
            try:
                for _ in range(15):
                    barrier.wait()
                    self.svc.recompute_batch(self.batch_id, '复判线程')
            except Exception as exc:  # noqa: BLE001
                self.errors.append(('recompute', repr(exc)))

        def correction_worker():
            payloads = [CORRECTION_V2, CORRECTION_V3]
            try:
                for payload in payloads:
                    barrier.wait()
                    self.svc.import_corrections(payload, 'late.csv', '校准组')
                # 剩余轮次只做重算前等待，保证两边轮数一致
                for _ in range(13):
                    barrier.wait()
            except Exception as exc:  # noqa: BLE001
                self.errors.append(('correction', repr(exc)))

        t1 = threading.Thread(target=recompute_worker)
        t2 = threading.Thread(target=correction_worker)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(self.errors, [])

        # 所有更正落库后再重算一次，当前结果必须反映 v3 范围
        self.svc.recompute_batch(self.batch_id, '收尾')
        detail = self.svc.get_batch_detail(self.batch_id)
        self.assertEqual(detail['latest_run']['status'], 'current')
        version = detail['versions'][0]
        self.assertEqual((version['range_lower'], version['range_upper']),
                         ('-0.20', '0.20'))

        # 见证值可验证：按声称的修正量代入，合格件数必须等于声称件数
        pieces = {p['piece_no']: Decimal(p['reading']) for p in detail['pieces']}
        lo, hi = Decimal('5.00'), Decimal('5.20')

        def qualified(witness):
            c = Decimal(witness)
            return sum(1 for x in pieces.values() if lo <= x + c <= hi)

        self.assertEqual(qualified(version['min_witness']), version['min_count'])
        self.assertEqual(qualified(version['max_witness']), version['max_count'])

        # 历史链：同一器具时段有 3 个修正版本，前两个已被取代
        conn = self.db.connect()
        rows = conn.execute(
            'SELECT version, superseded_at IS NOT NULL AS old FROM corrections'
            ' WHERE tool_id="QC-900" ORDER BY version').fetchall()
        self.assertEqual([(r['version'], r['old']) for r in rows],
                         [(1, 1), (2, 1), (3, 0)])

        # 运行链完整：除当前运行外全部 superseded（含被并发更正打断的旧结果）
        runs = detail['runs']
        self.assertEqual(sum(1 for r in runs if r['status'] == 'current'), 1)
        self.assertTrue(all(r['status'] in ('current', 'superseded') for r in runs))
        self.assertGreaterEqual(len(runs), 4)

        # 每个真正完成计算的运行都保留自己的结果版本可追查；挂起占位行允许无版本
        for run in runs:
            versions = conn.execute(
                'SELECT COUNT(*) AS n FROM rejudgment_versions WHERE run_id=?',
                (run['id'],)).fetchone()['n']
            if run['finished_at'] is not None and run['detail'] and '并入运行' not in run['detail']:
                self.assertGreater(versions, 0)


if __name__ == '__main__':
    unittest.main()
