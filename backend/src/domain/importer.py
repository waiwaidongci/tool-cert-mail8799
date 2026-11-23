"""CSV 导入与字段校验。

两类文件：
1. 读数批次 CSV：批次编号、产品、规格上下限（同批同一规格），逐件编号、读数、器具、测量时间。
2. 修正范围 CSV：器具编号、时段起止、修正范围上下限、来源。迟到记录按器具+时段补入或更正。

时间格式支持 `YYYY-MM-DD HH:MM:SS` 与 `YYYY-MM-DDTHH:MM:SS`。
"""

import csv
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

HEADER_ALIASES = {
    # 读数批次
    '批次编号': 'batch_code', 'batch_code': 'batch_code',
    '产品': 'product', 'product': 'product',
    '规格下限': 'spec_lower', 'spec_lower': 'spec_lower',
    '规格上限': 'spec_upper', 'spec_upper': 'spec_upper',
    '零件编号': 'piece_no', 'piece_no': 'piece_no',
    '读数': 'reading', 'reading': 'reading',
    '器具编号': 'tool_id', 'tool_id': 'tool_id',
    '测量时间': 'measured_at', 'measured_at': 'measured_at',
    # 修正范围
    '时段开始': 'period_start', 'period_start': 'period_start',
    '时段结束': 'period_end', 'period_end': 'period_end',
    '修正下限': 'range_lower', 'range_lower': 'range_lower',
    '修正上限': 'range_upper', 'range_upper': 'range_upper',
    '来源': 'source_name', 'source_name': 'source_name',
}

READING_FIELDS = {'batch_code', 'product', 'spec_lower', 'spec_upper',
                  'piece_no', 'reading', 'tool_id', 'measured_at'}
CORRECTION_FIELDS = {'tool_id', 'period_start', 'period_end',
                     'range_lower', 'range_upper', 'source_name'}


class ImportError_(ValueError):
    """带行号的导入错误。"""


def _decimal(value, label, row_no):
    value = (value or '').strip()
    if not value:
        raise ImportError_(f'第 {row_no} 行：{label}不能为空')
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ImportError_(f'第 {row_no} 行：{label}「{value}」不是有效数值') from None


def _text(value, label, row_no, max_len=64):
    value = (value or '').strip()
    if not 1 <= len(value) <= max_len:
        raise ImportError_(f'第 {row_no} 行：{label}须为 1 至 {max_len} 个字符')
    return value


def parse_dt(value, row_no):
    value = (value or '').strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ImportError_(f'第 {row_no} 行：时间「{value}」格式应为 YYYY-MM-DD HH:MM:SS')


@dataclass
class PieceInput:
    seq: int
    piece_no: str
    reading: Decimal
    tool_id: str
    measured_at: datetime


@dataclass
class BatchInput:
    code: str
    product: str
    spec_lower: Decimal
    spec_upper: Decimal
    source_name: str
    fingerprint: str
    pieces: list = field(default_factory=list)


def _normalize_headers(row):
    return {HEADER_ALIASES.get((k or '').strip(), (k or '').strip()): (v or '').strip()
            for k, v in row.items()}


def parse_reading_csv(text: str, source_name: str) -> BatchInput:
    """解析单批次读数 CSV。同批共用规格上下限。"""
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        raise ImportError_('CSV 内容为空')
    normalized = {HEADER_ALIASES.get(h.strip(), h.strip()) for h in reader.fieldnames}
    missing = READING_FIELDS - normalized
    if missing:
        raise ImportError_(f'读数 CSV 缺少字段：{"、".join(sorted(missing))}')

    batch = None
    seen_pieces = set()
    fingerprint = hashlib.sha256(text.encode('utf-8')).hexdigest()
    for row_no, raw in enumerate(reader, start=2):
        row = _normalize_headers(raw)
        if not any(row.values()):
            continue
        code = _text(row.get('batch_code'), '批次编号', row_no)
        product = _text(row.get('product'), '产品', row_no)
        spec_lower = _decimal(row.get('spec_lower'), '规格下限', row_no)
        spec_upper = _decimal(row.get('spec_upper'), '规格上限', row_no)
        if spec_lower >= spec_upper:
            raise ImportError_(f'第 {row_no} 行：规格下限须严格小于上限')
        piece_no = _text(row.get('piece_no'), '零件编号', row_no)
        reading = _decimal(row.get('reading'), '读数', row_no)
        tool_id = _text(row.get('tool_id'), '器具编号', row_no)
        measured_at = parse_dt(row.get('measured_at'), row_no)

        if batch is None:
            batch = BatchInput(code, product, spec_lower, spec_upper, source_name, fingerprint)
        else:
            if code != batch.code:
                raise ImportError_(f'第 {row_no} 行：一个文件只允许一个批次，发现 {code} 与 {batch.code}')
            if (product, spec_lower, spec_upper) != (batch.product, batch.spec_lower, batch.spec_upper):
                raise ImportError_(f'第 {row_no} 行：批次 {code} 的产品或规格前后不一致')
        if piece_no in seen_pieces:
            raise ImportError_(f'第 {row_no} 行：零件编号 {piece_no} 在批次内重复')
        seen_pieces.add(piece_no)
        batch.pieces.append(PieceInput(len(batch.pieces) + 1, piece_no, reading,
                                       tool_id, measured_at))

    if batch is None or not batch.pieces:
        raise ImportError_('读数 CSV 没有任何零件行')
    return batch


@dataclass
class CorrectionInput:
    tool_id: str
    period_start: datetime
    period_end: datetime
    range_lower: Decimal
    range_upper: Decimal
    source_name: str


def parse_correction_csv(text: str, source_name: str) -> list:
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        raise ImportError_('CSV 内容为空')
    normalized = {HEADER_ALIASES.get(h.strip(), h.strip()) for h in reader.fieldnames}
    missing = CORRECTION_FIELDS - normalized
    if missing:
        raise ImportError_(f'修正 CSV 缺少字段：{"、".join(sorted(missing))}')

    records = []
    seen = set()
    for row_no, raw in enumerate(reader, start=2):
        row = _normalize_headers(raw)
        if not any(row.values()):
            continue
        tool_id = _text(row.get('tool_id'), '器具编号', row_no)
        start = parse_dt(row.get('period_start'), row_no)
        end = parse_dt(row.get('period_end'), row_no)
        if start >= end:
            raise ImportError_(f'第 {row_no} 行：时段开始须严格早于结束')
        lower = _decimal(row.get('range_lower'), '修正下限', row_no)
        upper = _decimal(row.get('range_upper'), '修正上限', row_no)
        if lower > upper:
            raise ImportError_(f'第 {row_no} 行：修正下限不得大于上限')
        source = _text(row.get('source_name') or source_name, '来源', row_no)
        key = (tool_id, start, end)
        if key in seen:
            raise ImportError_(f'第 {row_no} 行：器具 {tool_id} 同一时段在文件内重复')
        seen.add(key)
        records.append(CorrectionInput(tool_id, start, end, lower, upper, source))
    if not records:
        raise ImportError_('修正 CSV 没有任何记录行')
    return records
