"""
Celery application wiring.

Run worker:
    celery -A app.tasks.celery_app worker -l info
"""

from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "closing_orchestration",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    beat_schedule={
        "reconcile-document-hash-dispatches": {
            "task": "cos.reconcile_document_hash_dispatches",
            "schedule": 60.0,
        }
    },
)

import app.tasks.jobs  # noqa: E402,F401 — register task modules
