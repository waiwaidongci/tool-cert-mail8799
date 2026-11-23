"""区间极值算法测试：固定用例 + 随机数据对暴力枚举交叉验证。"""

import random
import unittest
from decimal import Decimal

from src.domain.interval import Extremum, Interval, solve


def D(value):
    return Decimal(str(value))


class SolveFixedTests(unittest.TestCase):

    def _domain(self):
        return Interval(D('-0.20'), D('0.20'))

    def test_basic_extremes(self):
        # 与演示数据 QC-101 早班一致：可接受区间
        # p1 [-.10,.10] p2 [-.15,.15] p3 [0,.20] p4 [-.05,.15]
        pieces = [
            {'piece_id': 'P1', 'reading': D('10.00'), 'lower': D('9.90'), 'upper': D('10.10')},
            {'piece_id': 'P2', 'reading': D('10.00'), 'lower': D('9.85'), 'upper': D('10.15')},
            {'piece_id': 'P3', 'reading': D('9.90'), 'lower': D('9.90'), 'upper': D('10.10')},
            {'piece_id': 'P4', 'reading': D('9.95'), 'lower': D('9.90'), 'upper': D('10.10')},
        ]
        result = solve(pieces, self._domain())
        self.assertEqual(result.total, 4)
        self.assertEqual(result.eligible, 4)
        # 最大 4 件，c=0（最小位置的最优点）
        self.assertEqual(result.maximum, Extremum(4, D('0.00')))
        self.assertEqual(result.max_qualified_ids, ('P1', 'P2', 'P3', 'P4'))
        # 最小 0 件：c 取范围左端点 -0.20 时无一件合格
        self.assertEqual(result.minimum.count, 0)
        self.assertEqual(result.minimum.witness, D('-0.20'))
        witness = result.minimum.witness
        self.assertTrue(D('-0.20') <= witness <= D('0.20'))
        for row in result.per_piece:
            if row['piece_id'] == 'P3':
                self.assertEqual(row['accept_lo'], D('0.00'))
                self.assertEqual(row['accept_hi'], D('0.20'))

    def test_witness_actually_achieves_counts(self):
        pieces = [
            {'piece_id': f'P{i}', 'reading': D(i), 'lower': D(0), 'upper': D(10)}
            for i in range(5)
        ]
        result = solve(pieces, self._domain())
        for extremum in (result.maximum, result.minimum):
            count = sum(
                1 for p in pieces
                if p['lower'] <= p['reading'] + extremum.witness <= p['upper']
            )
            self.assertEqual(count, extremum.count)

    def test_no_eligible_piece(self):
        # 规格 0..1，读数 5，接受区间 [-5,-4] 与修正范围 [-0.2,0.2] 无交集
        pieces = [{'piece_id': 'X', 'reading': D('5'), 'lower': D('0'), 'upper': D('1')}]
        result = solve(pieces, self._domain())
        self.assertEqual(result.eligible, 0)
        self.assertEqual(result.maximum.count, 0)
        self.assertEqual(result.minimum.count, 0)
        self.assertEqual(result.minimum.witness, D('-0.20'))

    def test_all_always_qualified_min_equals_max(self):
        pieces = [
            {'piece_id': 'A', 'reading': D('1.00'), 'lower': D('0'), 'upper': D('2')},
            {'piece_id': 'B', 'reading': D('1.00'), 'lower': D('0'), 'upper': D('2')},
        ]
        result = solve(pieces, self._domain())
        self.assertEqual(result.maximum.count, 2)
        self.assertEqual(result.minimum.count, 2)
        # 相同件数时取最小位置（范围左端点）
        self.assertEqual(result.minimum.witness, D('-0.20'))

    def test_closed_endpoints_count(self):
        # A 接受区间 [-.20,.20]（恒合格）；B 接受区间 [-.20,.20] 同样恒合格，
        # 此例验证范围端点本身是合法见证点（闭区间）。
        pieces = [
            {'piece_id': 'A', 'reading': D('0.20'), 'lower': D('0'), 'upper': D('0.40')},
            {'piece_id': 'B', 'reading': D('-0.20'), 'lower': D('-0.40'), 'upper': D('0')},
        ]
        result = solve(pieces, self._domain())
        self.assertEqual(result.maximum.count, 2)
        self.assertEqual(result.minimum.count, 2)
        self.assertEqual(result.minimum.witness, D('-0.20'))  # 取最小位置：左端点

        # 只有一件、其区间恰为整个修正范围时，端点见证同样有效
        right = solve([pieces[0]], self._domain())
        self.assertEqual(right.maximum.witness, D('-0.20'))
        self.assertEqual(right.maximum.count, 1)

    def test_endpoint_single_qualified(self):
        # A 仅在右端点 c=0.20 合格；B 仅在左端点 c=-0.20 合格。
        # 两区间在 c=0 相切（闭区间，此时两件都合格），两端开区间各只有 0 件；
        # 但范围端点本身各有 1 件合格，因此最小件数为 1（取左端点见证）。
        pieces = [
            {'piece_id': 'A', 'reading': D('0.20'), 'lower': D('0.20'), 'upper': D('0.40')},
            {'piece_id': 'B', 'reading': D('-0.20'), 'lower': D('-0.40'), 'upper': D('-0.20')},
        ]
        result = solve(pieces, self._domain())
        self.assertEqual(result.maximum.count, 2)
        self.assertEqual(result.maximum.witness, D('0.00'))
        self.assertEqual(result.minimum.count, 1)
        self.assertEqual(result.minimum.witness, D('-0.20'))  # 左端点仅 B 合格

    def test_zero_coverage_gap_inside_domain(self):
        # 合格区间整体落在修正范围内部 [0.05, 0.15]，范围左端点为零覆盖区，最小 0 件。
        pieces = [
            {'piece_id': 'A', 'reading': D('0.00'), 'lower': D('0.05'), 'upper': D('0.15')},
            {'piece_id': 'B', 'reading': D('0.02'), 'lower': D('0.05'), 'upper': D('0.15')},
        ]
        result = solve(pieces, self._domain())
        self.assertEqual(result.maximum.count, 2)
        self.assertEqual(result.minimum.count, 0)
        self.assertEqual(result.minimum.witness, D('-0.20'))

    def test_point_domain(self):
        domain = Interval(D('0.05'), D('0.05'))
        pieces = [
            {'piece_id': 'A', 'reading': D('0.00'), 'lower': D('0'), 'upper': D('0.10')},
            {'piece_id': 'B', 'reading': D('0.20'), 'lower': D('0'), 'upper': D('0.10')},
        ]
        result = solve(pieces, domain)
        self.assertEqual(result.maximum.count, result.minimum.count)
        self.assertEqual(result.maximum.witness, D('0.05'))
        self.assertEqual(result.maximum.count, 1)

    def test_invalid_spec_raises(self):
        with self.assertRaises(ValueError):
            solve(
                [{'piece_id': 'A', 'reading': D('1'), 'lower': D('2'), 'upper': D('1')}],
                self._domain(),
            )

    def test_invalid_domain_raises(self):
        with self.assertRaises(ValueError):
            solve([], Interval(D('1'), D('0')))

    def test_empty_pieces(self):
        result = solve([], self._domain())
        self.assertEqual((result.total, result.eligible), (0, 0))
        self.assertEqual(result.maximum.count, 0)
        self.assertEqual(result.minimum.count, 0)


