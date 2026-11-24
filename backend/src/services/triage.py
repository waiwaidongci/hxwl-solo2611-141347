"""Revision-aware recompute of the on-shift triage list.

实判更正带修订号且可能乱序到货。本模块只做纯函数计算，不触碰数据库：

- 训练、验证、留出仍按 *闸口初检日* 归段；
- 每个截止点只采纳该截止前已经实际入账（posted_at）的最高修订号；
- 待验分流由冻结参考集（验证截止前已入账的最高修订）打分；
- 已派单的交接单重算时保留派单当时的实判依据（frozen_evidence 快照）。
"""

from dataclasses import dataclass
from datetime import date
from math import hypot
from heapq import nsmallest
from statistics import pstdev

from . import analysis as base
from .analysis import (
    CLEAR, NO_FLAG, ROUTINE_ACTION, HOLD_ACTIONS, REVIEW_ACTIONS,
    MIN_FEATURE_SCALE, Route,
    _day, _parse_rows, _parse_settings, _route, _evaluate_flags, _evaluation_row,
    _table, EVAL_COLUMNS,
)

BASELINE_REVISION = 0


@dataclass(frozen=True)
class Revision:
    """同一交接单的一次实判更正。revision 越大越新；posted_at 是实际入账日。"""
    ticket: str
    revision: int
    posted_at: date
    outcome: str


@dataclass(frozen=True)
class TriageItem:
    route: Route
    revision_used: int          # 该单冻结依据实际采用的修订号（无实判时为 None）
    frozen: bool                # 是否沿用派单当时的实判依据快照
    changed: bool               # 若按新数据重算，通道是否会变化（仅已派单单有意义）


# --------------------------------------------------------------------------- #
# 修订解析与截止点可见性
# --------------------------------------------------------------------------- #

def parse_revision(raw):
    """校验一条实判更正记录，返回 Revision。raw 来自 AuditRevision 行或 API。"""
    ticket = base._text(raw.get('ticket_id'), base.FIELD_BY_KEY['ticket_id'])
    revision = raw.get('revision')
    if type(revision) is not int or not 1 <= revision <= 32767:
        raise ValueError(f'{ticket} 的修订号须为 1 至 32767 的整数')
    posted = _day(raw.get('posted_at'), '实判入账日期')
    outcome = base._text(raw.get('audit_outcome'), base.FIELD_BY_KEY['audit_outcome'])
    if outcome != base.CLEAR and outcome not in REVIEW_ACTIONS:
        raise ValueError(f'{ticket} 的实判结论须为完好、结构修理、门封修理、烘干处理、清洗处理或隔离处置')
    return Revision(ticket, revision, posted, outcome)


def build_revision_index(revisions):
    """{ticket: {revision_no: Revision}}。"""
    index = {}
    for item in revisions:
        bucket = index.setdefault(item.ticket, {})
        if item.revision in bucket:
            raise ValueError(f'交接单 {item.ticket} 修订号 {item.revision} 重复')
        bucket[item.revision] = item
    return index


def baseline_audits(rows):
    """初版（修订号 0）实判来自导入批次里已填写的 audit_date/audit_outcome。"""
    result = {}
    for row in rows:
        if row.get('audit_date') not in ('', None):
            result[row['ticket_id']] = (
                _day(row['audit_date'], '实判入账日期'), row['audit_outcome'], BASELINE_REVISION)
    return result


def resolve_at_cutoff(ticket, cutoff, baseline, revisions):
    """返回某交接单在截止点 cutoff 实际可见的最高修订 (posted, outcome, revision)。

    乱序到货安全：修订即便编号更高，posted_at 晚于截止点也不可见。
    """
    candidates = []
    initial = baseline.get(ticket)
    if initial and initial[0] <= cutoff:
        candidates.append(initial)
    for rev in revisions.get(ticket, {}).values():
        if rev.posted_at <= cutoff:
            candidates.append((rev.posted_at, rev.outcome, rev.revision))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[2])


def effective_view(rows, revisions, cutoff):
    """按截止点把每行替换为当时实际入账的最高修订视图，未入账的保持待验。"""
    baseline = baseline_audits(rows)
    view = []
    for row in rows:
        visible = resolve_at_cutoff(row['ticket_id'], cutoff, baseline, revisions)
        current = dict(row)
        if visible is None:
            current['audit_date'], current['audit_outcome'] = None, None
        else:
            current['audit_date'], current['audit_outcome'] = visible[0].isoformat(), visible[1]
        view.append(current)
    return view


# --------------------------------------------------------------------------- #
# 参考集（与 analysis.ReferenceSet 同口径）
# --------------------------------------------------------------------------- #

