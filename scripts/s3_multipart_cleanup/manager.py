"""
S3 Multipart Cleanup Manager: discover buckets, preflight, merge lifecycle rules, update, report.

Flow: init client -> discover_buckets() -> process_buckets() -> print_report() / export_results().
"""
import csv
import fnmatch
import json
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Optional, Set, Tuple

import boto3
from botocore.exceptions import ClientError

from .constants import AWS_MANAGED_BUCKET_PATTERNS, RULE_ID
from .models import BucketResult, BucketStatus, CheckpointData
from .retry import retry_with_backoff

logger = logging.getLogger(__name__)


class S3MultipartCleanupManagerV2:
    """
    Add/update S3 lifecycle rule to abort incomplete multipart uploads after N days.
    Supports single account (profile) or multi-account via --profiles-file (caller loops).
    """

    RULE_ID = RULE_ID

    def __init__(
        self,
        region: Optional[str] = None,
        profile: Optional[str] = None,
        exceptions_file: Optional[str] = None,
        exceptions_list: Optional[List[str]] = None,
        preserve_scoped_rules: bool = True,
        expected_days: int = 7,
        skip_aws_managed: bool = True,
    ):
        self.region = region
        self.profile = profile
        self.preserve_scoped_rules = preserve_scoped_rules
        self.expected_days = expected_days
        self.skip_aws_managed = skip_aws_managed
        self._aws_managed_patterns = [re.compile(p) for p in AWS_MANAGED_BUCKET_PATTERNS]
        self.exceptions: Set[str] = set(exceptions_list or [])
        if exceptions_file and os.path.exists(exceptions_file):
            self._load_exceptions_file(exceptions_file)
        self._results_lock = Lock()
        self._results: List[BucketResult] = []
        self._processed_buckets: Set[str] = set()
        self._bucket_owner_cache: Dict[str, str] = {}
        self._our_account_id: Optional[str] = None
        self._init_client()

    def _load_exceptions_file(self, filepath: str) -> None:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    self.exceptions.add(line)
        logger.info("Loaded %d exceptions from %s", len(self.exceptions), filepath)

    def _init_client(self) -> None:
        try:
            session = boto3.Session(profile_name=self.profile) if self.profile else boto3.Session()
            self.s3_client = session.client('s3', region_name=self.region)
            self.sts_client = session.client('sts')
            self._our_account_id = self.sts_client.get_caller_identity()['Account']
            self.s3_client.list_buckets()
            logger.info("Connected to AWS account %s, region %s", self._our_account_id, self.region or 'default')
        except Exception as e:
            logger.error("Failed to initialize AWS client: %s", e)
            sys.exit(1)

    # ---------- Discovery ----------

    def discover_buckets(
        self,
        bucket_pattern: Optional[str] = None,
        bucket_glob: Optional[str] = None,
        region_filter: Optional[str] = None,
        skip_cross_account: bool = True,
    ) -> List[str]:
        """
        List buckets in current account and filter by exceptions, regex, glob, region.
        list_buckets() only returns current account; skip_cross_account is for future external list.
        """
        try:
            response = self.s3_client.list_buckets()
            all_buckets = [b['Name'] for b in response['Buckets']]
            logger.info("Found %d total buckets in account %s", len(all_buckets), self._our_account_id)
            regex = re.compile(bucket_pattern) if bucket_pattern else None
            out = []
            for name in all_buckets:
                if self._is_excepted(name):
                    continue
                if regex and not regex.search(name):
                    continue
                if bucket_glob and not fnmatch.fnmatch(name, bucket_glob):
                    continue
                if region_filter:
                    try:
                        if self._get_bucket_region(name) != region_filter:
                            continue
                    except ClientError:
                        continue
                out.append(name)
            logger.info("Discovered %d buckets matching criteria", len(out))
            return out
        except ClientError as e:
            logger.error("Failed to discover buckets: %s", e)
            return []

    def _is_excepted(self, bucket_name: str) -> bool:
        for pattern in self.exceptions:
            if pattern == bucket_name or ('*' in pattern or '?' in pattern) and fnmatch.fnmatch(bucket_name, pattern):
                return True
        return False

    def _is_aws_managed_bucket(self, bucket_name: str) -> bool:
        return any(p.match(bucket_name) for p in self._aws_managed_patterns)

    @retry_with_backoff(max_retries=3)
    def _get_bucket_region(self, bucket_name: str) -> str:
        r = self.s3_client.get_bucket_location(Bucket=bucket_name).get('LocationConstraint')
        return r if r else 'us-east-1'

    @retry_with_backoff(max_retries=3)
    def _is_owned_by_us(self, bucket_name: str) -> bool:
        if bucket_name in self._bucket_owner_cache:
            return self._bucket_owner_cache[bucket_name] == self._our_account_id
        try:
            self.s3_client.get_bucket_acl(Bucket=bucket_name)
            self._bucket_owner_cache[bucket_name] = self._our_account_id
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == 'AccessDenied':
                self._bucket_owner_cache[bucket_name] = 'unknown'
                return False
            raise

    # ---------- Preflight ----------

    def preflight_check(
        self,
        bucket_name: str,
        prefetched_lifecycle: Optional[Dict] = None,
    ) -> Tuple[bool, str, BucketStatus, Optional[Dict]]:
        """Returns (can_proceed, reason, status, lifecycle_config)."""
        if self._is_excepted(bucket_name):
            return False, "Bucket in exception list", BucketStatus.SKIPPED_EXCEPTION, None
        if self.skip_aws_managed and self._is_aws_managed_bucket(bucket_name):
            return False, "AWS-managed bucket - skipping", BucketStatus.SKIPPED_SPECIAL, None
        try:
            try:
                lock = self.s3_client.get_object_lock_configuration(Bucket=bucket_name)
                if lock.get('ObjectLockConfiguration', {}).get('ObjectLockEnabled') == 'Enabled':
                    return False, "Object Lock enabled", BucketStatus.SKIPPED_OBJECT_LOCK, None
            except ClientError as e:
                if e.response['Error']['Code'] != 'ObjectLockConfigurationNotFoundError':
                    pass
            lifecycle_config = prefetched_lifecycle if prefetched_lifecycle is not None else self._get_lifecycle_configuration(bucket_name)
            if lifecycle_config and len(lifecycle_config.get('Rules', [])) >= 999:
                return False, "Lifecycle rule limit reached", BucketStatus.SKIPPED_LIFECYCLE_LIMIT, None
            return True, "Preflight passed", BucketStatus.PENDING, lifecycle_config
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code', 'Unknown')
            if code == 'AccessDenied':
                return False, f"Access denied: {e}", BucketStatus.FAILED_PERMISSION, None
            return False, f"Preflight error: {e}", BucketStatus.FAILED_ERROR, None

    # ---------- Lifecycle ----------

    @retry_with_backoff(max_retries=5)
    def _get_lifecycle_configuration(self, bucket_name: str) -> Optional[Dict]:
        try:
            return self.s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchLifecycleConfiguration':
                return None
            raise

    def analyze_existing_rules(self, lifecycle_config: Optional[Dict]) -> Dict[str, Any]:
        """Analyze multipart rules: has_global_rule, has_scoped_rules, existing_days, is_compliant, our_rule_exists."""
        analysis = {
            'has_global_rule': False, 'has_scoped_rules': False, 'existing_days': None,
            'scoped_rules': [], 'is_compliant': False, 'our_rule_exists': False,
        }
        if not lifecycle_config or 'Rules' not in lifecycle_config:
            return analysis
        for rule in lifecycle_config['Rules']:
            if 'AbortIncompleteMultipartUpload' not in rule:
                continue
            days = rule['AbortIncompleteMultipartUpload'].get('DaysAfterInitiation')
            rid = rule.get('ID', '')
            if rid == self.RULE_ID:
                analysis['our_rule_exists'] = analysis['has_global_rule'] = True
                analysis['existing_days'] = days
                analysis['is_compliant'] = (days == self.expected_days)
                continue
            prefix = rule.get('Filter', {}).get('Prefix', '') or rule.get('Prefix', '')
            filt = rule.get('Filter', {})
            if not prefix and not filt.get('Tag') and not filt.get('And'):
                analysis['has_global_rule'] = True
                analysis['existing_days'] = days
                analysis['is_compliant'] = (days == self.expected_days)
            else:
                analysis['has_scoped_rules'] = True
                analysis['scoped_rules'].append({'id': rid, 'prefix': prefix, 'days': days, 'filter': filt})
        return analysis

    def create_multipart_cleanup_rule(self, days: int = 7) -> Dict:
        return {
            'ID': self.RULE_ID, 'Status': 'Enabled', 'Filter': {'Prefix': ''},
            'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': days},
        }

    def merge_lifecycle_rules(
        self,
        existing_config: Optional[Dict],
        cleanup_rule: Dict,
        analysis: Dict[str, Any],
    ) -> Tuple[Dict, str]:
        """Merge our rule into config; preserve others. Returns (new_config, change_description)."""
        if not existing_config:
            return {'Rules': [cleanup_rule]}, "Created new lifecycle configuration"
        rules = [r for r in existing_config.get('Rules', []) if r.get('ID') != self.RULE_ID]
        rules.append(cleanup_rule)
        if analysis['our_rule_exists']:
            desc = "Updated existing managed rule"
        elif analysis['has_global_rule']:
            desc = "Added managed rule (existing global preserved)"
        elif analysis['has_scoped_rules']:
            desc = f"Added global rule (preserved {len(analysis['scoped_rules'])} scoped)"
        else:
            desc = "Added multipart cleanup rule"
        return {'Rules': rules}, desc

    # ---------- Update one bucket ----------

    @retry_with_backoff(max_retries=5, base_delay=2.0)
    def _put_lifecycle_configuration(self, bucket_name: str, config: Dict) -> None:
        self.s3_client.put_bucket_lifecycle_configuration(Bucket=bucket_name, LifecycleConfiguration=config)

    def update_bucket(self, bucket_name: str, days: int = 7, dry_run: bool = True) -> BucketResult:
        """Preflight, analyze, merge, then put (or dry-run)."""
        region = ""
        try:
            region = self._get_bucket_region(bucket_name)
        except Exception:
            pass
        can_proceed, reason, status, current_config = self.preflight_check(bucket_name)
        if not can_proceed:
            return BucketResult(bucket_name=bucket_name, status=status, message=reason, region=region)
        try:
            analysis = self.analyze_existing_rules(current_config)
            if analysis['is_compliant'] and analysis['our_rule_exists']:
                return BucketResult(
                    bucket_name=bucket_name, status=BucketStatus.ALREADY_CONFIGURED,
                    message=f"Already configured with {analysis['existing_days']} days (managed rule)",
                    region=region, existing_days=analysis['existing_days'],
                )
            if analysis['is_compliant'] and analysis['has_global_rule']:
                return BucketResult(
                    bucket_name=bucket_name, status=BucketStatus.ALREADY_CONFIGURED,
                    message=f"Has compliant global rule ({analysis['existing_days']} days) - not modifying",
                    region=region, existing_days=analysis['existing_days'],
                )
            if analysis['has_scoped_rules'] and self.preserve_scoped_rules:
                logger.info("Bucket %s has %d scoped rules - will preserve", bucket_name, len(analysis['scoped_rules']))
            cleanup_rule = self.create_multipart_cleanup_rule(days)
            new_config, change_desc = self.merge_lifecycle_rules(current_config, cleanup_rule, analysis)
            if dry_run:
                return BucketResult(
                    bucket_name=bucket_name, status=BucketStatus.DRY_RUN,
                    message=f"DRY RUN: Would apply - {change_desc}",
                    region=region, existing_days=analysis['existing_days'], applied_days=days,
                    had_scoped_rules=analysis['has_scoped_rules'],
                )
            self._put_lifecycle_configuration(bucket_name, new_config)
            return BucketResult(
                bucket_name=bucket_name, status=BucketStatus.SUCCESS, message=f"Successfully updated - {change_desc}",
                region=region, existing_days=analysis['existing_days'], applied_days=days,
                had_scoped_rules=analysis['has_scoped_rules'],
            )
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code', 'Unknown')
            if code in ('SlowDown', 'Throttling'):
                return BucketResult(bucket_name=bucket_name, status=BucketStatus.FAILED_THROTTLED, message=f"Throttled: {e}", region=region)
            if code == 'AccessDenied':
                return BucketResult(bucket_name=bucket_name, status=BucketStatus.FAILED_PERMISSION, message=f"Access denied: {e}", region=region)
            return BucketResult(bucket_name=bucket_name, status=BucketStatus.FAILED_ERROR, message=f"AWS error: {e}", region=region)
        except Exception as e:
            return BucketResult(bucket_name=bucket_name, status=BucketStatus.FAILED_ERROR, message=f"Unexpected: {e}", region=region)

    # ---------- Batch + checkpoint ----------

    def process_buckets(
        self,
        bucket_names: List[str],
        days: int = 7,
        dry_run: bool = True,
        max_workers: int = 5,
        checkpoint_interval: int = 50,
        checkpoint_file: Optional[str] = None,
    ) -> List[BucketResult]:
        """Process buckets in parallel; checkpoint every checkpoint_interval."""
        if not checkpoint_file:
            checkpoint_file = f"checkpoint_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        total = len(bucket_names)
        remaining = [b for b in bucket_names if b not in self._processed_buckets]
        logger.info("Processing %d buckets (%d remaining), workers=%d", total, len(remaining), max_workers)
        processed_count = len(self._processed_buckets)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self.update_bucket, b, days, dry_run): b for b in remaining}
            for future in as_completed(futures):
                bucket_name = futures[future]
                try:
                    result = future.result()
                    self._add_result(result)
                    if result.status in (BucketStatus.SUCCESS, BucketStatus.DRY_RUN):
                        logger.info("✓ %s", result.message)
                    elif result.status == BucketStatus.ALREADY_CONFIGURED:
                        logger.info("○ %s: %s", bucket_name, result.message)
                    elif result.status.value.startswith('skipped'):
                        logger.warning("⊘ %s: %s", bucket_name, result.message)
                    else:
                        logger.error("✗ %s: %s", bucket_name, result.message)
                except Exception as e:
                    self._add_result(BucketResult(bucket_name=bucket_name, status=BucketStatus.FAILED_ERROR, message=f"Exception: {e}"))
                    logger.error("✗ %s: Exception - %s", bucket_name, e)
                processed_count += 1
                if processed_count % 25 == 0:
                    logger.info("Progress: %d/%d", processed_count, total)
                if processed_count % checkpoint_interval == 0:
                    self._save_checkpoint(checkpoint_file, days, dry_run)
        self._save_checkpoint(checkpoint_file, days, dry_run)
        return self._results

    def _add_result(self, result: BucketResult) -> None:
        with self._results_lock:
            if result.account_id is None and self._our_account_id:
                result.account_id = self._our_account_id
            self._results.append(result)
            self._processed_buckets.add(result.bucket_name)

    def _save_checkpoint(self, filepath: str, days: int, dry_run: bool) -> None:
        CheckpointData(
            started_at=datetime.now().isoformat(),
            config={'days': days, 'dry_run': dry_run},
            processed_buckets=list(self._processed_buckets),
            results=[r.to_dict() for r in self._results],
        ).save(filepath)
        logger.debug("Checkpoint saved: %s", filepath)

    def load_checkpoint(self, filepath: str) -> Optional[Dict]:
        """Restore processed_buckets and results from checkpoint for resume."""
        if not os.path.exists(filepath):
            logger.warning("Checkpoint file not found: %s", filepath)
            return None
        cp = CheckpointData.load(filepath)
        self._processed_buckets = set(cp.processed_buckets)
        for d in cp.results:
            try:
                status = BucketStatus(d.get('status', 'failed_error'))
            except ValueError:
                status = BucketStatus.FAILED_ERROR
            self._results.append(BucketResult(
                bucket_name=d.get('bucket_name', ''),
                status=status,
                message=d.get('message', ''),
                region=d.get('region', ''),
                account_id=d.get('account_id'),
                existing_days=d.get('existing_days'),
                applied_days=d.get('applied_days'),
                had_scoped_rules=d.get('had_scoped_rules', False),
                timestamp=d.get('timestamp', datetime.now().isoformat()),
            ))
        logger.info("Loaded checkpoint: %d buckets, %d results", len(self._processed_buckets), len(self._results))
        return cp.config

    # ---------- Reporting ----------

    def generate_summary(self) -> Dict[str, Any]:
        summary = {'total': len(self._results), 'by_status': {}, 'by_account': {}, 'regions': {}, 'scoped_rules_encountered': 0}
        for r in self._results:
            summary['by_status'][r.status.value] = summary['by_status'].get(r.status.value, 0) + 1
            if r.account_id:
                summary['by_account'][r.account_id] = summary['by_account'].get(r.account_id, 0) + 1
            if r.region:
                summary['regions'][r.region] = summary['regions'].get(r.region, 0) + 1
            if r.had_scoped_rules:
                summary['scoped_rules_encountered'] += 1
        return summary

    def export_results(self, filepath: str, output_format: str = 'json') -> None:
        if output_format == 'json':
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump({'summary': self.generate_summary(), 'results': [r.to_dict() for r in self._results]}, f, indent=2)
        else:
            with open(filepath, 'w', newline='', encoding='utf-8') as f:
                if self._results:
                    w = csv.DictWriter(f, fieldnames=self._results[0].to_dict().keys())
                    w.writeheader()
                    for r in self._results:
                        w.writerow(r.to_dict())
        logger.info("Results exported to %s", filepath)

    def print_report(self, dry_run: bool) -> None:
        s = self.generate_summary()
        print("\n" + "=" * 70)
        print("S3 MULTIPART CLEANUP OPERATION SUMMARY (v2.1)")
        print("=" * 70)
        print("Mode:", "DRY RUN" if dry_run else "LIVE UPDATE")
        print("Total Buckets Processed:", s['total'])
        print("\nResults by Status:")
        for status, count in sorted(s['by_status'].items()):
            icon = "✓" if status in ('success', 'dry_run') else "○" if status == 'already_configured' else "⊘" if 'skipped' in status else "✗"
            print(f"  {icon} {status}: {count}")
        if s['scoped_rules_encountered'] > 0:
            print(f"\n⚠ Buckets with prefix-scoped rules: {s['scoped_rules_encountered']}")
        if s.get('by_account'):
            print("\nBuckets by Account:")
            for aid, count in sorted(s['by_account'].items(), key=lambda x: -x[1]):
                print(f"  {aid}: {count}")
        if s['regions']:
            print("\nBuckets by Region:")
            for reg, count in sorted(s['regions'].items(), key=lambda x: -x[1]):
                print(f"  {reg}: {count}")
        print("=" * 70)
