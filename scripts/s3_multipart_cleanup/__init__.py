"""
S3 Multipart Cleanup v2.1: add lifecycle rule to abort incomplete multipart uploads.

Package layout:
  models     - BucketStatus, BucketResult, CheckpointData
  constants  - RULE_ID, AWS_MANAGED_BUCKET_PATTERNS
  retry      - retry_with_backoff (AWS throttling)
  manager    - S3MultipartCleanupManagerV2 (discover, preflight, update, report)
  cli        - build_parser, main (single/multi-account)
"""
from .cli import main
from .manager import S3MultipartCleanupManagerV2
from .models import BucketResult, BucketStatus, CheckpointData

__all__ = [
    'main',
    'S3MultipartCleanupManagerV2',
    'BucketResult',
    'BucketStatus',
    'CheckpointData',
]
