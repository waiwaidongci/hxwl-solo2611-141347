"""API 集成测试：修订登记、重算受理、清单切换、派单幂等、角色授权。

运行：PYTHONPATH=/tmp/pylibs:. python3 tests/test_triage_api.py
"""

import csv
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['JWT_SECRET'] = 'test-secret'
os.environ['SEED_DEMO'] = 'false'

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.config import database as db_config  # noqa: E402
from src.config.database import Base  # noqa: E402
from src.main import app  # noqa: E402
from src.models.identity import User  # noqa: E402
from src.models.source import Dataset  # noqa: E402
from src.utils.security import hash_password  # noqa: E402

PARAMS = {'train_end': '2026-11-13', 'validation_end': '2026-11-17', 'holdout_end': '2026-11-22',
          'candidate_ks': '1,3,5,7', 'threshold_pct': 45, 'review_cost': 55, 'miss_cost': 380}


def load_rows():
    with (ROOT / 'demo' / 'sample.csv').open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for key in ('dent_mm', 'door_gap_mm', 'floor_moisture_pct', 'stain_pct'):
            row[key] = int(row[key])
    return rows


class TriageApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            'sqlite:///:memory:',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool)
        cls.Session = sessionmaker(cls.engine, expire_on_commit=False)
        db_config.engine = cls.engine
        db_config.Session = cls.Session
        import src.services.triage_jobs as jobs_module
        jobs_module.Session = cls.Session
        Base.metadata.create_all(cls.engine)
        with cls.Session.begin() as db:
            db.add_all([
                User(username='admin', role='admin', password_hash=hash_password('admin-pass')),
                User(username='analyst', role='analyst', password_hash=hash_password('analyst-pass')),
                User(username='viewer', role='viewer', password_hash=hash_password('viewer-pass')),
                Dataset(name='demo', filename='sample.csv', checksum='a' * 64,
                        rows=load_rows(), row_count=55, author='seed'),
            ])
        cls.client = TestClient(app)

    def token(self, username, password):
        response = self.client.post('/auth/login', json={'username': username, 'password': password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['token']

    def auth(self, username, password):
        token = self.token(username, password)
        return {'Authorization': f'Bearer {token}'}

    def test_end_to_end_revision_recompute_dispatch(self):
        analyst = self.auth('analyst', 'analyst-pass')
        # 1. 登记一条乱序到货的高修订号（入账日早于后续低修订登记）
        response = self.client.post('/triage/revisions', headers=analyst, json={
            'dataset_id': 1, 'ticket_id': 'EIR-1112-02', 'revision': 2,
            'posted_at': '2026-11-12', 'audit_outcome': '完好'})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['revision'], 2)
        # 重复修订号被拒绝
        dup = self.client.post('/triage/revisions', headers=analyst, json={
            'dataset_id': 1, 'ticket_id': 'EIR-1112-02', 'revision': 2,
            'posted_at': '2026-11-13', 'audit_outcome': '门封修理'})
        self.assertEqual(dup.status_code, 422)

        # 2. 受理重算
        queued = self.client.post('/triage/recompute', headers=analyst,
                                  json={'dataset_id': 1, 'name': '当班重算', 'params': PARAMS})
        self.assertEqual(queued.status_code, 202, queued.text)
        list_id = queued.json()['id']
        self.assertEqual(queued.json()['status'], 'queued')

        # 3. 同步驱动 worker 逻辑（测试环境无独立 worker 进程）
        from src.services.triage_jobs import claim_recompute, execute_recompute
        lease = claim_recompute()
        self.assertIsNotNone(lease)
        execute_recompute(lease)

        # 4. 生效清单可查，包含修订可见性表
        active = self.client.get('/triage/lists/active', headers=analyst)
        self.assertEqual(active.status_code, 200)
        body = active.json()
        self.assertEqual(body['id'], list_id)
        self.assertEqual(body['status'], 'active')
        titles = [table['title'] for table in body['result']['tables']]
        self.assertIn('实判修订截止可见性', titles)
        self.assertIn('当班分流清单（重算版）', titles)

        # 5. 派单：首次成功、重试幂等
        tickets = [row[0] for row in body['result']['tables'][0]['rows']][:3]
        sent = self.client.post(f'/triage/lists/{list_id}/dispatch', headers=analyst, json={
            'ticket_ids': tickets, 'recipient': '澄浦北区堆场调度组'})
        self.assertEqual(sent.status_code, 201, sent.text)
        self.assertEqual(len(sent.json()['created']), 3)
        again = self.client.post(f'/triage/lists/{list_id}/dispatch', headers=analyst, json={
            'ticket_ids': tickets, 'recipient': '澄浦北区堆场调度组'})
        self.assertEqual(again.status_code, 201)
        self.assertEqual(again.json()['created'], [])
        self.assertEqual(len(again.json()['skipped_ticket_ids']), 3)
        receipts = self.client.get('/triage/dispatches', headers=analyst).json()
        self.assertEqual(len(receipts), 3)
        self.assertTrue(all(item['evidence_snapshot']['frozen'] is False for item in receipts))

    def test_viewer_cannot_register_or_recompute(self):
        viewer = self.auth('viewer', 'viewer-pass')
        response = self.client.post('/triage/revisions', headers=viewer, json={
            'dataset_id': 1, 'ticket_id': 'EIR-1107-01', 'revision': 1,
            'posted_at': '2026-11-08', 'audit_outcome': '完好'})
        self.assertEqual(response.status_code, 403)
        response = self.client.post('/triage/recompute', headers=viewer,
                                    json={'dataset_id': 1, 'name': 'x', 'params': PARAMS})
        self.assertEqual(response.status_code, 403)
        # 只读角色仍可查看清单与修订
        self.assertEqual(self.client.get('/triage/lists', headers=viewer).status_code, 200)
        self.assertEqual(self.client.get('/triage/revisions?dataset_id=1', headers=viewer).status_code, 200)

    def test_dispatch_unknown_ticket_rejected(self):
        analyst = self.auth('analyst', 'analyst-pass')
        self.client.post('/triage/recompute', headers=analyst,
                         json={'dataset_id': 1, 'name': '派单校验重算', 'params': PARAMS})
        from src.services.triage_jobs import claim_recompute, execute_recompute
        execute_recompute(claim_recompute())
        active = self.client.get('/triage/lists/active', headers=analyst).json()
        response = self.client.post(f"/triage/lists/{active['id']}/dispatch", headers=analyst, json={
            'ticket_ids': ['EIR-9999-99'], 'recipient': '澄浦北区堆场调度组'})
        self.assertEqual(response.status_code, 422)

    def test_second_recompute_supersedes_first_and_keeps_dispatch_snapshot(self):
        analyst = self.auth('analyst', 'analyst-pass')
        active_before = self.client.get('/triage/lists/active', headers=analyst).json()
        target = 'EIR-1124-02'
        original = next(row for row in active_before['result']['snapshot'] if row['ticket_id'] == target)
        # 已派单
        self.client.post(f"/triage/lists/{active_before['id']}/dispatch", headers=analyst,
                         json={'ticket_ids': [target], 'recipient': '澄浦设备验箱一班'})
        # 新修订 + 重算
        self.client.post('/triage/revisions', headers=analyst, json={
            'dataset_id': 1, 'ticket_id': 'EIR-1113-01', 'revision': 1,
            'posted_at': '2026-11-14', 'audit_outcome': '完好'})
        self.client.post('/triage/revisions', headers=analyst, json={
            'dataset_id': 1, 'ticket_id': 'EIR-1115-02', 'revision': 1,
            'posted_at': '2026-11-16', 'audit_outcome': '完好'})
        self.client.post('/triage/recompute', headers=analyst,
                         json={'dataset_id': 1, 'name': '二次重算', 'params': PARAMS})
        from src.services.triage_jobs import claim_recompute, execute_recompute
        execute_recompute(claim_recompute())
        active_after = self.client.get('/triage/lists/active', headers=analyst).json()
        self.assertNotEqual(active_after['id'], active_before['id'])
        row = next(item for item in active_after['result']['snapshot'] if item['ticket_id'] == target)
        self.assertTrue(row['frozen'])
        self.assertEqual(row['lane'], original['lane'])
        # 派单回执仍只有此前那一张，重算未重复派单
        receipts = self.client.get('/triage/dispatches', headers=analyst).json()
        self.assertEqual(len([r for r in receipts if r['ticket_id'] == target]), 1)

    def test_unauthenticated_requests_rejected(self):
        self.assertEqual(self.client.get('/triage/lists').status_code, 401)
        self.assertEqual(self.client.get('/triage/lists/active').status_code, 401)


if __name__ == '__main__':
    unittest.main(verbosity=2)
