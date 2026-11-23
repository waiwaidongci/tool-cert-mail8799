"""同批同器具同时段修正量极值算法。

业务口径（计量室复判）：
- 第 i 件读数 x_i，规格 [L, U]，共用修正量 c（修正后读数 = x_i + c）。
- 该件可接受的修正区间为 [L - x_i, U - x_i]，闭区间。
- 修正量只能在器具该时段修正范围 [lo, hi] 内取值。
- 求一个 c，使合格件数分别达到**可能的最大值**与**可能的最小值**，
  并各给出一个能达到该件数的取值。

采用扫描线（事件点 + 相邻事件点之间的开区间）精确求解，全部使用 Decimal
避免浮点误差。开区间内覆盖件数恒定，取中点作为见证点。
"""

from decimal import Decimal, localcontext
from dataclasses import dataclass

_ZERO = Decimal('0')


@dataclass(frozen=True)
class Interval:
    """闭区间 [lo, hi]，Decimal 表示。"""

    lo: Decimal
    hi: Decimal

    @property
    def valid(self) -> bool:
        return self.lo <= self.hi


def clip(interval: Interval, domain: Interval) -> Interval | None:
    """把区间与修正范围取交集；无交集返回 None。"""
    lo = max(interval.lo, domain.lo)
    hi = min(interval.hi, domain.hi)
    return Interval(lo, hi) if lo <= hi else None


@dataclass(frozen=True)
class Extremum:
    """极值结果：件数与达到该件数的一个修正量取值。"""

    count: int
    witness: Decimal


@dataclass(frozen=True)
class Rejudgment:
    """单组（同批同器具同时段）复判结果。"""

    domain: Interval
    total: int                 # 组内零件总数
    eligible: int              # 在修正范围内存在合格可能的零件数
    maximum: Extremum
    minimum: Extremum
    max_qualified_ids: tuple   # 取最大见证值时合格的零件编号
    per_piece: tuple           # 逐件区间明细（dict 元组）


def solve(pieces, domain: Interval) -> Rejudgment:
    """计算极值。

    pieces: 可迭代对象，每项为 dict，含
        piece_id（可空）、reading、lower、upper，均为 Decimal。
    domain: 器具该时段修正范围 [lo, hi]。
    """
    if not domain.valid:
        raise ValueError('修正范围下限不得大于上限')

    raw = []
    clipped_rows = []
    for piece in pieces:
        reading = Decimal(piece['reading'])
        lower = Decimal(piece['lower'])
        upper = Decimal(piece['upper'])
        if lower > upper:
            raise ValueError(f"零件 {piece.get('piece_id')} 的规格下限大于上限")
        accept = Interval(lower - reading, upper - reading)
        inter = clip(accept, domain)
        row = {
            'piece_id': piece.get('piece_id'),
            'reading': reading,
            'lower': lower,
            'upper': upper,
            'accept_lo': accept.lo,
            'accept_hi': accept.hi,
            'clipped_lo': inter.lo if inter else None,
            'clipped_hi': inter.hi if inter else None,
            'eligible': inter is not None,
        }
        clipped_rows.append(row)
        if inter is not None:
            raw.append((inter, row))

    total = len(clipped_rows)
    if not raw:
        # 没有任何零件存在合格可能：取修正范围任一端点，件数恒为 0
        witness = domain.lo
        return Rejudgment(
            domain=domain, total=total, eligible=0,
            maximum=Extremum(0, witness), minimum=Extremum(0, witness),
            max_qualified_ids=(), per_piece=tuple(clipped_rows),
        )

    # 扫描线：每个有效交集区间产生 +1@lo、-1@hi 事件。
    # 修正范围端点作为零事件并入，使范围两端之外的零覆盖区域也有候选单元。
    events = {domain.lo: [0, 0], domain.hi: [0, 0]}
    for inter, _ in raw:
        events.setdefault(inter.lo, [0, 0])
        events.setdefault(inter.hi, [0, 0])
        events[inter.lo][0] += 1
        events[inter.hi][1] += 1
    positions = sorted(events)

    # 点单元：position 处覆盖数（含以该点为端点的闭区间）。
    point_count = {}
    running = 0
    for pos in positions:
        plus, minus = events[pos]
        point_count[pos] = running + plus  # 闭区间：右端点仍计入
        running += plus - minus

    cells = []  # (count, witness, kind, position)
    for idx, pos in enumerate(positions):
        cells.append((point_count[pos], pos, 'point', pos))
        if idx + 1 < len(positions):
            nxt = positions[idx + 1]
            # 开区间 (pos, nxt)：覆盖数 = pos 点覆盖数 - 在 pos 结束的区间数
            gap_count = point_count[pos] - events[pos][1]
            with localcontext() as ctx:
                ctx.prec = 28
                mid = (pos + nxt) / 2
            cells.append((gap_count, mid, 'gap', pos))

    # 最小件数：件数升序；相同取位置最小，再相同优先取端点（point）。
    min_cell = min(cells, key=lambda c: (c[0], c[3], 0 if c[2] == 'point' else 1))
    # 最大件数：件数降序（以负件数走 min）；相同取位置最小，再相同优先取端点。
    max_cell = min(cells, key=lambda c: (-c[0], c[3], 0 if c[2] == 'point' else 1))

    max_witness = max_cell[1]
    max_ids = tuple(
        row['piece_id']
        for inter, row in raw
        if inter.lo <= max_witness <= inter.hi
    )

    return Rejudgment(
        domain=domain, total=total, eligible=len(raw),
        maximum=Extremum(max_cell[0], max_witness),
        minimum=Extremum(min_cell[0], min_cell[1]),
        max_qualified_ids=max_ids,
        per_piece=tuple(clipped_rows),
    )
