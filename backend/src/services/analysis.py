"""Temporal replay of dry-container gate triage, with traceable KNN votes."""

from dataclasses import dataclass
from datetime import date
from heapq import nsmallest
from math import dist, fsum, hypot, isfinite
import re
from statistics import pstdev

from ..domain.spec import BUSINESS


FEATURES = ('dent_mm', 'door_gap_mm', 'floor_moisture_pct', 'stain_pct')
MIN_FEATURE_SCALE = 0.01
BOX_TYPES = {'20GP', '40GP', '40HC'}
CLEAR = '完好'
NO_FLAG = '无'
ROUTINE_ACTION = '验箱员按常规验收项目复核并签字。'
HOLD_ACTIONS = {
    '疑似结构险': ('结构留置', '转结构留置区，由验箱员复核箱角件、梁柱及受损箱板。'),
    '不明残留': ('残留隔离', '暂停交接，通知安全值班与验箱班，按堆场隔离流程处理。'),
}
REVIEW_ACTIONS = {
    '结构修理': '到复检工位核对箱板凹陷和相邻梁柱，记录受损位置。',
    '门封修理': '到复检工位核对门封闭合和缝隙，记录异常位置。',
    '烘干处理': '到复检工位复测地板含水率，标记异常测点。',
    '清洗处理': '到复检工位核对可见污迹范围，留存初检点位。',
    '隔离处置': '到受控复核工位核对异常标记，请验箱班长确认后续处置。',
}
FIELD_BY_KEY = {field['key']: field for field in BUSINESS['fields']}
PARAM_BY_KEY = {param['key']: param for param in BUSINESS['parameters']}


@dataclass(frozen=True)
class Inspection:
    ticket: str
    container: str
    arrival: date
    box_type: str
    readings: tuple[float, ...]
    flag: str
    posted: date | None
    outcome: str | None


@dataclass(frozen=True)
class Neighbor:
    inspection: Inspection
    distance: float
    weight: float


@dataclass(frozen=True)
class Route:
    inspection: Inspection
    lane: str
    action: str
    risk: float | None = None
    neighbors: tuple[Neighbor, ...] = ()
    reason: str = ''

    @property
    def recheck(self):
        return self.lane != '常规验收'


@dataclass(frozen=True)
class Evaluation:
    tp: int
    fp: int
    fn: int
    tn: int
    cost: int

    @property
    def review_count(self):
        return self.tp + self.fp


def _day(value, label):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f'{label}须为有效日期，格式为 YYYY-MM-DD') from None
    if parsed.isoformat() != value:
        raise ValueError(f'{label}须使用 YYYY-MM-DD 格式')
    return parsed


def _number(value, field):
    label = field['label']
    if field['type'] == 'int':
        valid = type(value) is int
    else:
        valid = type(value) in (int, float)
    if not valid or not field['min'] <= value <= field['max'] or not isfinite(value):
        raise ValueError(f'{label}须为 {field["min"]} 至 {field["max"]} 之间的有效数值')
    return value


def _text(value, field):
    text = value.strip() if isinstance(value, str) else ''
    if not text or len(text) > field.get('max_length', 300):
        raise ValueError(f'{field["label"]}缺失或长度超限')
    return text


def _parse_row(row):
    ticket = _text(row.get('ticket_id'), FIELD_BY_KEY['ticket_id'])
    container = _text(row.get('container_id'), FIELD_BY_KEY['container_id']).upper()
    if not re.fullmatch(r'[A-Z]{3}U[0-9]{7}', container):
        raise ValueError('干货集装箱号须为三位字母、U 和七位数字')
    arrival = _day(row.get('inspection_date'), '闸口初检日期')
    box_type = _text(row.get('box_type'), FIELD_BY_KEY['box_type'])
    if box_type not in BOX_TYPES:
        raise ValueError('箱型只支持 20GP、40GP 和 40HC')
    readings = tuple(_number(row.get(key), FIELD_BY_KEY[key]) for key in FEATURES)
    flag = _text(row.get('gate_flag'), FIELD_BY_KEY['gate_flag'])
    if flag != NO_FLAG and flag not in HOLD_ACTIONS:
        raise ValueError('闸口留置标记只接受无、疑似结构险或不明残留')

    raw_day = row.get('audit_date')
    posted = _day(raw_day, '实判入账日期') if raw_day not in ('', None) else None
    raw_outcome = row.get('audit_outcome')
    outcome = _text(raw_outcome, FIELD_BY_KEY['audit_outcome']) if raw_outcome not in ('', None) else None
    if (posted is None) != (outcome is None):
        raise ValueError('实判入账日期和实判结论须同时填写或同时留空')
    if outcome is not None and outcome != CLEAR and outcome not in REVIEW_ACTIONS:
        raise ValueError('实判结论须为完好、结构修理、门封修理、烘干处理、清洗处理或隔离处置')
    if posted is not None and posted < arrival:
        raise ValueError('实判入账日期不能早于闸口初检日期')
    return Inspection(ticket, container, arrival, box_type, readings, flag, posted, outcome)


