from datetime import datetime
from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now


class AuditRevision(Base):
    """实判更正流水。同一交接单的修订号严格递增，乱序到货也按 revision 判定新旧。"""
    __tablename__ = 'audit_revisions'
    __table_args__ = (
        UniqueConstraint('ticket_id', 'revision', name='uq_audit_revision_ticket_revision'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[str] = mapped_column(String(48), index=True)
    revision: Mapped[int]
    posted_at: Mapped[datetime]      # 实际入账日（date 以 UTC 零点存储）
    outcome: Mapped[str] = mapped_column(String(20))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)


class TriageList(Base):
    """当班分流清单版本。重算期间沿用旧清单，整份算完后一次性原子切换。"""
    __tablename__ = 'triage_lists'

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    # 受理序号：并发重算时序号小的任务即使晚完成，也不能覆盖序号大的新清单。
    seq: Mapped[int] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(String(20), index=True)  # queued/running/active/superseded/failed
    params: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(500))
    # 本次重算纳入的修订流水 id，失败重试据此跳过已处理修订。
    revision_ids: Mapped[list] = mapped_column(JSON, default=list)
    attempt: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None]
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    finished_at: Mapped[datetime | None]
    activated_at: Mapped[datetime | None]


class DispatchRecord(Base):
    """派单记录。失败重试不重复派单：(list_id, ticket_id) 唯一。"""
    __tablename__ = 'dispatch_records'
    __table_args__ = (
        UniqueConstraint('list_id', 'ticket_id', name='uq_dispatch_list_ticket'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[int] = mapped_column(ForeignKey('triage_lists.id'), index=True)
    ticket_id: Mapped[str] = mapped_column(String(48), index=True)
    lane: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(300))
    reason: Mapped[str | None] = mapped_column(String(120))
    risk: Mapped[float | None]
    # 派单当时实际采用的实判依据（修订号与通道快照），重算不得改写。
    evidence_snapshot: Mapped[dict] = mapped_column(JSON)
    recipient: Mapped[str] = mapped_column(String(100))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
