"""纯引擎测试：仅依赖标准库，直接运行 `python3 tests/test_triage_engine.py`。

覆盖：
1. 乱序到货：高修订号晚入账时，各截止点只看到截止前实际入账的最高修订；
2. 初检日归段不变；
3. 已派单交接单保留派单当时实判依据（frozen_evidence），即使新数据会改道；
4. 重算结果确定性；
5. 修订解析与重复修订号校验。
"""

import csv
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services.triage import (  # noqa: E402
    Revision, BASELINE_REVISION, resolve_at_cutoff, baseline_audits,
    build_revision_index, parse_revision, effective_view, recompute,
)

DEMO_CSV = ROOT / 'demo' / 'sample.csv'
PARAMS = {'train_end': '2026-11-13', 'validation_end': '2026-11-17', 'holdout_end': '2026-11-22',
          'candidate_ks': '1,3,5,7', 'threshold_pct': 45, 'review_cost': 55, 'miss_cost': 380}


def load_rows():
    with DEMO_CSV.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def coerce(rows):
    """模拟 validation.parse_rows 的类型转换（测试中不引入 pydantic）。"""
    ints = {'dent_mm', 'door_gap_mm', 'floor_moisture_pct', 'stain_pct'}
    out = []
    for row in rows:
        item = dict(row)
        for key in ints:
            item[key] = int(item[key])
        out.append(item)
    return out


class CutoffVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.rows = coerce(load_rows())
        self.baseline = baseline_audits(self.rows)
        # EIR-1112-02 初检 11-12，初版 audit 2026-11-13 清洗处理（修订 0，训练截止前可见）
        self.ticket = 'EIR-1112-02'

    def test_out_of_order_higher_revision_after_cutoff_is_invisible(self):
        revisions = build_revision_index([
            Revision(self.ticket, 2, date(2026, 11, 12), '完好'),       # 乱序：rev2 先到
            Revision(self.ticket, 3, date(2026, 11, 18), '门封修理'),   # rev3 验证截止后才入账
        ])
        train = resolve_at_cutoff(self.ticket, date(2026, 11, 13), self.baseline, revisions)
        validation = resolve_at_cutoff(self.ticket, date(2026, 11, 17), self.baseline, revisions)
        holdout = resolve_at_cutoff(self.ticket, date(2026, 11, 22), self.baseline, revisions)
        self.assertEqual(train, (date(2026, 11, 12), '完好', 2))
        self.assertEqual(validation, (date(2026, 11, 12), '完好', 2))
        self.assertEqual(holdout, (date(2026, 11, 18), '门封修理', 3))

    def test_each_segment_uses_highest_posted_before_its_own_cutoff(self):
        revisions = build_revision_index([
            Revision(self.ticket, 1, date(2026, 11, 14), '烘干处理'),
            Revision(self.ticket, 2, date(2026, 11, 16), '完好'),
            Revision(self.ticket, 3, date(2026, 11, 20), '清洗处理'),
        ])
        # 初版 11-13 入账；训练截止 11-13 只看得到修订 0
        self.assertEqual(resolve_at_cutoff(self.ticket, date(2026, 11, 13), self.baseline, revisions)[2], 0)
        self.assertEqual(resolve_at_cutoff(self.ticket, date(2026, 11, 17), self.baseline, revisions),
                         (date(2026, 11, 16), '完好', 2))
        self.assertEqual(resolve_at_cutoff(self.ticket, date(2026, 11, 22), self.baseline, revisions),
                         (date(2026, 11, 20), '清洗处理', 3))

    def test_effective_view_marks_unposted_as_pending(self):
        # EIR-1113-01 基线实判 11-14 入账：验证截止 11-17 前本就可见，改用修订取代它，
        # 再用一张基线未入账的留出段单（EIR-1121-03 基线 11-22 入账）验证截止前不可见。
        revisions = build_revision_index([
            Revision('EIR-1121-03', 1, date(2026, 11, 23), '门封修理'),  # 留出截止后入账
        ])
        view = effective_view(self.rows, revisions, date(2026, 11, 22))
        row = next(item for item in view if item['ticket_id'] == 'EIR-1121-03')
        # 基线 11-22 已在留出截止当天入账，因此截止点可见基线；新修订不可见
        self.assertEqual(row['audit_date'], '2026-11-22')
        self.assertEqual(row['audit_outcome'], '完好')
        later = effective_view(self.rows, revisions, date(2026, 11, 24))
        row = next(item for item in later if item['ticket_id'] == 'EIR-1121-03')
        self.assertEqual(row['audit_outcome'], '门封修理')

    def test_duplicate_revision_rejected(self):
        with self.assertRaises(ValueError):
            build_revision_index([
                Revision('EIR-1107-01', 1, date(2026, 11, 8), '完好'),
                Revision('EIR-1107-01', 1, date(2026, 11, 9), '结构修理'),
            ])

    def test_parse_revision_validation(self):
        with self.assertRaises(ValueError):
            parse_revision({'ticket_id': 'EIR-1107-01', 'revision': 0,
                            'posted_at': '2026-11-08', 'audit_outcome': '完好'})
        with self.assertRaises(ValueError):
            parse_revision({'ticket_id': 'EIR-1107-01', 'revision': 1,
                            'posted_at': '2026-11-08', 'audit_outcome': '不存在的结论'})
        parsed = parse_revision({'ticket_id': 'EIR-1107-01', 'revision': 1,
                                 'posted_at': '2026-11-08', 'audit_outcome': '完好'})
        self.assertEqual(parsed.revision, 1)