class RevisionReferenceSet:
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
            (hypot(_euclid(target_vector, vector), float(target.box_type != record.box_type)),
             arrival, record.ticket, record)
            for record, vector, arrival in self.examples
            if record.container != target.container
        )
        ranked = nsmallest(k, distances)
        if len(ranked) < k:
            raise ValueError(f'{target.ticket} 排除相同箱号后可用邻居仅 {len(ranked)} 只，k={k} 无法比较或运行')
        return tuple((distance, record) for distance, _, _, record in ranked)


def _euclid(left, right):
    return hypot(*(a - b for a, b in zip(left, right)))


def _action_for(lane, reason):
    for flag, (hold_lane, action) in HOLD_ACTIONS.items():
        if lane == hold_lane:
            return action
    if lane == '常规验收':
        return ROUTINE_ACTION
    return REVIEW_ACTIONS.get(reason, '')


# --------------------------------------------------------------------------- #
# 重算主流程
# --------------------------------------------------------------------------- #

def _split(records, rows, revisions, train_end, validation_end, holdout_end):
    """初检日归段，每段用各自截止点可见的最高修订重新解析。"""
    baseline = baseline_audits(rows)
    # 每个截止点只构建一次全量视图，避免逐单重解析。
    views = {
        cutoff: {record.ticket: record for record in
                 _parse_rows([dict(row) for row in effective_view(rows, revisions, cutoff)])}
        for cutoff in (train_end, validation_end, holdout_end)}
    by_ticket = {record.ticket: record for record in records}

    train, validation, holdout, pending = [], [], [], []
    train_unposted = future_closed = 0
    for record in records:
        if record.arrival <= train_end:
            if resolve_at_cutoff(record.ticket, train_end, baseline, revisions):
                train.append(views[train_end][record.ticket])
            else:
                train_unposted += 1
        elif record.arrival <= validation_end:
            validation.append(views[validation_end][record.ticket])
        elif record.arrival <= holdout_end:
            holdout.append(views[holdout_end][record.ticket])
        elif resolve_at_cutoff(record.ticket, holdout_end, baseline, revisions) is None:
            pending.append(by_ticket[record.ticket])
        else:
            future_closed += 1

    for name, cohort, cutoff in (('验证', validation, validation_end), ('留出', holdout, holdout_end)):
        if not any(record.flag == NO_FLAG for record in cohort):
            raise ValueError(f'{name}段至少需要一只走常规打分的验箱记录，请检查时间切分')
        for record in cohort:
            if record.posted is None or record.posted > cutoff:
                raise ValueError(f'{name}段 {record.ticket} 的实判须在 {cutoff.isoformat()} 或之前入账')
    return train, validation, holdout, pending, train_unposted, future_closed, baseline


def recompute(rows, revision_rows, params, frozen_evidence=None):
    """重算整份当班分流清单。

    frozen_evidence: {ticket: {'revision': int, 'lane': str, 'reason': str,
                               'risk': float|None, 'action': str}}
                     已派单交接单的当时实判依据，重算不得改写。
    返回 (可 JSON 序列化的清单载荷, TriageItem 列表)。
    """
    frozen_evidence = frozen_evidence or {}
    train_end, validation_end, holdout_end, ks, threshold, review_cost, miss_cost = _parse_settings(params)
    revisions = build_revision_index([
        parse_revision(item) if isinstance(item, dict) else item for item in revision_rows])
    records = _parse_rows([dict(row) for row in rows])
    train, validation, holdout, pending, train_unposted, future_closed, baseline = _split(
        records, rows, revisions, train_end, validation_end, holdout_end)

    train_reference = [record for record in train if record.flag == NO_FLAG]
    model = RevisionReferenceSet(train_reference)
    validation_ranks = [model.nearest(row, ks[-1]) if row.flag == NO_FLAG else () for row in validation]
    comparisons = {}
    for k in ks:
        routes = (_route(row, rank[:k], threshold) for row, rank in zip(validation, validation_ranks))
        comparisons[k] = _evaluate_flags(validation, (route.recheck for route in routes), review_cost, miss_cost)
    chosen = min(ks, key=lambda k: (comparisons[k].cost, comparisons[k].fn, k))

    # 冻结参考集：验证截止前已入账（最高修订）的训练与验证常规箱。
    frozen_view = effective_view(rows, revisions, validation_end)
    final_records = [
        record for record in _parse_rows([dict(row) for row in frozen_view])
        if record.arrival <= validation_end and record.posted is not None and record.flag == NO_FLAG]
    frozen = RevisionReferenceSet(final_records)

    holdout_routes = [_route(row, frozen.nearest(row, chosen) if row.flag == NO_FLAG else (), threshold)
                      for row in holdout]
    held = _evaluate_flags(holdout, (route.recheck for route in holdout_routes), review_cost, miss_cost)
    all_recheck = _evaluate_flags(holdout, (True for _ in holdout), review_cost, miss_cost)
    rule_only = _evaluate_flags(holdout, (row.flag != NO_FLAG for row in holdout), review_cost, miss_cost)

    items = _triage_items(pending, frozen, chosen, threshold, baseline, revisions,
                          validation_end, frozen_evidence)
    payload = _payload(
        items, chosen, threshold, review_cost, miss_cost, comparisons, held, all_recheck, rule_only,
        train_end, validation_end, holdout_end, train_reference, final_records, train_unposted,
        future_closed, validation, holdout, holdout_routes, revisions, baseline)
    return payload, items


