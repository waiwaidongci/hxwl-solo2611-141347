from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.domain.spec import SPEC
from src.models.identity import User
from src.models.source import Dataset
from src.models.triage import TriageList
from src.schemas.requests import DispatchRequest, RecomputeRequest, RevisionRequest
from src.services import triage_jobs as jobs
from src.services.validation import normalize_params
from src.utils import views
from src.utils.wakeup import wake

router = APIRouter(prefix='/triage')


@router.get('/revisions')
def revisions(dataset_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    if not db.get(Dataset, dataset_id):
        raise HTTPException(404, '找不到指定的数据批次')
    return [views.revision(item) for item in jobs.list_revisions(db, dataset_id)]


@router.post('/revisions', status_code=201)
def add_revision(body: RevisionRequest, db: Session = Depends(connection),
                 actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    try:
        item = jobs.register_revision(db, body.dataset_id, body.model_dump(), actor.username)
        db.commit()
    except ValueError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(422, '该交接单的修订号已存在，请改用更大的修订号补登') from None
    wake()
    return views.revision(item)


@router.post('/recompute', status_code=202)
def recompute(body: RecomputeRequest, db: Session = Depends(connection),
              actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    if not db.get(Dataset, body.dataset_id):
        raise HTTPException(404, '找不到指定的数据批次')
    try:
        item = jobs.request_recompute(db, body.dataset_id, normalize_params(body.params), actor.username)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(422, '受理序号冲突，请重新发起重算') from None
    wake()
    return views.triage_list(item, detail=True)


@router.get('/lists')
def lists(db: Session = Depends(connection), actor: User = Depends(current)):
    items = db.scalars(select(TriageList).order_by(TriageList.seq.desc()).limit(100))
    return [views.triage_list(item) for item in items]


@router.get('/lists/active')
def active(db: Session = Depends(connection), actor: User = Depends(current)):
    item = jobs.active_list(db)
    if item is None:
        raise HTTPException(404, '当前没有生效的分流清单，请先发起重算')
    return views.triage_list(item, detail=True)


@router.get('/lists/{list_id}')
def detail(list_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    item = db.get(TriageList, list_id)
    if item is None:
        raise HTTPException(404, '找不到该分流清单')
    return views.triage_list(item, detail=True)


@router.post('/lists/{list_id}/dispatch', status_code=201)
def dispatch(list_id: int, body: DispatchRequest, db: Session = Depends(connection),
             actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    if body.recipient not in SPEC['recipients']:
        raise HTTPException(422, '请选择当前业务的收件团队')
    try:
        created, skipped = jobs.dispatch(db, list_id, body.ticket_ids, body.recipient, actor.username)
        db.commit()
    except ValueError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(422, '存在并发派单冲突，请刷新后重试（不会产生重复派单）') from None
    return {'created': [views.dispatch(item) for item in created],
            'skipped_ticket_ids': skipped,
            'duplicate': not bool(created) and bool(skipped)}


@router.get('/dispatches')
def dispatches(list_id: int | None = None, db: Session = Depends(connection),
               actor: User = Depends(current)):
    return [views.dispatch(item) for item in jobs.list_dispatches(db, list_id)]
