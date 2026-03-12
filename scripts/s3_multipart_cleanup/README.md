# S3 Multipart Cleanup package

One-sentence per file so readers can follow in a single pass:

| File | Purpose |
|------|--------|
| **models.py** | `BucketStatus` enum, `BucketResult` (per-bucket outcome), `CheckpointData` (resume state). |
| **constants.py** | `RULE_ID` (lifecycle rule we manage), `AWS_MANAGED_BUCKET_PATTERNS` (buckets we skip). |
| **retry.py** | `retry_with_backoff` decorator for AWS throttling (SlowDown, Throttling). |
| **manager.py** | `S3MultipartCleanupManagerV2`: init client → discover_buckets → preflight_check → update_bucket / process_buckets → generate_summary, export_results, print_report. |
| **cli.py** | `build_parser`, `run_single_account`, `run_multi_account`, `main` (parse args → run → report). |

**Entrypoint:** `../s3_multipart_cleanup_manager_v2.py` (sets up logging and calls `main()`).