def _parse_rows(rows):
    inspections = []
    seen = set()
    for index, raw in enumerate(rows, start=1):
        try:
            record = _parse_row(raw)
        except ValueError as error:
            raise ValueError(f'台账第 {index} 条 · {error}') from None
        if record.ticket in seen:
            raise ValueError(f'设备交接单号 {record.ticket} 重复')
        seen.add(record.ticket)
        inspections.append(record)
    return sorted(inspections, key=lambda record: (record.arrival, record.ticket))


def validate_rows(rows):
    _parse_rows(rows)


def _parse_settings(params):
    if set(params) - PARAM_BY_KEY.keys():
        raise ValueError('存在未知运行参数，请检查复检方案配置')
    values = {key: params.get(key, field['default']) for key, field in PARAM_BY_KEY.items()}
    train_end = _day(values['train_end'], '训练截止日')
    validation_end = _day(values['validation_end'], '验证截止日')
    holdout_end = _day(values['holdout_end'], '留出截止日')
    if not train_end < validation_end < holdout_end:
        raise ValueError('训练、验证、留出三个截止日须依次递增')

    raw_ks = values['candidate_ks']
    tokens = re.split('[,，]', raw_ks) if isinstance(raw_ks, str) else []
    if not 2 <= len(tokens) <= 8 or any(not re.fullmatch(r'[0-9]{1,2}', token.strip()) for token in tokens):
        raise ValueError('候选邻居数 k 须填写 2 至 8 个以逗号分隔的整数')
    ks = tuple(sorted(map(int, tokens)))
    if len(set(ks)) != len(ks) or ks[0] < 1 or ks[-1] > 15:
        raise ValueError('候选邻居数 k 须互不重复，每个取值范围为 1 至 15')
    return (train_end, validation_end, holdout_end, ks,
            _number(values['threshold_pct'], PARAM_BY_KEY['threshold_pct']),
            _number(values['review_cost'], PARAM_BY_KEY['review_cost']),
            _number(values['miss_cost'], PARAM_BY_KEY['miss_cost']))


def _split(inspections, train_end, validation_end, holdout_end):
    train, validation, holdout, pending = [], [], [], []
    train_unposted = future_closed = 0
    for record in inspections:
        if record.arrival <= train_end:
            if record.posted is not None and record.posted <= train_end:
                train.append(record)
            else:
                train_unposted += 1
        elif record.arrival <= validation_end:
            validation.append(record)
        elif record.arrival <= holdout_end:
            holdout.append(record)
        elif record.posted is None:
            pending.append(record)
        else:
            future_closed += 1
    for name, cohort, cutoff in (('验证', validation, validation_end), ('留出', holdout, holdout_end)):
        if not any(record.flag == NO_FLAG for record in cohort):
            raise ValueError(f'{name}段至少需要一只走常规打分的验箱记录，请检查时间切分')
        for record in cohort:
            if record.posted is None or record.posted > cutoff:
                raise ValueError(f'{name}段 {record.ticket} 的实判须在 {cutoff.isoformat()} 或之前入账')
    return train, validation, holdout, pending, train_unposted, future_closed


class ReferenceSet:
    def __init__(self, records):
        if {record.outcome == CLEAR for record in records} != {False, True}:
            raise ValueError('训练参考须同时包含已入账的完好与异常常规箱')
        self.scales = tuple(
            max(MIN_FEATURE_SCALE, pstdev(feature)) if min(feature) != max(feature) else 0
            for feature in zip(*(row.readings for row in records))
        )
        self.examples = [(record, self.vector(record), -record.arrival.toordinal()) for record in records]

    def vector(self, record):
        return tuple(value / scale for value, scale in zip(record.readings, self.scales) if scale > 0)

    def nearest(self, target, k):
        target_vector = self.vector(target)
        distances = (
            (hypot(dist(target_vector, vector), float(target.box_type != record.box_type)), arrival, record.ticket, record)
            for record, vector, arrival in self.examples
            if record.container != target.container
        )
        ranked = nsmallest(k, distances)
        if len(ranked) < k:
            raise ValueError(f'{target.ticket} 排除相同箱号后可用邻居仅 {len(ranked)} 只，k={k} 无法比较或运行')
        return tuple((distance, record) for distance, _, _, record in ranked)