class RecomputeTests(unittest.TestCase):
    def setUp(self):
        self.rows = coerce(load_rows())

    def test_baseline_recompute_matches_original_pending_list(self):
        payload, items = recompute(self.rows, [], PARAMS)
        tickets = {item.route.inspection.ticket for item in items}
        self.assertEqual(tickets, {f'EIR-1124-0{i}' for i in range(1, 9)})
        # 留置单跳过打分且排在清单最前
        self.assertEqual([item.route.inspection.ticket for item in items[:2]],
                         ['EIR-1124-05', 'EIR-1124-06'])
        self.assertTrue(all(item.route.risk is None for item in items[:2]))
        self.assertTrue(all(item.revision_used is None for item in items))
        self.assertTrue(all(not item.frozen for item in items))

    def test_revision_changes_reference_outcome_and_triage_risk(self):
        # EIR-1108-01（CPUU7001035）是待验单 EIR-1124-02 同箱号历史单，会被邻居排除；
        # 选取一条训练段常规箱做结论更正，验证重算后的票比发生变化且采用修订号被记录。
        revisions = [
            {'ticket_id': 'EIR-1109-01', 'revision': 1, 'posted_at': '2026-11-10',
             'audit_outcome': '结构修理'},
        ]
        payload_before, _ = recompute(self.rows, [], PARAMS)
        payload_after, items = recompute(self.rows, revisions, PARAMS)
        self.assertNotEqual(
            [row[:7] for row in payload_before['tables'][0]['rows']],
            [row[:7] for row in payload_after['tables'][0]['rows']])
        evidence = payload_after['tables'][1]['rows']
        rev_row = next(row for row in evidence if row[2] == 'EIR-1109-01')
        self.assertEqual(rev_row[10], 1)  # 邻居修订号列
        self.assertEqual(rev_row[6], '结构修理')

    def test_late_revision_excluded_from_validation_cutoff(self):
        # 修订在验证截止之后才入账：验证段选 k 与冻结参考集都不得采用，基线修订 0 继续有效
        revisions = [
            {'ticket_id': 'EIR-1109-01', 'revision': 1, 'posted_at': '2026-11-20',
             'audit_outcome': '结构修理'},
        ]
        payload, _ = recompute(self.rows, revisions, PARAMS)
        visibility = payload['tables'][2]['rows']
        row = next(item for item in visibility if item[0] == 'EIR-1109-01')
        self.assertIn('被更高修订替代', row[4])   # 训练截止仍采用基线修订 0
        self.assertIn('被更高修订替代', row[5])   # 验证截止仍采用基线修订 0
        self.assertIn('修订 1', row[6])           # 留出截止可见新修订
        # 冻结参考集（验证截止）中该单实判仍是基线的“完好”
        frozen_evidence = next(item for item in payload['tables'][1]['rows']
                               if item[2] == 'EIR-1109-01')
        self.assertEqual(frozen_evidence[6], '完好')

    def test_frozen_evidence_keeps_dispatched_lane(self):
        # 先取无修订的基线，构造一张已派单（送复检）快照
        _, base_items = recompute(self.rows, [], PARAMS)
        target = next(item for item in base_items if item.route.inspection.ticket == 'EIR-1124-02')
        original_lane = target.route.lane
        evidence = {
            'EIR-1124-02': {
                'revision': target.revision_used if target.revision_used is not None else BASELINE_REVISION,
                'lane': target.route.lane, 'action': target.route.action,
                'risk': target.route.risk, 'reason': target.route.reason,
            }
        }
        # 制造足以改道的修订：把其近邻中的异常历史单改成完好
        revisions = [
            {'ticket_id': 'EIR-1113-01', 'revision': 1, 'posted_at': '2026-11-14',
             'audit_outcome': '完好'},
            {'ticket_id': 'EIR-1115-02', 'revision': 1, 'posted_at': '2026-11-16',
             'audit_outcome': '完好'},
        ]
        payload, items = recompute(self.rows, revisions, PARAMS, frozen_evidence=evidence)
        entry = next(item for item in items if item.route.inspection.ticket == 'EIR-1124-02')
        self.assertTrue(entry.frozen)
        self.assertEqual(entry.route.lane, original_lane)  # 派单通道不变
        table_row = next(row for row in payload['tables'][0]['rows'] if row[0] == 'EIR-1124-02')
        self.assertEqual(table_row[13], '派单快照保留')

    def test_recompute_is_deterministic(self):
        revisions = [
            {'ticket_id': 'EIR-1112-01', 'revision': 2, 'posted_at': '2026-11-13',
             'audit_outcome': '门封修理'},
            {'ticket_id': 'EIR-1112-01', 'revision': 1, 'posted_at': '2026-11-12',
             'audit_outcome': '完好'},
        ]
        first, items_first = recompute(self.rows, revisions, PARAMS)
        second, items_second = recompute(self.rows, revisions, PARAMS)
        self.assertEqual(first['tables'][0]['rows'], second['tables'][0]['rows'])
        self.assertEqual([(i.route.inspection.ticket, i.route.lane) for i in items_first],
                         [(i.route.inspection.ticket, i.route.lane) for i in items_second])

    def test_segments_follow_inspection_date_not_posted_date(self):
        # 初检 11-15（验证段）的单，即使实判 11-20 才更正入账，仍归验证段
        revisions = [
            {'ticket_id': 'EIR-1115-01', 'revision': 1, 'posted_at': '2026-11-20',
             'audit_outcome': '门封修理'},
        ]
        payload, _ = recompute(self.rows, revisions, PARAMS)
        split = {row[0]: row for row in payload['tables'][6]['rows']}
        self.assertEqual(split['时间验证'][2], 12)  # 验证段：11-14 至 11-17 初检
        self.assertEqual(split['时间留出'][2], 12)  # 留出段：11-19 至 11-22 初检
        # 该单在验证截止前仍只有基线，留出截止才看到修订 1，
        # 但始终按初检日 11-15 留在验证段，不会因晚入账漂移到留出段。
        row = next(item for item in payload['tables'][2]['rows'] if item[0] == 'EIR-1115-01')
        self.assertIn('被更高修订替代', row[5])
        self.assertIn('修订 1', row[6])


if __name__ == '__main__':
    unittest.main(verbosity=2)
