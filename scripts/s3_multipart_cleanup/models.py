"""
Data models for S3 multipart cleanup: status enum, bucket result, checkpoint.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


class BucketStatus(Enum):
    """Status of processing a single bucket."""
    PENDING = "pending"
    SUCCESS = "success"
    ALREADY_CONFIGURED = "already_configured"
    SKIPPED_EXCEPTION = "skipped_exception"
    SKIPPED_CROSS_ACCOUNT = "skipped_cross_account"
    SKIPPED_OBJECT_LOCK = "skipped_object_lock"
    SKIPPED_SPECIAL = "skipped_special"
    SKIPPED_LIFECYCLE_LIMIT = "skipped_lifecycle_limit"
    FAILED_PERMISSION = "failed_permission"
    FAILED_THROTTLED = "failed_throttled"
    FAILED_ERROR = "failed_error"
    DRY_RUN = "dry_run"


@dataclass
class BucketResult:
    """Result of processing one bucket. account_id set in multi-account runs."""
    bucket_name: str
    status: BucketStatus
    message: str
    region: str = ""
    account_id: Optional[str] = None
    existing_days: Optional[int] = None
    applied_days: Optional[int] = None
    had_scoped_rules: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict:
        out = asdict(self)
        out['status'] = self.status.value
        return out


@dataclass
class CheckpointData:
    """Checkpoint for resume: config, processed bucket names, results."""
    started_at: str
    config: Dict
    processed_buckets: List[str]
    results: List[Dict]

    def save(self, filepath: str) -> None:
        import json
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, filepath: str) -> 'CheckpointData':
        import json
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls(**data)