def _route(record, ranked, threshold):
    if record.flag != NO_FLAG:
        lane, action = HOLD_ACTIONS[record.flag]
        return Route(record, lane, action, reason=record.flag)

    closest = ranked[0][0]
    # The common factor preserves inverse-distance ratios without using very large weights.
    weights = [closest / distance if closest else float(distance == 0) for distance, _ in ranked]
    total = fsum(weights)
    abnormal_weight = fsum(weight for weight, (_, neighbor) in zip(weights, ranked) if neighbor.outcome != CLEAR)
    risk = 100 * (abnormal_weight / total)
    neighbors = tuple(Neighbor(neighbor, distance, weight / total) for (distance, neighbor), weight in zip(ranked, weights))
    if risk < threshold:
        return Route(record, '常规验收', ROUTINE_ACTION, risk, neighbors, '异常票低于阈值')

    votes = {}
    for item in neighbors:
        outcome = item.inspection.outcome
        if outcome != CLEAR and item.weight:
            votes[outcome] = votes.get(outcome, 0.0) + item.weight
    # Ties for the suggested inspection item follow the first occurrence in the ranked list.
    topic = max(votes, key=votes.get)
    return Route(record, '送复检', REVIEW_ACTIONS[topic], risk, neighbors, topic)


def _route_records(records, reference, k, threshold):
    return [_route(row, reference.nearest(row, k) if row.flag == NO_FLAG else (), threshold) for row in records]


def _evaluate_flags(records, rechecks, review_cost, miss_cost):
    tp = fp = fn = tn = 0
    for record, recheck in zip(records, rechecks, strict=True):
        if record.outcome == CLEAR:
            fp += recheck
            tn += not recheck
        else:
            tp += recheck
            fn += not recheck
    return Evaluation(tp, fp, fn, tn, (tp + fp) * review_cost + fn * miss_cost)


def _evaluation_row(label, result, review_cost, miss_cost):
    return [label, result.review_count, result.tp, result.fp, result.fn, result.tn,
            result.review_count * review_cost, result.fn * miss_cost, result.cost]


def _table(title, columns, rows):
    return {'title': title, 'columns': columns, 'rows': rows}


EVAL_COLUMNS = ['方案', '复检及留置', '检出异常', '额外复检', '漏检', '正确常规验收', '复检费（元）', '漏检损失（元）', '总成本（元）']