def _triage_items(pending, frozen, chosen, threshold, baseline, revisions,
                  validation_end, frozen_evidence):
    items = []
    for record in pending:
        ranked = frozen.nearest(record, chosen) if record.flag == NO_FLAG else ()
        current = _route(record, ranked, threshold)
        evidence = frozen_evidence.get(record.ticket)
        if evidence is not None and record.flag == NO_FLAG:
            # 已派单：通道与依据保留派单当时的快照；近邻证据按当前冻结集重算仅用于展示。
            snapshot = Route(record, evidence['lane'], evidence.get('action')
                             or _action_for(evidence['lane'], evidence.get('reason')),
                             evidence.get('risk'), current.neighbors, evidence.get('reason', ''))
            items.append(TriageItem(snapshot, evidence['revision'], True,
                                    snapshot.lane != current.lane))
        else:
            visible = resolve_at_cutoff(record.ticket, validation_end, baseline, revisions)
            items.append(TriageItem(current, visible[2] if visible else None, False, False))
    items.sort(key=lambda item: (item.route.risk is not None, not item.route.recheck,
                                 -(item.route.risk or 0), item.route.inspection.arrival,
                                 item.route.inspection.ticket))
    return items


def _payload(items, chosen, threshold, review_cost, miss_cost, comparisons, held, all_recheck,
             rule_only, train_end, validation_end, holdout_end, train_reference, final_records,
             train_unposted, future_closed, validation, holdout, holdout_routes,
             revisions, baseline):
    holds = sum(item.route.risk is None for item in items)
    rechecks = sum(item.route.lane == '送复检' for item in items)
    routine = len(items) - holds - rechecks
    frozen_count = sum(item.frozen for item in items)
    changed_count = sum(item.changed for item in items)

    live_rows, evidence_rows, snapshot_rows = [], [], []
    for item in items:
        route = item.route
        row = route.inspection
        live_rows.append([row.ticket, row.container, row.arrival.isoformat(), route.lane,
                          round(route.risk, 4) if route.risk is not None else None, route.reason,
                          route.action, row.box_type, *row.readings,
                          item.revision_used if item.revision_used is not None else '',
                          '派单快照保留' if item.frozen else '本次重算',
                          '若重算将改道，维持原派单' if item.changed else ''])
        snapshot_rows.append({
            'ticket_id': row.ticket, 'container_id': row.container,
            'inspection_date': row.arrival.isoformat(), 'box_type': row.box_type, 'flag': row.flag,
            'readings': list(row.readings), 'lane': route.lane, 'action': route.action,
            'risk': route.risk, 'reason': route.reason,
            'revision_used': item.revision_used, 'frozen': item.frozen, 'changed': item.changed})
        for rank, neighbor in enumerate(route.neighbors, start=1):
            historical = neighbor.inspection
            rev = final_revision_lookup(historical.ticket, baseline, revisions, validation_end)
            evidence_rows.append([row.ticket, rank, historical.ticket, historical.container,
                                  historical.arrival.isoformat(), historical.posted.isoformat(),
                                  historical.outcome, round(neighbor.distance, 4),
                                  round(100 * neighbor.weight, 4), historical.box_type,
                                  rev, *historical.readings])

    split_summary = [
        ['训练参考', train_end.isoformat(), len(train_reference), '初检和实判均在截止日或之前的常规箱（取截止前最高修订）'],
        ['训练截止尚未入账', train_end.isoformat(), train_unposted, '仅在实判入账后才可作为后续参考'],
        ['时间验证', validation_end.isoformat(), len(validation), '次段初检记录，用于选 k'],
        ['冻结参考', validation_end.isoformat(), len(final_records), '验证截止前已入账最高修订的训练与验证常规箱'],
        ['时间留出', holdout_end.isoformat(), len(holdout), '后段初检记录，用于独立计分'],
        ['待验队列', f'{holdout_end.isoformat()} 之后', len(items), '冻结方案给出分流建议'],
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

    revision_rows_out = []
    for ticket, bucket in sorted(revisions.items()):
        for revision_no in sorted(bucket):
            rev = bucket[revision_no]
            revision_rows_out.append([
                ticket, rev.revision, rev.posted_at.isoformat(), rev.outcome,
                _rev_label(resolve_at_cutoff(ticket, train_end, baseline, revisions), rev.revision),
                _rev_label(resolve_at_cutoff(ticket, validation_end, baseline, revisions), rev.revision),
                _rev_label(resolve_at_cutoff(ticket, holdout_end, baseline, revisions), rev.revision)])

    summary = (f'验证选用 k={chosen}，成本 {comparisons[chosen].cost} 元。'
               f'留出 {len(holdout)} 箱，复检或留置 {held.review_count} 箱、漏检 {held.fn} 箱，估算 {held.cost} 元。')
    if items:
        summary += (f'待验 {len(items)} 箱安排 {rechecks} 箱复检、{routine} 箱常规验收、{holds} 箱留置；'
                    f'其中 {frozen_count} 张已派单单保留当时实判依据'
                    + (f'，{changed_count} 张若按新数据改道但维持原派单。' if changed_count else '。'))
    else:
        summary += '此次没有待验单。'
    notes = [
        f'异常票达到 {threshold:g}% 时进入复检，授权验箱员作实判。留置单按闸口标记优先处置。',
        '训练、验证、留出按闸口初检日归段；每段只取该段截止前实际入账的最高修订，晚于截止点入账的修订不可见。',
        '重算期间现场继续沿用旧清单；整份清单计算完成后一次性切换，已派单交接单保留派单当时的实判依据。',
        '证据同距时优先较新的初检日，再按历史交接单号排序。表格票比与距离四舍五入，分流使用原始精度。',
    ]
    return {
        'summary': summary,
        'metrics': [
            {'label': '验证选用 k', 'value': chosen, 'detail': f'验证 {len(validation)} 箱，成本 {comparisons[chosen].cost} 元'},
            {'label': '留出漏检（箱）', 'value': held.fn, 'detail': f'实判异常共 {held.tp + held.fn} 箱'},
            {'label': '留出总成本（元）', 'value': held.cost, 'detail': f'全量复检 {all_recheck.cost} 元，仅按留置标记 {rule_only.cost} 元'},
            {'label': '当班留置及复检（箱）', 'value': holds + rechecks,
             'detail': f'留置 {holds} 箱，复检 {rechecks} 箱，常规 {routine} 箱；派单快照 {frozen_count} 张'},
        ],
        'tables': [
            _table('当班分流清单（重算版）',
                   ['待验交接单', '箱号', '闸口初检日', '建议通道', '异常票（%）', '主要依据', '下一步',
                    '箱型', '凹陷（mm）', '门缝（mm）', '含水率（%）', '污迹（%）', '采用修订号', '依据来源', '改道提示'],
                   live_rows),
            _table('逐箱近邻证据', ['待验交接单', '近邻次序', '历史交接单', '历史箱号', '历史初检日', '结论入账日',
                              '历史实判', '距离', '投票权重（%）', '箱型', '邻居修订号', '凹陷（mm）', '门缝（mm）',
                              '含水率（%）', '污迹（%）'], evidence_rows),
            _table('实判修订截止可见性',
                   ['交接单', '修订号', '入账日', '更正后实判', '训练截止采用', '验证截止采用', '留出截止采用'],
                   revision_rows_out),
            _table('验证段候选 k 成本比较', EVAL_COLUMNS, validation_rows),
            _table('留出段独立核验与成本对照', EVAL_COLUMNS, holdout_rows),
            _table('留出段错分交接单', ['交接单', '箱号', '闸口初检日', '结论入账日', '错分', '实判', '建议通道',
                                 '异常票（%）', '主要依据'], holdout_errors),
            _table('时间切分与参考范围', ['用途', '时间边界', '箱数', '纳入口径'], split_summary),
        ],
        'series_title': '验证段各候选 k 的估算成本（元）',
        'series': [{'label': f'k={k}', 'value': result.cost} for k, result in comparisons.items()],
        'notes': notes,
        'ready': bool(items),
        'snapshot': snapshot_rows,
        'chosen_k': chosen,
    }


def final_revision_lookup(ticket, baseline, revisions, cutoff):
    visible = resolve_at_cutoff(ticket, cutoff, baseline, revisions)
    return visible[2] if visible else ''


def _rev_label(visible, revision_no):
    if visible is None:
        return '未入账'
    if visible[2] == revision_no:
        return f'修订 {visible[2]}（{visible[1]}）'
    return '被更高修订替代'
