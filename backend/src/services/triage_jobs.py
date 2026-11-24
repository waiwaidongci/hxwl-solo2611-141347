"""当班分流清单的重算编排：修订入账、并发受理、原子切换、派单幂等。"""

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from src.config.database import Session
from src.models.source import Dataset
from src.models.triage import AuditRevision, DispatchRecord, TriageList
from src.services import triage as engine
from src.services.audit import record
from src.utils.time import now

log = logging.getLogger(__name__)

QUEUED = 'queued'
RUNNING = 'running'
ACTIVE = 'active'
SUPERSEDED = 'superseded'
FAILED = 'failed'
LEASE_MINUTES = 3
MAX_ATTEMPTS = 3


# --------------------------------------------------------------------------- #
# 实判修订入账
# --------------------------------------------------------------------------- #

def register_revision(db, dataset_id, payload, actor):
    """登记一条实判更正。修订号必须大于该交接单已登记的最大修订号。"""
    if not db.get(Dataset, dataset_id):
        raise ValueError('找不到指定的数据批次')
    parsed = engine.parse_revision(payload)
    arrival = date.fromisoformat(str(_arrival_of(db, dataset_id, parsed.ticket)))
    if parsed.posted_at < arrival:
        raise ValueError(f'{parsed.ticket} 的实判入账日期不能早于闸口初检日期')
    # 串行化同一交接单的并发登记：行锁取当前最大修订号。
    current_max = db.scalar(
        select(func.coalesce(func.max(AuditRevision.revision), 0))
        .where(AuditRevision.ticket_id == parsed.ticket,
               AuditRevision.dataset_id == dataset_id).with_for_update()) or 0
    if parsed.revision <= current_max:
        raise ValueError(
            f'{parsed.ticket} 已登记至修订号 {current_max}，更正修订号必须严格递增（乱序到货请按修订号补登）')
    item = AuditRevision(ticket_id=parsed.ticket, revision=parsed.revision,
                         posted_at=parsed.posted_at, outcome=parsed.outcome,
                         dataset_id=dataset_id, author=actor)
    db.add(item)
    db.flush()
    record(db, actor, 'triage.revision_registered', item.id,
           ticket=parsed.ticket, revision=parsed.revision)
    return item


def _arrival_of(db, dataset_id, ticket):
    dataset = db.get(Dataset, dataset_id)
    for row in dataset.rows:
        if row['ticket_id'] == ticket:
            return row['inspection_date']
    raise ValueError(f'交接单 {ticket} 不在该数据批次中')


def list_revisions(db, dataset_id):
    return db.scalars(select(AuditRevision)
                      .where(AuditRevision.dataset_id == dataset_id)
                      .order_by(AuditRevision.ticket_id, AuditRevision.revision)).all()


# --------------------------------------------------------------------------- #
# 重算受理
# --------------------------------------------------------------------------- #

def request_recompute(db, dataset_id, params, actor):
    """受理一次重算。受理序号单调递增，作为并发覆盖保护的次序依据。"""
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise ValueError('找不到指定的数据批次')
    seq = db.scalar(select(func.coalesce(func.max(TriageList.seq), 0))) + 1
    item = TriageList(dataset_id=dataset_id, seq=seq, status=QUEUED, params=dict(params),
                      revision_ids=[], created_by=actor)
    db.add(item)
    db.flush()
    record(db, actor, 'triage.recompute_queued', item.id, seq=seq, dataset_id=dataset_id)
    return item


def claim_recompute(stamp=None):
    """Worker 认领一个重算任务，返回 (id, attempt, rows, params, revision_payloads)。"""
    stamp = stamp or now()
    with Session.begin() as db:
        query = select(TriageList).where(
            (TriageList.status == QUEUED)
            | ((TriageList.status == RUNNING) & (TriageList.lease_until < stamp)))
        job = db.scalar(query.order_by(TriageList.seq).limit(1).with_for_update(skip_locked=True))
        if job is None:
            return None
        if job.attempt >= MAX_ATTEMPTS:
            job.status, job.error, job.finished_at = FAILED, '后台进程多次中断，请重新发起重算', stamp
            record(db, 'worker', 'triage.failed', job.id, reason=job.error)
            return None
        job.status, job.attempt, job.lease_until = RUNNING, job.attempt + 1, stamp + timedelta(minutes=LEASE_MINUTES)
        revision_rows = db.scalars(
            select(AuditRevision).where(AuditRevision.dataset_id == job.dataset_id)
            .order_by(AuditRevision.id)).all()
        payloads = [_revision_payload(item) for item in revision_rows]
        return job.id, job.attempt, db.get(Dataset, job.dataset_id).rows, dict(job.params), payloads


def _revision_payload(item):
    return {'ticket_id': item.ticket_id, 'revision': item.revision,
            'posted_at': item.posted_at.date().isoformat()
            if isinstance(item.posted_at, datetime) else str(item.posted_at),
            'audit_outcome': item.outcome}


# --------------------------------------------------------------------------- #
# 执行与原子切换
# --------------------------------------------------------------------------- #