class SolveRandomCrossCheck(unittest.TestCase):
    """对随机实例，用候选点暴力枚举核对最小/最大件数。"""

    def test_cross_check(self):
        rng = random.Random(20261123)
        for _ in range(400):
            lo = D(rng.randint(-3, 0))
            hi = lo + D(rng.randint(1, 6))
            domain = Interval(lo, hi)
            n = rng.randint(1, 8)
            pieces = []
            candidates = {lo, hi}
            for i in range(n):
                reading = D(rng.randint(-5, 5))
                lower = D(rng.randint(-6, 2))
                upper = lower + D(rng.randint(1, 8))
                pieces.append({
                    'piece_id': f'P{i}', 'reading': reading,
                    'lower': lower, 'upper': upper,
                })
                # 只取与修正范围真实相交的端点，避免把范围端点误当成合格点
                clo = max(lo, lower - reading)
                chi = min(hi, upper - reading)
                if clo <= chi:
                    candidates.add(clo)
                    candidates.add(chi)
            result = solve(pieces, domain)

            def count_at(c):
                return sum(
                    1 for p in pieces
                    if p['lower'] <= p['reading'] + c <= p['upper']
                )

            coords = sorted(candidates)
            probe = set(coords)
            for a, b in zip(coords, coords[1:]):
                probe.add((a + b) / 2)
            brute_counts = {count_at(c): c for c in probe}
            brute_min = min(brute_counts)
            brute_max = max(brute_counts)
            self.assertEqual(result.minimum.count, brute_min)
            self.assertEqual(result.maximum.count, brute_max)
            self.assertEqual(count_at(result.minimum.witness), brute_min)
            self.assertEqual(count_at(result.maximum.witness), brute_max)
            self.assertTrue(lo <= result.minimum.witness <= hi)
            self.assertTrue(lo <= result.maximum.witness <= hi)


if __name__ == '__main__':
    unittest.main()