def analyze(rows, params):
    train_end, validation_end, holdout_end, ks, threshold, review_cost, miss_cost = _parse_settings(params)
    all_rows = _parse_rows(rows)
    train, validation, holdout, pending, train_unposted, future_closed = _split(
        all_rows, train_end, validation_end, holdout_end)
    train_reference = [record for record in train if record.flag == NO_FLAG]
    model = ReferenceSet(train_reference)
    # k alternatives share one ranked list per validation ticket.
    validation_ranks = [model.nearest(row, ks[-1]) if row.flag == NO_FLAG else () for row in validation]
    comparisons = {}
    for k in ks:
        routes = (_route(row, rank[:k], threshold) for row, rank in zip(validation, validation_ranks))
        comparisons[k] = _evaluate_flags(validation, (route.recheck for route in routes), review_cost, miss_cost)
    chosen = min(ks, key=lambda k: (comparisons[k].cost, comparisons[k].fn, k))

    final_reference = [record for record in all_rows
                       if record.arrival <= validation_end and record.posted is not None
                       and record.posted <= validation_end and record.flag == NO_FLAG]
    frozen = ReferenceSet(final_reference)
    holdout_routes = _route_records(holdout, frozen, chosen, threshold)
    held = _evaluate_flags(holdout, (route.recheck for route in holdout_routes), review_cost, miss_cost)
    all_recheck = _evaluate_flags(holdout, (True for _ in holdout), review_cost, miss_cost)
    rule_only = _evaluate_flags(holdout, (row.flag != NO_FLAG for row in holdout), review_cost, miss_cost)

    live = _route_records(pending, frozen, chosen, threshold)
    live.sort(key=lambda route: (route.risk is not None, not route.recheck,
                                -(route.risk or 0), route.inspection.arrival, route.inspection.ticket))
    holds = sum(route.risk is None for route in live)
    rechecks = sum(route.lane == '送复检' for route in live)
    routine = len(live) - holds - rechecks
    split_summary = [
        ['训练参考', train_end.isoformat(), len(train_reference), '初检和实判均在截止日或之前的常规箱'],
        ['训练截止尚未入账', train_end.isoformat(), train_unposted, '仅在实判入账后才可作为后续参考'],
        ['时间验证', validation_end.isoformat(), len(validation), '次段初检记录，用于选 k'],
        ['冻结参考', validation_end.isoformat(), len(final_reference), '验证截止前已入账的训练与验证常规箱'],
        ['时间留出', holdout_end.isoformat(), len(holdout), '后段初检记录，用于独立计分'],
        ['待验队列', f'{holdout_end.isoformat()} 之后', len(pending), '冻结方案给出分流建议'],
        ['留出之后已结案', f'{holdout_end.isoformat()} 之后', future_closed, '本次回放排除这些已结案单'],
    ]
    validation_rows = [_evaluation_row(f'k={k}' + (' · 选用' if k == chosen else ''), result, review_cost, miss_cost)
                       for k, result in comparisons.items()]
    holdout_rows = [_evaluation_row(label, result, review_cost, miss_cost) for label, result in (
        (f'冻结分流 k={chosen}', held), ('全量复检', all_recheck), ('仅按留置标记', rule_only))]
    holdout_errors = []
    for route in holdout_routes:
        row = route.inspection
        if route.recheck == (row.outcome != CLEAR):
            continue
        holdout_errors.append([row.ticket, row.container, row.arrival.isoformat(), row.posted.isoformat(),
                               '额外复检' if route.recheck else '漏检', row.outcome, route.lane,
                               round(route.risk, 4) if route.risk is not None else None, route.reason])

    live_rows, evidence_rows = [], []
    for route in live:
        row = route.inspection
        live_rows.append([row.ticket, row.container, row.arrival.isoformat(), route.lane,
                          round(route.risk, 4) if route.risk is not None else None, route.reason, route.action,
                          row.box_type, *row.readings])
        for rank, neighbor in enumerate(route.neighbors, start=1):
            historical = neighbor.inspection
            evidence_rows.append([row.ticket, rank, historical.ticket, historical.container,
                                  historical.arrival.isoformat(), historical.posted.isoformat(), historical.outcome,
                                  round(neighbor.distance, 4), round(100 * neighbor.weight, 4), historical.box_type,
                                  *historical.readings])
    summary = (f'验证选用 k={chosen}，成本 {comparisons[chosen].cost} 元。'
               f'留出 {len(holdout)} 箱，复检或留置 {held.review_count} 箱、漏检 {held.fn} 箱，估算 {held.cost} 元。')
    if live:
        summary += f'待验 {len(live)} 箱先处置 {holds} 箱留置单，安排 {rechecks} 箱复检、{routine} 箱常规验收。'
    else:
        summary += '此次没有待验单。'
    notes = [
        f'异常票达到 {threshold:g}% 时进入复检，授权验箱员作实判。留置单按闸口标记优先处置。',
        f'每箱复检或留置核验 {review_cost} 元，漏检每箱 {miss_cost} 元。留出结论不参与选 k、拟合或当班邻居投票。',
        '证据同距时优先较新的初检日，再按历史交接单号排序。表格票比与距离四舍五入，分流使用原始精度。',
        '时间回放需要验证及留出全量实判。待验队列为空时可查看评估，班次派单无待验交接单。',
    ]
    for name, phase in (('验证', validation), ('留出', holdout)):
        if len({row.outcome == CLEAR for row in phase}) == 1:
            notes.append(f'{name}实判只有一类，成本仅反映已观察到的箱况。')
    return {
        'summary': summary,
        'metrics': [
            {'label': '验证选用 k', 'value': chosen, 'detail': f'验证 {len(validation)} 箱，成本 {comparisons[chosen].cost} 元'},
            {'label': '留出漏检（箱）', 'value': held.fn, 'detail': f'实判异常共 {held.tp + held.fn} 箱'},
            {'label': '留出总成本（元）', 'value': held.cost, 'detail': f'全量复检 {all_recheck.cost} 元，仅按留置标记 {rule_only.cost} 元'},
            {'label': '当班留置及复检（箱）', 'value': holds + rechecks, 'detail': f'留置 {holds} 箱，复检 {rechecks} 箱，常规 {routine} 箱'},
        ],
        'tables': [
            _table('待验分流清单', ['待验交接单', '箱号', '闸口初检日', '建议通道', '异常票（%）', '主要依据', '下一步',
                              '箱型', '凹陷（mm）', '门缝（mm）', '含水率（%）', '污迹（%）'], live_rows),
            _table('逐箱近邻证据', ['待验交接单', '近邻次序', '历史交接单', '历史箱号', '历史初检日', '结论入账日',
                              '历史实判', '距离', '投票权重（%）', '箱型', '凹陷（mm）', '门缝（mm）', '含水率（%）', '污迹（%）'], evidence_rows),
            _table('验证段候选 k 成本比较', EVAL_COLUMNS, validation_rows),
            _table('留出段独立核验与成本对照', EVAL_COLUMNS, holdout_rows),
            _table('留出段错分交接单', ['交接单', '箱号', '闸口初检日', '结论入账日', '错分', '实判', '建议通道',
                                 '异常票（%）', '主要依据'], holdout_errors),
            _table('时间切分与参考范围', ['用途', '时间边界', '箱数', '纳入口径'], split_summary),
        ],
        'series_title': '验证段各候选 k 的估算成本（元）',
        'series': [{'label': f'k={k}', 'value': result.cost} for k, result in comparisons.items()],
        'notes': notes,
        'ready': bool(live),
    }