def execute_recompute(lease):
    job_id, attempt, rows, params, revision_payloads = lease
    try:
        payload, items = engine.recompute(rows, revision_payloads, params,
                                          frozen_evidence=_active_evidence(rows))
        outcome = ('ok', payload)
    except ValueError as error:
        outcome = ('failed', str(error)[:500])
    except Exception:
        log.exception('Triage recompute %s failed', job_id)
        outcome = ('failed', '重算失败，请检查修订与参数口径后重试')

    with Session.begin() as db:
        job = db.get(TriageList, job_id, with_for_update=True)
        if job is None or job.attempt != attempt or job.status != RUNNING:
            return  # 租约已失效，结果丢弃
        job.lease_until, job.finished_at = None, now()
        if outcome[0] == 'failed':
            job.status, job.error = FAILED, outcome[1]
            record(db, 'worker', 'triage.failed', job.id, attempt=attempt, reason=job.error)
            return
        revision_ids = list(db.scalars(
            select(AuditRevision.id).where(AuditRevision.dataset_id == job.dataset_id)))
        _atomic_switch(db, job, outcome[1], items, revision_ids)


def _active_evidence(rows):
    """读取各交接单最早一次派单的实判依据作为重算冻结依据。

    派单记录挂在派单当时的清单版本下，清单切换后旧记录仍保留；
    因此跨全部清单按交接单取最早派单，确保多代重算后已派单依据不丢失。
    """
    tickets = {row['ticket_id'] for row in rows}
    with Session() as db:
        dispatches = db.scalars(
            select(DispatchRecord).where(DispatchRecord.ticket_id.in_(tickets))
            .order_by(DispatchRecord.id)).all()
        evidence = {}
        for dispatch in dispatches:  # 已按 id 升序，首条即最早派单
            evidence.setdefault(dispatch.ticket_id, dict(dispatch.evidence_snapshot))
        return evidence


def _atomic_switch(db, job, payload, items, revision_ids):
    """整份清单算完后一次性切换：旧 active → superseded，新清单 → active。

    并发保护：只允许当前最大受理序号的任务激活；较早受理的任务即便晚完成也只标记 superseded，
    其结果不得覆盖较新的清单。
    """
    max_seq = db.scalar(select(func.max(TriageList.seq)))
    if job.seq < max_seq:
        job.status, job.result, job.error = SUPERSEDED, payload, '已有更新的重算受理，本结果不切换'
        record(db, 'worker', 'triage.superseded', job.id, seq=job.seq, max_seq=max_seq)
        return
    previous = db.scalars(select(TriageList).where(TriageList.status == ACTIVE)
                          .with_for_update()).all()
    for old in previous:
        old.status = SUPERSEDED
    job.status, job.result, job.error, job.activated_at = ACTIVE, payload, None, now()
    job.revision_ids = revision_ids
    record(db, 'worker', 'triage.activated', job.id,
           seq=job.seq, tickets=len(items), superseded=[old.id for old in previous])


# --------------------------------------------------------------------------- #
# 派单（幂等）
# --------------------------------------------------------------------------- #

def dispatch(db, list_id, ticket_ids, recipient, actor):
    """按清单派单。已派单的交接单直接跳过，失败重试不会产生重复派单。"""
    job = db.get(TriageList, list_id, with_for_update=True)
    if job is None:
        raise ValueError('找不到该分流清单')
    if job.status != ACTIVE:
        raise ValueError('只有当前生效清单可以派单')
    wanted = set(ticket_ids)
    snapshots = {item['ticket_id']: item for item in job.result['snapshot']}
    missing = wanted - set(snapshots)
    if missing:
        raise ValueError(f'交接单 {sorted(missing)} 不在当前清单中')
    existing = {item.ticket_id for item in db.scalars(
        select(DispatchRecord).where(DispatchRecord.list_id == list_id,
                                     DispatchRecord.ticket_id.in_(wanted))
        .with_for_update())}
    created = []
    for ticket in ticket_ids:
        if ticket in existing:
            continue
        snap = snapshots[ticket]
        record_item = DispatchRecord(
            list_id=list_id, ticket_id=ticket, lane=snap['lane'], action=snap['action'],
            reason=snap.get('reason'), risk=snap.get('risk'), recipient=recipient,
            created_by=actor, evidence_snapshot={
                'revision': snap.get('revision_used'), 'lane': snap['lane'],
                'action': snap['action'], 'reason': snap.get('reason'), 'risk': snap.get('risk'),
                'frozen': snap.get('frozen', False)})
        db.add(record_item)
        created.append(record_item)
    db.flush()
    for item in created:
        record(db, actor, 'triage.dispatched', item.id, ticket=item.ticket_id, recipient=recipient)
    return created, sorted(existing)


def active_list(db):
    return db.scalar(select(TriageList).where(TriageList.status == ACTIVE)
                     .order_by(TriageList.seq.desc()).limit(1))


def list_dispatches(db, list_id=None):
    query = select(DispatchRecord).order_by(DispatchRecord.id.desc()).limit(200)
    if list_id is not None:
        query = select(DispatchRecord).where(DispatchRecord.list_id == list_id).order_by(DispatchRecord.id.desc())
    return db.scalars(query).all()
