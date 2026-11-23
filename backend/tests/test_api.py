"""端到端 API 测试：真实 HTTP 启动服务，覆盖鉴权、RBAC、导入、更正、重算、班报。"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

from src.domain.database import Database
from src.domain.service import RejudgmentService
from src.web.server import AppState, build_server
from src.web.auth import Authenticator
from src.main import DEFAULT_ACCOUNTS

PORT = 19377
BASE = f'http://127.0.0.1:{PORT}'

READING_CSV = """批次编号,产品,规格下限,规格上限,零件编号,读数,器具编号,测量时间
B-E2E,滚轮,2.00,2.20,K1,2.10,QC-77,2026-11-23 08:05:00
B-E2E,滚轮,2.00,2.20,K2,2.05,QC-77,2026-11-23 08:05:00
B-E2E,滚轮,2.00,2.20,K3,2.18,QC-77,2026-11-23 08:05:00
"""
CORR_V1 = """器具编号,时段开始,时段结束,修正下限,修正上限,来源
QC-77,2026-11-23 08:00:00,2026-11-23 10:00:00,-0.05,0.05,初版
"""
CORR_V2 = """器具编号,时段开始,时段结束,修正下限,修正上限,来源
QC-77,2026-11-23 08:00:00,2026-11-23 10:00:00,-0.15,0.15,迟到更正
"""


class ApiEndToEndTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db = Database(Path(cls.tmp.name) / 'e2e.db')
        auth = Authenticator(DEFAULT_ACCOUNTS, 'e2e-test-secret')
        cls.state = AppState(RejudgmentService(db), auth)
        cls.server = build_server('127.0.0.1', PORT, cls.state)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def call(self, method, path, token=None, payload=None):
        data = json.dumps(payload).encode('utf-8') if payload is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header('Content-Type', 'application/json')
        if token:
            req.add_header('Authorization', f'Bearer {token}')
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode('utf-8'))

    def login(self, username, password):
        status, data = self.call('POST', '/api/login',
                                 payload={'username': username, 'password': password})
        self.assertEqual(status, 200)
        return data['token']

    def test_full_e2e(self):
        # 未登录被拦截
        status, _ = self.call('GET', '/api/batches')
        self.assertEqual(status, 401)

        # 错误密码
        status, data = self.call('POST', '/api/login',
                                 payload={'username': 'analyst', 'password': 'wrong'})
        self.assertEqual(status, 401)

        analyst = self.login('analyst', 'analyst-demo-2611')
        viewer = self.login('viewer', 'viewer-demo-2611')
        admin = self.login('admin', 'admin-demo-2611')

        # 只读角色不能导入
        status, _ = self.call('POST', '/api/import/readings', token=viewer,
                              payload={'filename': 'r.csv', 'content': READING_CSV})
        self.assertEqual(status, 401)

        # 分析员导入读数
        status, data = self.call('POST', '/api/import/readings', token=analyst,
                                 payload={'filename': 'readings.csv', 'content': READING_CSV})
        self.assertEqual(status, 201)
        batch_id = data['batch_id']

        # 重复导入被拒绝
        status, data = self.call('POST', '/api/import/readings', token=analyst,
                                 payload={'filename': 'r2.csv', 'content': READING_CSV})
        self.assertEqual(status, 400)
        self.assertIn('已导入', data['error'])

        # 班报列表：新批次 pending
        status, data = self.call('GET', '/api/batches', token=viewer)
        self.assertEqual(status, 200)
        batch = next(b for b in data['batches'] if b['id'] == batch_id)
        self.assertEqual(batch['latest_run']['status'], 'pending')

        # 无修正范围重算 → 仍待核
        self.call('POST', f'/api/batches/{batch_id}/recompute', token=analyst, payload={})
        status, detail = self.call('GET', f'/api/batches/{batch_id}/detail', token=viewer)
        self.assertEqual(detail['latest_run']['status'], 'pending')
        self.assertTrue(all(v['status'] == 'pending' for v in detail['versions']))

        # 补录 v1 后重算 → current
        self.call('POST', '/api/import/corrections', token=analyst,
                  payload={'filename': 'c1.csv', 'content': CORR_V1})
        self.call('POST', f'/api/batches/{batch_id}/recompute', token=analyst, payload={})
        status, detail = self.call('GET', f'/api/batches/{batch_id}/detail', token=viewer)
        self.assertEqual(detail['latest_run']['status'], 'current')
        v1_result = detail['versions'][0]
        self.assertEqual((v1_result['range_lower'], v1_result['range_upper']),
                         ('-0.05', '0.05'))

        # 迟到更正 v2：批次自动回到待重算，旧运行留档
        status, outcome = self.call('POST', '/api/import/corrections', token=analyst,
                                    payload={'filename': 'c2.csv', 'content': CORR_V2})
        self.assertEqual(status, 200)
        self.assertEqual(outcome['corrected'], 1)
        self.assertEqual(outcome['affected_batch_ids'], [batch_id])
        status, detail = self.call('GET', f'/api/batches/{batch_id}/detail', token=viewer)
        self.assertEqual(detail['latest_run']['status'], 'pending')
        self.assertTrue(any(r['status'] == 'superseded' for r in detail['runs']))

        # 重算后极值变化，且见证值经独立复核确实达到声称件数
        self.call('POST', f'/api/batches/{batch_id}/recompute', token=analyst, payload={})
        status, detail = self.call('GET', f'/api/batches/{batch_id}/detail', token=viewer)
        self.assertEqual(detail['latest_run']['status'], 'current')
        group = detail['versions'][0]
        self.assertEqual((group['range_lower'], group['range_upper']), ('-0.15', '0.15'))
        readings = {p['piece_no']: Decimal(p['reading']) for p in detail['pieces']}
        lo, hi = Decimal('2.00'), Decimal('2.20')

        def count_qualified(witness):
            c = Decimal(witness)
            return sum(1 for x in readings.values() if lo <= x + c <= hi)

        self.assertEqual(count_qualified(group['min_witness']), group['min_count'])
        self.assertEqual(count_qualified(group['max_witness']), group['max_count'])
        # v2 范围放宽后最小件数应小于等于 v1
        self.assertLessEqual(group['min_count'], v1_result['min_count'])

        # 运行留痕与审计：仅主管可看审计
        status, _ = self.call('GET', '/api/audit', token=analyst)
        self.assertEqual(status, 401)
        status, audit = self.call('GET', '/api/audit', token=admin)
        self.assertEqual(status, 200)
        actions = {e['action'] for e in audit['events']}
        self.assertTrue(
            {'import_batch', 'correction_add', 'correction_replace',
             'rejudge_start', 'rejudge_finish'}.issubset(actions))

        # 登出后令牌失效
        self.call('POST', '/api/logout', token=viewer, payload={})
        status, _ = self.call('GET', '/api/me', token=viewer)
        self.assertEqual(status, 401)

        # 静态页面可访问
        with urllib.request.urlopen(BASE + '/', timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn('超差器具零件复判班报', resp.read().decode('utf-8'))

    def test_bad_csv_returns_row_error(self):
        analyst = self.login('analyst', 'analyst-demo-2611')
        bad = '批次编号,产品,规格下限,规格上限,零件编号,读数,器具编号,测量时间\n' \
              'B-BAD,件,1.0,2.0,X1,not-a-number,Q1,2026-11-23 08:00:00\n'
        status, data = self.call('POST', '/api/import/readings', token=analyst,
                                 payload={'filename': 'bad.csv', 'content': bad})
        self.assertEqual(status, 400)
        self.assertIn('第 2 行', data['error'])


if __name__ == '__main__':
    unittest.main()
