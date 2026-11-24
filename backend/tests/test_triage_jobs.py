"""服务层测试：分流清单的并发受理、原子切换、派单幂等与失败重试。

运行：PYTHONPATH=/tmp/pylibs:. python3 tests/test_triage_jobs.py
"""

import csv
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('JWT_SECRET', 'test-secret')

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.config.database import Base  # noqa: E402
from src.models.source import Dataset  # noqa: E402
from src.models.triage import DispatchRecord, TriageList  # noqa: E402
from src.services import triage_jobs as jobs  # noqa: E402
from src.services.triage_jobs import ACTIVE, FAILED, RUNNING, SUPERSEDED  # noqa: E402

PARAMS = {'train_end': '2026-11-13', 'validation_end': '2026-11-17', 'holdout_end': '2026-11-22',
          'candidate_ks': '1,3,5,7', 'threshold_pct': 45, 'review_cost': 55, 'miss_cost': 380}


def load_rows():
    with (ROOT / 'demo' / 'sample.csv').open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for key in ('dent_mm', 'door_gap_mm', 'floor_moisture_pct', 'stain_pct'):
            row[key] = int(row[key])
    return rows


class JobTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            'sqlite:///:memory:', connect_args={'check_same_thread': False},
            poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        jobs.Session = self.Session  # 服务模块默认使用测试会话工厂
        with self.Session.begin() as db:
            dataset = Dataset(name='demo', filename='sample.csv', checksum='x' * 64,
                              rows=load_rows(), row_count=55, author='seed')
            db.add(dataset)
        self.dataset_id = 1

    def _revision(self, ticket, revision, posted, outcome='完好'):
        return {'dataset_id': self.dataset_id, 'ticket_id': ticket, 'revision': revision,
                'posted_at': posted, 'audit_outcome': outcome}

    def test_revision_sequence_must_be_strictly_increasing(self):
        with self.Session.begin() as db:
            jobs.register_revision(db, self.dataset_id,
                                   self._revision('EIR-1109-01', 1, '2026-11-10', '结构修理'), 'analyst')
        with self.Session.begin() as db:
            with self.assertRaises(ValueError):
                jobs.register_revision(db, self.dataset_id,
                                       self._revision('EIR-1109-01', 1, '2026-11-11'), 'analyst')
        # 乱序到货合法：修订号更大即可，入账日允许早于上一修订的登记时间
        with self.Session.begin() as db:
            item = jobs.register_revision(db, self.dataset_id,
                                          self._revision('EIR-1109-01', 2, '2026-11-09'), 'analyst')
            self.assertEqual(item.revision, 2)
        # 但不能再补登更小的修订号（即便入账日更早）
        with self.Session.begin() as db:
            with self.assertRaises(ValueError):
                jobs.register_revision(db, self.dataset_id,
                                       self._revision('EIR-1109-01', 1, '2026-11-08'), 'analyst')

    def test_full_recompute_activates_first_list(self):
        with self.Session.begin() as db:
            item = jobs.request_recompute(db, self.dataset_id, PARAMS, 'analyst')
            self.assertEqual(item.seq, 1)
        lease = jobs.claim_recompute(stamp=datetime(2026, 11, 24))
        self.assertIsNotNone(lease)
        jobs.execute_recompute(lease)
        with self.Session() as db:
            active = jobs.active_list(db)
            self.assertEqual(active.status, ACTIVE)
            self.assertEqual(active.seq, 1)
            self.assertTrue(active.result['ready'])
            self.assertEqual(len(active.result['snapshot']), 8)

    def test_concurrent_recompute_older_completion_cannot_override_newer_list(self):
        # 受理两个重算：seq=1（较早）与 seq=2（较新）
        with self.Session.begin() as db:
            first = jobs.request_recompute(db, self.dataset_id, PARAMS, 'analyst')
        with self.Session.begin() as db:
            second = jobs.request_recompute(db, self.dataset_id, PARAMS, 'analyst')
        lease_one = jobs.claim_recompute(stamp=datetime(2026, 11, 24))
        # seq=1 计算很慢；期间 seq=2 被另一个 worker 认领并完成激活
        lease_two = jobs.claim_recompute(stamp=datetime(2026, 11, 24))
        self.assertEqual((lease_one[0], lease_two[0]), (first.id, second.id))
        jobs.execute_recompute(lease_two)
        with self.Session() as db:
            self.assertEqual(jobs.active_list(db).id, second.id)
        # 较早受理的 seq=1 最后完成：不得覆盖新清单
        jobs.execute_recompute(lease_one)
        with self.Session() as db:
            active = jobs.active_list(db)
            self.assertEqual(active.id, second.id)
            older = db.get(TriageList, first.id)
            self.assertEqual(older.status, SUPERSEDED)
            self.assertIn('更新的重算', older.error)

    def test_recompute_uses_old_list_until_atomic_switch(self):
        # 第一次重算激活清单 A
        with self.Session.begin() as db:
            jobs.request_recompute(db, self.dataset_id, PARAMS, 'analyst')
        jobs.execute_recompute(jobs.claim_recompute(stamp=datetime(2026, 11, 24)))
        with self.Session() as db:
            active_a = jobs.active_list(db)

        # 登记修订并受理第二次重算；运行期间 active 仍指向清单 A
        with self.Session.begin() as db:
            jobs.register_revision(db, self.dataset_id,
                                   self._revision('EIR-1109-01', 1, '2026-11-10', '结构修理'), 'analyst')
            jobs.request_recompute(db, self.dataset_id, PARAMS, 'analyst')
        lease = jobs.claim_recompute(stamp=datetime(2026, 11, 24))
        # 认领后任务处于 running，但尚未完成切换
        with self.Session() as db:
            self.assertEqual(jobs.active_list(db).id, active_a.id)
            running = db.get(TriageList, lease[0])
            self.assertEqual(running.status, RUNNING)
        jobs.execute_recompute(lease)
        with self.Session() as db:
            active_b = jobs.active_list(db)
            self.assertNotEqual(active_b.id, active_a.id)
            self.assertEqual(db.get(TriageList, active_a.id).status, SUPERSEDED)
            self.assertTrue(any(
                row[13] == '本次重算' for row in active_b.result['tables'][0]['rows']))

    def test_dispatch_is_idempotent_and_failure_retry_does_not_duplicate(self):
        with self.Session.begin() as db:
            jobs.request_recompute(db, self.dataset_id, PARAMS, 'analyst')
        jobs.execute_recompute(jobs.claim_recompute(stamp=datetime(2026, 11, 24)))
        with self.Session() as db:
            active = jobs.active_list(db)
            list_id = active.id
            tickets = [row['ticket_id'] for row in active.result['snapshot']][:3]
        with self.Session.begin() as db:
            created, skipped = jobs.dispatch(db, list_id, tickets, '澄浦北区堆场调度组', 'admin')
            self.assertEqual(len(created), 3)
            self.assertEqual(skipped, [])
        # 重试同一批派单：全部跳过，不产生新记录
        with self.Session.begin() as db:
            created, skipped = jobs.dispatch(db, list_id, tickets, '澄浦北区堆场调度组', 'admin')
            self.assertEqual(created, [])
            self.assertEqual(sorted(skipped), sorted(tickets))
        with self.Session() as db:
            total = db.query(DispatchRecord).filter(DispatchRecord.list_id == list_id).count()
            self.assertEqual(total, 3)
            record = db.scalars(select(DispatchRecord).where(
                DispatchRecord.ticket_id == tickets[0])).one()
            self.assertIn('revision', record.evidence_snapshot)

    def test_dispatched_evidence_is_frozen_on_next_recompute(self):
        with self.Session.begin() as db:
            jobs.request_recompute(db, self.dataset_id, PARAMS, 'analyst')
        jobs.execute_recompute(jobs.claim_recompute(stamp=datetime(2026, 11, 24)))
        with self.Session() as db:
            active = jobs.active_list(db)
            target = 'EIR-1124-02'
            original = next(row for row in active.result['snapshot'] if row['ticket_id'] == target)
        with self.Session.begin() as db:
            jobs.dispatch(db, active.id, [target], '澄浦设备验箱一班', 'admin')

        # 新修订改变参考集后再次重算
        with self.Session.begin() as db:
            jobs.register_revision(db, self.dataset_id,
                                   self._revision('EIR-1113-01', 1, '2026-11-14', '完好'), 'analyst')
            jobs.register_revision(db, self.dataset_id,
                                   self._revision('EIR-1115-02', 1, '2026-11-16', '完好'), 'analyst')
            jobs.request_recompute(db, self.dataset_id, PARAMS, 'analyst')
        jobs.execute_recompute(jobs.claim_recompute(stamp=datetime(2026, 11, 24)))
        with self.Session() as db:
            newest = jobs.active_list(db)
            row = next(item for item in newest.result['snapshot'] if item['ticket_id'] == target)
            self.assertTrue(row['frozen'])
            self.assertEqual(row['lane'], original['lane'])  # 派单通道保持
            table_row = next(r for r in newest.result['tables'][0]['rows'] if r[0] == target)
            self.assertEqual(table_row[13], '派单快照保留')

    def test_frozen_evidence_survives_multiple_recompute_generations(self):
        # 第一代：重算并派单
        with self.Session.begin() as db:
            jobs.request_recompute(db, self.dataset_id, PARAMS, 'analyst')
        jobs.execute_recompute(jobs.claim_recompute(stamp=datetime(2026, 11, 24)))
        with self.Session() as db:
            first = jobs.active_list(db)
            target = 'EIR-1124-02'
            original = next(row for row in first.result['snapshot'] if row['ticket_id'] == target)
        with self.Session.begin() as db:
            jobs.dispatch(db, first.id, [target], '澄浦设备验箱一班', 'admin')

        def recompute_generation(revisions):
            with self.Session.begin() as db:
                for payload in revisions:
                    jobs.register_revision(db, self.dataset_id, payload, 'analyst')
                jobs.request_recompute(db, self.dataset_id, PARAMS, 'analyst')
            jobs.execute_recompute(jobs.claim_recompute(stamp=datetime(2026, 11, 24)))
            with self.Session() as db:
                active = jobs.active_list(db)
                return next(row for row in active.result['snapshot'] if row['ticket_id'] == target), active

        # 第二代、第三代重算，各引入新修订
        second, list_two = recompute_generation([
            self._revision('EIR-1113-01', 1, '2026-11-14', '完好'),
            self._revision('EIR-1115-02', 1, '2026-11-16', '完好')])
        self.assertTrue(second['frozen'])
        self.assertEqual(second['lane'], original['lane'])
        third, list_three = recompute_generation([
            self._revision('EIR-1116-02', 1, '2026-11-16', '完好'),
            self._revision('EIR-1119-02', 1, '2026-11-20', '完好')])
        # 派单挂在第一代清单下，第三代重算仍须冻结当时依据
        self.assertTrue(third['frozen'])
        self.assertEqual(third['lane'], original['lane'])
        with self.Session() as db:
            # 全程只有一张派单回执，重算未重复派单
            self.assertEqual(db.query(DispatchRecord).filter(
                DispatchRecord.ticket_id == target).count(), 1)
            self.assertEqual(db.get(TriageList, first.id).status, SUPERSEDED)
            self.assertEqual(db.get(TriageList, list_two.id).status, SUPERSEDED)
            self.assertEqual(jobs.active_list(db).id, list_three.id)

    def test_failed_recompute_marks_failed_and_retry_is_new_lease_without_dispatch(self):
        with self.Session.begin() as db:
            good = jobs.request_recompute(db, self.dataset_id, PARAMS, 'analyst')
        jobs.execute_recompute(jobs.claim_recompute(stamp=datetime(2026, 11, 24)))
        with self.Session.begin() as db:
            jobs.dispatch(db, good.id + 0, ['EIR-1124-01'], '澄浦北区堆场调度组', 'admin')

        # 构造一个必失败的重算（坏参数）
        bad_params = dict(PARAMS, candidate_ks='99')
        with self.Session.begin() as db:
            bad = jobs.request_recompute(db, self.dataset_id, bad_params, 'analyst')
        lease = jobs.claim_recompute(stamp=datetime(2026, 11, 24))
        jobs.execute_recompute(lease)
        with self.Session() as db:
            self.assertEqual(db.get(TriageList, bad.id).status, FAILED)
            self.assertTrue(db.get(TriageList, bad.id).error)
            # 旧清单保持生效
            self.assertEqual(jobs.active_list(db).id, good.id)
            # 派单没有重复
            self.assertEqual(db.query(DispatchRecord).count(), 1)

        # 修正参数后重新受理并重试成功
        with self.Session.begin() as db:
            retry = jobs.request_recompute(db, self.dataset_id, PARAMS, 'analyst')
        jobs.execute_recompute(jobs.claim_recompute(stamp=datetime(2026, 11, 24)))
        with self.Session() as db:
            self.assertEqual(jobs.active_list(db).id, retry.id)
            self.assertEqual(db.query(DispatchRecord).count(), 1)  # 无重复派单

    def test_lease_reclaim_after_timeout_keeps_attempt_guard(self):
        with self.Session.begin() as db:
            item = jobs.request_recompute(db, self.dataset_id, PARAMS, 'analyst')
        first = jobs.claim_recompute(stamp=datetime(2026, 11, 24))
        self.assertEqual(first[1], 1)
        # worker 宕机：租约过期后可被重新认领
        second = jobs.claim_recompute(stamp=datetime(2026, 11, 24, 0, 10))
        self.assertEqual(second[0], item.id)
        self.assertEqual(second[1], 2)
        jobs.execute_recompute(second)
        with self.Session() as db:
            self.assertEqual(jobs.active_list(db).id, item.id)
        # 第一次租约的延迟结果必须被丢弃（attempt 不匹配）
        jobs.execute_recompute(first)
        with self.Session() as db:
            self.assertEqual(jobs.active_list(db).attempt, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
