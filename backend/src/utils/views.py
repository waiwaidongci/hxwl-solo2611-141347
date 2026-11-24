from src.utils.time import iso


def dataset(item):
    return {'id': item.id, 'name': item.name, 'filename': item.filename, 'row_count': item.row_count,
            'checksum': item.checksum, 'author': item.author, 'created_at': iso(item.created_at)}


def run(item, detail=False):
    item_view = {'id': item.id, 'dataset_id': item.dataset_id, 'name': item.name, 'status': item.status,
                 'author': item.author, 'review': item.review, 'decision': item.decision,
                 'created_at': iso(item.created_at), 'finished_at': iso(item.finished_at),
                 'scheduled_at': iso(item.scheduled_at), 'schedule_id': item.schedule_id,
                 'has_export': bool(item.export_file), 'error': item.error}
    if detail:
        item_view.update(params=item.params, result=item.result, review_note=item.review_note)
    return item_view


def schedule(item):
    from src.domain.spec import SPEC
    return {'id': item.id, 'name': item.name, 'dataset_id': item.dataset_id, 'recipient': item.recipient,
            'next_at': iso(item.next_at), 'repeat_daily': item.repeat_daily, 'enabled': item.enabled,
            'next_report_day': item.params.get(SPEC['schedule_date_param']) if item.enabled else None}


def delivery(item):
    return {'id': item.id, 'run_id': item.run_id, 'recipient': item.recipient,
            'state': item.state, 'detail': item.detail, 'created_at': iso(item.created_at)}


def revision(item):
    return {'id': item.id, 'ticket_id': item.ticket_id, 'revision': item.revision,
            'posted_at': item.posted_at.date().isoformat() if hasattr(item.posted_at, 'date') else item.posted_at,
            'audit_outcome': item.outcome, 'author': item.author, 'created_at': iso(item.created_at)}


def triage_list(item, detail=False):
    view = {'id': item.id, 'dataset_id': item.dataset_id, 'seq': item.seq, 'status': item.status,
            'created_by': item.created_by, 'created_at': iso(item.created_at),
            'finished_at': iso(item.finished_at), 'activated_at': iso(item.activated_at),
            'error': item.error, 'attempt': item.attempt,
            'revision_count': len(item.revision_ids or [])}
    if detail:
        view.update(params=item.params, result=item.result)
    return view


def dispatch(item):
    return {'id': item.id, 'list_id': item.list_id, 'ticket_id': item.ticket_id, 'lane': item.lane,
            'action': item.action, 'reason': item.reason, 'risk': item.risk,
            'recipient': item.recipient, 'created_by': item.created_by, 'created_at': iso(item.created_at),
            'evidence_snapshot': item.evidence_snapshot}
