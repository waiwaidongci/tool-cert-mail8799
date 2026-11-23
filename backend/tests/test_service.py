"""服务层端到端测试：导入 → 待核 → 补录 → 复判 → 更正 → 重算 → 并发失效。"""

import tempfile
import unittest
from pathlib import Path

from src.domain.database import Database
from src.domain.service import RejudgmentService

READING_CSV = """批次编号,产品,规格下限,规格上限,零件编号,读数,器具编号,测量时间
B2026-1123-01,销轴,9.90,10.10,P1,10.00,QC-101,2026-11-23 08:05:00
B2026-1123-01,销轴,9.90,10.10,P2,9.95,QC-101,2026-11-23 08:05:00
B2026-1123-01,销轴,9.90,10.10,P3,9.90,QC-101,2026-11-23 09:10:00
B2026-1123-01,销轴,9.90,10.10,P4,9.95,QC-204,2026-11-23 08:40:00
"""

CORRECTION_MORNING = """器具编号,时段开始,时段结束,修正下限,修正上限,来源
QC-101,2026-11-23 08:00:00,2026-11-23 10:00:00,-0.10,0.10,校准组补录
QC-204,2026-11-23 08:00:00,2026-11-23 10:00:00,-0.05,0.15,校准组补录
"""

CORRECTION_CORRECTED = """器具编号,时段开始,时段结束,修正下限,修正上限,来源
QC-101,2026-11-23 08:00:00,2026-11-23 10:00:00,-0.20,0.20,校准组更正
"""


class ServiceFlowTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'test.db')
        self.svc = RejudgmentService(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_flow_and_audit_trail(self):
        actor = '计量员甲'
        batch_id = self.svc.import_readings(READING_CSV, 'readings.csv', actor)

        # 导入后最新运行为 pending（尚未复判）
        detail = self.svc.get_batch_detail(batch_id)
        self.assertEqual(detail['latest_run']['status'], 'pending')
        self.assertEqual(len(detail['pieces']), 4)

        # 首次重算：没有任何修正范围 → 全部待核
        run1 = self.svc.recompute_batch(batch_id, actor)
        detail = self.svc.get_batch_detail(batch_id)
        self.assertEqual(detail['latest_run']['status'], 'pending')
        self.assertTrue(all(v['status'] == 'pending' for v in detail['versions']))
        self.assertIsNone(detail['versions'][0]['max_count'])

        # 补录修正范围后，受影响批次自动失效并重新挂 pending
        outcome = self.svc.import_corrections(CORRECTION_MORNING, 'corrections.csv', actor)
        self.assertEqual(outcome['inserted'], 2)
        self.assertEqual(outcome['affected_batch_ids'], [batch_id])
        detail = self.svc.get_batch_detail(batch_id)
        self.assertEqual(detail['latest_run']['status'], 'pending')
        self.assertEqual(detail['latest_run']['reason'], 'correction')

        # 重算 → current，逐组极值
        self.svc.recompute_batch(batch_id, actor)
        detail = self.svc.get_batch_detail(batch_id)
        self.assertEqual(detail['latest_run']['status'], 'current')
        # 更正后重算：此前的全待核运行已被取代但仍留档，只供追查
        statuses = {r['status'] for r in detail['runs']}
        self.assertIn('superseded', statuses)
        groups = {(v['tool_id'], v['group_piece_count']): v for v in detail['versions']}
        qc101 = groups[('QC-101', 3)]  # P1/P2/P3 同在 08:00-10:00 时段，共用修正量
        self.assertEqual(qc101['max_count'], 3)
        # 旧范围 -.10..10：c=-.10 时仅 P1 合格（P2、P3 超下限），最小 1 件
        self.assertEqual(qc101['min_count'], 1)
        qc204 = groups[('QC-204', 1)]
        self.assertEqual(qc204['group_piece_count'], 1)

        # 迟到更正：范围放宽到 -.20..20，最小件数降为 0
        outcome = self.svc.import_corrections(CORRECTION_CORRECTED, 'corrections-v2.csv', actor)
        self.assertEqual(outcome['corrected'], 1)
        detail = self.svc.get_batch_detail(batch_id)
        self.assertEqual(detail['latest_run']['status'], 'pending')
        # 修正历史留版本
        conn = self.db.connect()
        history = conn.execute(
            'SELECT version, range_lower, range_upper, superseded_at IS NOT NULL AS old'
            ' FROM corrections WHERE tool_id="QC-101" ORDER BY version').fetchall()
        self.assertEqual([(r['version'], r['old']) for r in history], [(1, 1), (2, 0)])

        self.svc.recompute_batch(batch_id, actor)
        detail = self.svc.get_batch_detail(batch_id)
        self.assertEqual(detail['latest_run']['status'], 'current')
        qc101 = next(v for v in detail['versions']
                     if v['tool_id'] == 'QC-101' and v['group_piece_count'] == 3)
        # 范围放宽到 -.20..20 后，左端点开区间无合格件，最小降为 0，最大仍为 3
        self.assertEqual(qc101['min_count'], 0)
        self.assertEqual(qc101['max_count'], 3)

        # 审计链完整
        actions = [e['action'] for e in self.svc.list_audit()]
        self.assertIn('import_batch', actions)
        self.assertIn('correction_add', actions)
        self.assertIn('correction_replace', actions)
        self.assertEqual(actions.count('rejudge_start'), 4)  # 导入自动 + 三次重算

    def test_idempotent_duplicate_correction(self):
        self.svc.import_readings(READING_CSV, 'r.csv', 'a')
        self.svc.import_corrections(CORRECTION_MORNING, 'c.csv', 'a')
        again = self.svc.import_corrections(CORRECTION_MORNING, 'c.csv', 'a')
        self.assertEqual((again['inserted'], again['corrected']), (0, 0))
        conn = self.db.connect()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM corrections').fetchone()[0], 2)

    def test_duplicate_batch_rejected(self):
        self.svc.import_readings(READING_CSV, 'r.csv', 'a')
        with self.assertRaises(ValueError):
            self.svc.import_readings(READING_CSV, 'r.csv', 'a')

    def test_missing_range_batch_pending_but_other_groups_ok(self):
        # 只补 QC-101，QC-204 仍缺：批次整体 pending，但已覆盖组给出极值
        self.svc.import_readings(READING_CSV, 'r.csv', 'a')
        batch_id = self.svc.list_batches()[0]['id']
        partial = ('器具编号,时段开始,时段结束,修正下限,修正上限,来源\n'
                   'QC-101,2026-11-23 08:00:00,2026-11-23 10:00:00,-0.10,0.10,组\n')
        self.svc.import_corrections(partial, 'c.csv', 'a')
        self.svc.recompute_batch(batch_id, 'a')
        detail = self.svc.get_batch_detail(batch_id)
        self.assertEqual(detail['latest_run']['status'], 'pending')
        by_tool = {}
        for v in detail['versions']:
            by_tool.setdefault(v['tool_id'], []).append(v)
        self.assertTrue(all(v['status'] == 'ok' for v in by_tool['QC-101']))
        self.assertTrue(all(v['status'] == 'pending' for v in by_tool['QC-204']))


if __name__ == '__main__':
    unittest.main()
