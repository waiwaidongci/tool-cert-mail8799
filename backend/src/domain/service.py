"""复判应用服务：导入、修正补录/更正、重算与版本留痕。

并发口径：
- 所有写事务以 BEGIN IMMEDIATE 立即取库级写锁，更正与复判串行化，不会互相覆盖。
- 更正提交时，受影响批次当前运行置为 superseded，并补一条 pending 运行，
  表示「当前判定等待重算」；旧版本结果保留，只供追查。
- 重算在同一把写锁内完成「失效旧运行 → 写入新结果」，避免读到半更新状态。
"""

import threading
from datetime import datetime
from decimal import Decimal

from .database import Database
from .importer import BatchInput, CorrectionInput, parse_correction_csv, parse_reading_csv
from .interval import Interval, solve


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat(sep=' ')


def _dt(value):
    if isinstance(value, datetime):
        return value
    return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')


class RejudgmentService:
    def __init__(self, db: Database):
        self.db = db
        self._write_lock = threading.RLock()  # 可重入：导入事务提交后内部触发复判

    # ------------------------------------------------------------------ audit
    def _audit(self, conn, actor, action, target=None, detail=None):
        conn.execute(
            'INSERT INTO audit_events(at, actor, action, target, detail) VALUES (?,?,?,?,?)',
            (now_iso(), actor, action, target, detail),
        )

    # ------------------------------------------------------------- batch import
    def import_readings(self, text: str, source_name: str, actor: str) -> int:
        parsed: BatchInput = parse_reading_csv(text, source_name)
        with self._write_lock:
            conn = self.db.connect()
            conn.execute('BEGIN IMMEDIATE')
            try:
                dup = conn.execute(
                    'SELECT id FROM batches WHERE code=? OR fingerprint=?',
                    (parsed.code, parsed.fingerprint),
                ).fetchone()
                if dup:
                    raise ValueError(f'批次 {parsed.code} 已导入（数据指纹或编号重复）')
                cur = conn.execute(
                    'INSERT INTO batches(code, product, spec_lower, spec_upper, source_name,'
                    ' fingerprint, created_at) VALUES (?,?,?,?,?,?,?)',
                    (parsed.code, parsed.product, str(parsed.spec_lower), str(parsed.spec_upper),
                     source_name, parsed.fingerprint, now_iso()),
                )
                batch_id = cur.lastrowid
                for piece in parsed.pieces:
                    pc = conn.execute(
                        'INSERT INTO pieces(batch_id, seq, piece_no, reading) VALUES (?,?,?,?)',
                        (batch_id, piece.seq, piece.piece_no, str(piece.reading)),
                    )
                    conn.execute(
                        'INSERT INTO measurements(batch_id, piece_id, tool_id, measured_at)'
                        ' VALUES (?,?,?,?)',
                        (batch_id, pc.lastrowid, piece.tool_id,
                         piece.measured_at.strftime('%Y-%m-%d %H:%M:%S')),
                    )
                # 新批次先挂一条 pending 运行，等待首次复判
                conn.execute(
                    'INSERT INTO rejudgment_runs(batch_id, started_at, status, reason)'
                    ' VALUES (?,?,?,?)',
                    (batch_id, now_iso(), 'pending', 'import'),
                )
                self._audit(conn, actor, 'import_batch', parsed.code,
                            f'{len(parsed.pieces)} 件 · 来源 {source_name}')
                conn.execute('COMMIT')
                # 导入后立即复判一次：缺修正范围的组生成待核占位，班报可直接呈现待核项
                self.recompute_batch(batch_id, actor)
                return batch_id
            except Exception:
                conn.execute('ROLLBACK')
                raise

    # ------------------------------------------------------ correction upsert
    def import_corrections(self, text: str, source_name: str, actor: str) -> dict:
        records: list[CorrectionInput] = parse_correction_csv(text, source_name)
        affected_batches = set()
        inserted = corrected = 0
        with self._write_lock:
            conn = self.db.connect()
            conn.execute('BEGIN IMMEDIATE')
            try:
                for rec in records:
                    start = rec.period_start.strftime('%Y-%m-%d %H:%M:%S')
                    end = rec.period_end.strftime('%Y-%m-%d %H:%M:%S')
                    existing = conn.execute(
                        'SELECT id, range_lower, range_upper FROM corrections'
                        ' WHERE tool_id=? AND period_start=? AND period_end=?'
                        ' AND superseded_at IS NULL',
                        (rec.tool_id, start, end),
                    ).fetchone()
                    if existing is None:
                        conn.execute(
                            'INSERT INTO corrections(tool_id, period_start, period_end,'
                            ' range_lower, range_upper, source_name, version, created_at)'
                            ' VALUES (?,?,?,?,?,?,1,?)',
                            (rec.tool_id, start, end, str(rec.range_lower),
                             str(rec.range_upper), rec.source_name, now_iso()),
                        )
                        inserted += 1
                        self._audit(conn, actor, 'correction_add', rec.tool_id,
                                    f'{start} ~ {end} 范围 [{rec.range_lower}, {rec.range_upper}]')
                    else:
                        if (Decimal(existing['range_lower']) == rec.range_lower
                                and Decimal(existing['range_upper']) == rec.range_upper):
                            continue  # 完全相同的迟到记录，幂等忽略
                        next_version = conn.execute(
                            'SELECT COALESCE(MAX(version),0)+1 FROM corrections'
                            ' WHERE tool_id=? AND period_start=? AND period_end=?',
                            (rec.tool_id, start, end),
                        ).fetchone()[0]
                        conn.execute(
                            'UPDATE corrections SET superseded_at=? WHERE id=?',
                            (now_iso(), existing['id']),
                        )
                        conn.execute(
                            'INSERT INTO corrections(tool_id, period_start, period_end,'
                            ' range_lower, range_upper, source_name, version, created_at)'
                            ' VALUES (?,?,?,?,?,?,?,?)',
                            (rec.tool_id, start, end, str(rec.range_lower),
                             str(rec.range_upper), rec.source_name, next_version, now_iso()),
                        )
                        corrected += 1
                        self._audit(conn, actor, 'correction_replace', rec.tool_id,
                                    f'{start} ~ {end} v{next_version}：'
                                    f'[{existing["range_lower"]}, {existing["range_upper"]}] → '
                                    f'[{rec.range_lower}, {rec.range_upper}]')
                    for row in conn.execute(
                        'SELECT DISTINCT batch_id FROM measurements'
                        ' WHERE tool_id=? AND measured_at>=? AND measured_at<?',
                        (rec.tool_id, start, end),
                    ):
                        affected_batches.add(row['batch_id'])

                for batch_id in sorted(affected_batches):
                    self._invalidate_locked(conn, batch_id, reason='correction')
                conn.execute('COMMIT')
            except Exception:
                conn.execute('ROLLBACK')
                raise
        return {'inserted': inserted, 'corrected': corrected,
                'affected_batch_ids': sorted(affected_batches)}

    def _invalidate_locked(self, conn, batch_id, reason):
        """调用方须已持有写锁与事务。旧运行置 superseded，补一条 pending。"""
        current = conn.execute(
            'SELECT id FROM rejudgment_runs WHERE batch_id=? AND status=? ORDER BY id DESC LIMIT 1',
            (batch_id, 'current'),
        ).fetchone()
        if current is not None:
            conn.execute(
                'UPDATE rejudgment_runs SET status=? WHERE id=?',
                ('superseded', current['id']),
            )
        pending = conn.execute(
            'SELECT id FROM rejudgment_runs WHERE batch_id=? AND status=? ORDER BY id DESC LIMIT 1',
            (batch_id, 'pending'),
        ).fetchone()
        if pending is None:
            conn.execute(
                'INSERT INTO rejudgment_runs(batch_id, started_at, status, reason)'
                ' VALUES (?,?,?,?)',
                (batch_id, now_iso(), 'pending', reason),
            )
        else:
            # 已在等待重算：刷新挂起原因与时间，保持每个批次至多一条待处理运行
            conn.execute(
                'UPDATE rejudgment_runs SET started_at=?, reason=? WHERE id=?',
                (now_iso(), reason, pending['id']),
            )

    # ------------------------------------------------------------- recompute
    def recompute_batch(self, batch_id: int, actor: str) -> int:
        with self._write_lock:
            conn = self.db.connect()
            conn.execute('BEGIN IMMEDIATE')
            try:
                batch = conn.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
                if batch is None:
                    raise ValueError(f'批次 {batch_id} 不存在')
                # 重算总是开启一条新版本运行，保证每次计算都有独立留痕；
                # 已存在的 pending 占位行（导入/更正挂起）关闭并并入本次，避免待处理堆叠。
                pending_row = conn.execute(
                    'SELECT id FROM rejudgment_runs WHERE batch_id=? AND status=?'
                    ' ORDER BY id DESC LIMIT 1',
                    (batch_id, 'pending'),
                ).fetchone()
                run_id = conn.execute(
                    'INSERT INTO rejudgment_runs(batch_id, started_at, status, reason)'
                    ' VALUES (?,?,?,?)',
                    (batch_id, now_iso(), 'running',
                     'manual' if pending_row is None else 'recompute'),
                ).lastrowid
                if pending_row is not None:
                    conn.execute(
                        'UPDATE rejudgment_runs SET status=?, finished_at=?, detail=? WHERE id=?',
                        ('superseded', now_iso(), f'并入运行 {run_id}', pending_row['id']),
                    )
                self._audit(conn, actor, 'rejudge_start', batch['code'], f'run {run_id}')

                rows = conn.execute(
                    'SELECT p.id AS piece_id, p.seq, p.piece_no, p.reading,'
                    ' m.tool_id, m.measured_at FROM pieces p'
                    ' JOIN measurements m ON m.piece_id=p.id'
                    ' WHERE p.batch_id=? ORDER BY p.seq',
                    (batch_id,),
                ).fetchall()

                # 分组口径：同批 + 同器具 + 同一修正时段，共用一个修正量。
                # 先为每件测量匹配其所在时段的现行修正记录，再按
                # （器具, 时段起, 时段止）归组；匹配不到修正范围的零件按器具并入待核组。
                groups = {}
                order = []
                for row in rows:
                    correction = conn.execute(
                        'SELECT * FROM corrections WHERE tool_id=? AND period_start<=? AND period_end>? '
                        'AND superseded_at IS NULL ORDER BY version DESC LIMIT 1',
                        (row['tool_id'], row['measured_at'], row['measured_at']),
                    ).fetchone()
                    if correction is None:
                        key = (row['tool_id'], None, None)
                    else:
                        key = (row['tool_id'], correction['period_start'], correction['period_end'])
                    if key not in groups:
                        groups[key] = {'correction': correction, 'pieces': []}
                        order.append(key)
                    groups[key]['pieces'].append(row)

                spec_lower = Decimal(batch['spec_lower'])
                spec_upper = Decimal(batch['spec_upper'])
                version_rows = []
                pending_groups = 0
                for key in order:
                    tool_id, period_start, period_end = key
                    bucket = groups[key]
                    pieces = bucket['pieces']
                    correction = bucket['correction']
                    if correction is None:
                        pending_groups += 1
                        version_rows.append({
                            'tool_id': tool_id, 'period_start': None, 'period_end': None,
                            'count': len(pieces), 'range_lower': None, 'range_upper': None,
                            'min_count': None, 'min_witness': None,
                            'max_count': None, 'max_witness': None, 'status': 'pending',
                        })
                        continue
                    domain = Interval(Decimal(correction['range_lower']),
                                      Decimal(correction['range_upper']))
                    payload = [{
                        'piece_id': r['piece_no'],
                        'reading': Decimal(r['reading']),
                        'lower': spec_lower, 'upper': spec_upper,
                    } for r in pieces]
                    result = solve(payload, domain)
                    version_rows.append({
                        'tool_id': tool_id,
                        'period_start': period_start,
                        'period_end': period_end,
                        'count': len(pieces),
                        'range_lower': str(domain.lo), 'range_upper': str(domain.hi),
                        'min_count': result.minimum.count,
                        'min_witness': str(result.minimum.witness),
                        'max_count': result.maximum.count,
                        'max_witness': str(result.maximum.witness),
                        'status': 'ok',
                    })

                for v in version_rows:
                    conn.execute(
                        'INSERT INTO rejudgment_versions(run_id, batch_id, tool_id, period_start,'
                        ' period_end, group_piece_count, range_lower, range_upper, min_count,'
                        ' min_witness, max_count, max_witness, status, created_at)'
                        ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (run_id, batch_id, v['tool_id'], v['period_start'], v['period_end'],
                         v['count'], v['range_lower'], v['range_upper'], v['min_count'],
                         v['min_witness'], v['max_count'], v['max_witness'], v['status'],
                         now_iso()),
                    )
                # 同批此前的运行全部留档（含并发中被更正的），当前运行唯一
                conn.execute(
                    'UPDATE rejudgment_runs SET status=? WHERE batch_id=? AND id<>?',
                    ('superseded', batch_id, run_id),
                )
                final_status = 'pending' if pending_groups else 'current'
                conn.execute(
                    'UPDATE rejudgment_runs SET finished_at=?, status=?, detail=?'
                    ' WHERE id=?',
                    (now_iso(), final_status,
                     f'{len(version_rows)} 组，{pending_groups} 组缺修正范围待核', run_id),
                )
                self._audit(conn, actor, 'rejudge_finish', batch['code'],
                            f'run {run_id} → {final_status}')
                conn.execute('COMMIT')
                return run_id
            except Exception:
                conn.execute('ROLLBACK')
                # 失败运行标记 failed，保留现场
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    conn.execute(
                        'UPDATE rejudgment_runs SET finished_at=?, status=? WHERE id=?',
                        (now_iso(), 'failed', run_id),
                    )
                    conn.execute('COMMIT')
                except Exception:
                    pass
                raise

    # ------------------------------------------------------------- queries
    def list_batches(self):
        conn = self.db.connect()
        batches = [dict(r) for r in conn.execute(
            'SELECT b.*, (SELECT COUNT(*) FROM pieces p WHERE p.batch_id=b.id) AS piece_count,'
            ' (SELECT COUNT(DISTINCT tool_id) FROM measurements m WHERE m.batch_id=b.id) AS tool_count'
            ' FROM batches b ORDER BY b.id')]
        for b in batches:
            run = conn.execute(
                'SELECT * FROM rejudgment_runs WHERE batch_id=? ORDER BY id DESC LIMIT 1',
                (b['id'],),
            ).fetchone()
            b['latest_run'] = dict(run) if run else None
        return batches

    def get_batch_detail(self, batch_id: int) -> dict:
        conn = self.db.connect()
        batch = conn.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
        if batch is None:
            raise ValueError(f'批次 {batch_id} 不存在')
        batch = dict(batch)
        runs = [dict(r) for r in conn.execute(
            'SELECT * FROM rejudgment_runs WHERE batch_id=? ORDER BY id DESC', (batch_id,))]
        latest = runs[0] if runs else None
        versions = []
        if latest:
            versions = [dict(r) for r in conn.execute(
                'SELECT * FROM rejudgment_versions WHERE run_id=? ORDER BY id', (latest['id'],))]
        pieces = [dict(r) for r in conn.execute(
            'SELECT p.seq, p.piece_no, p.reading, m.tool_id, m.measured_at'
            ' FROM pieces p JOIN measurements m ON m.piece_id=p.id'
            ' WHERE p.batch_id=? ORDER BY p.seq', (batch_id,))]
        return {'batch': batch, 'latest_run': latest, 'versions': versions,
                'runs': runs, 'pieces': pieces}

    def list_runs(self, limit=100):
        conn = self.db.connect()
        return [dict(r) for r in conn.execute(
            'SELECT r.*, b.code AS batch_code FROM rejudgment_runs r'
            ' JOIN batches b ON b.id=r.batch_id ORDER BY r.id DESC LIMIT ?', (limit,))]

    def list_audit(self, limit=100):
        conn = self.db.connect()
        return [dict(r) for r in conn.execute(
            'SELECT * FROM audit_events ORDER BY id DESC LIMIT ?', (limit,))]

    def list_corrections(self):
        conn = self.db.connect()
        return [dict(r) for r in conn.execute(
            'SELECT * FROM corrections ORDER BY tool_id, period_start, version')]
