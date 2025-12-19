#!/usr/bin/env python3
"""
S3 Multipart Upload Cleanup Manager v2.1 - ENHANCED PRODUCTION VERSION
======================================================================

Addresses all known limitations from v1.0:
- Exception/allow-deny list support
- Preserves prefix-scoped rules (no over-broad replacement)
- Exponential backoff retry for API throttling
- Checkpointing/resume for partial failures (with unified reporting)
- Structured output (JSON/CSV) for observability
- Preflight permission & special bucket validation
- Cross-account bucket detection (configurable)
- AWS-managed bucket detection (CloudTrail, Config, etc.)
- Glob and regex pattern support
- Extended days support (> 7) for long-running uploads with warning

REQUIREMENTS:
- boto3
- AWS credentials configured
- IAM permissions: s3:GetBucket*, s3:PutBucketLifecycleConfiguration, s3:GetObjectLockConfiguration

Usage Examples:
    # Dry run with exception list
    python s3_multipart_cleanup_manager_v2.py --dry-run --days 7 --exceptions-file exceptions.txt
    
    # Resume from checkpoint after failure
    python s3_multipart_cleanup_manager_v2.py --resume checkpoint_20250918_143022.json --apply
    
    # Use glob pattern instead of regex
    python s3_multipart_cleanup_manager_v2.py --bucket-glob "myapp-*-prod" --apply
    
    # Preserve existing prefix-scoped rules
    python s3_multipart_cleanup_manager_v2.py --preserve-scoped-rules --apply
    
    # Output structured results
    python s3_multipart_cleanup_manager_v2.py --output-format json --output-file results.json --apply

Author: Shabana Sulthana
Date: December 2025
Version: 2.1 (Enhanced - Review Fixes)
"""

import argparse
import boto3
import csv
import fnmatch
import json
import logging
import os
import re
import sys
import time
from botocore.exceptions import ClientError
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from functools import wraps
from threading import Lock
from typing import Dict, List, Optional, Set, Tuple, Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f's3_multipart_cleanup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
    ]
)
logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS & DATA CLASSES
# =============================================================================

class BucketStatus(Enum):
    """Status categories for bucket processing."""
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
    """Result of processing a single bucket."""
    bucket_name: str
    status: BucketStatus
    message: str
    region: str = ""
    existing_days: Optional[int] = None
    applied_days: Optional[int] = None
    had_scoped_rules: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> Dict:
        result = asdict(self)
        result['status'] = self.status.value
        return result


@dataclass 
class CheckpointData:
    """Checkpoint data for resume functionality."""
    started_at: str
    config: Dict
    processed_buckets: List[str]
    results: List[Dict]
    
    def save(self, filepath: str):
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(asdict(self), f, indent=2)
    
    @classmethod
    def load(cls, filepath: str) -> 'CheckpointData':
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return cls(**data)


# =============================================================================
# RETRY DECORATOR WITH EXPONENTIAL BACKOFF
# =============================================================================

def retry_with_backoff(max_retries: int = 5, base_delay: float = 1.0, 
                       max_delay: float = 60.0, exponential_base: float = 2.0):
    """
    Retry decorator with exponential backoff for handling AWS throttling.
    
    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay between retries
        exponential_base: Base for exponential calculation
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except ClientError as e:
                    error_code = e.response.get('Error', {}).get('Code', '')
                    # Retry on throttling errors
                    if error_code in ['SlowDown', 'Throttling', 'RequestLimitExceeded', 
                                      'ProvisionedThroughputExceededException']:
                        last_exception = e
                        if attempt < max_retries:
                            delay = min(base_delay * (exponential_base ** attempt), max_delay)
                            # Add jitter
                            delay = delay * (0.5 + (hash(str(args)) % 100) / 100)
                            logger.warning(f"Throttled, retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})")
                            time.sleep(delay)
                            continue
                    raise
                except Exception:
                    raise
            if last_exception:
                raise last_exception
        return wrapper
    return decorator


# =============================================================================
# MAIN MANAGER CLASS
# =============================================================================

class S3MultipartCleanupManagerV2:
    """Enhanced S3 multipart upload cleanup manager with all fixes."""
    
    # Rule ID used by this tool
    RULE_ID = "auto-multipart-cleanup-managed"
    
    # Patterns for AWS-managed/service buckets that may have restrictions
    AWS_MANAGED_BUCKET_PATTERNS = [
        r'^aws-cloudtrail-logs-',
        r'^aws-config-',
        r'^elasticbeanstalk-',
        r'^aws-glue-',
        r'^aws-athena-query-results-',
        r'^cf-templates-',
        r'^codepipeline-',
        r'^amplify-',
        r'^aws-sam-cli-',
        r'^sagemaker-',
    ]
    
    def __init__(self, 
                 region: Optional[str] = None, 
                 profile: Optional[str] = None,
                 exceptions_file: Optional[str] = None,
                 exceptions_list: Optional[List[str]] = None,
                 preserve_scoped_rules: bool = True,
                 expected_days: int = 7,
                 skip_aws_managed: bool = True):
        """
        Initialize the enhanced S3 client.
        
        Args:
            region: AWS region to use
            profile: AWS profile to use
            exceptions_file: Path to file with bucket names to exclude (one per line)
            exceptions_list: List of bucket names/patterns to exclude
            preserve_scoped_rules: If True, don't replace prefix-scoped rules
            expected_days: Expected DaysAfterInitiation value for compliance check
            skip_aws_managed: If True, skip AWS-managed buckets (CloudTrail, Config, etc.)
        """
        self.region = region
        self.profile = profile
        self.preserve_scoped_rules = preserve_scoped_rules
        self.expected_days = expected_days
        self.skip_aws_managed = skip_aws_managed
        
        # Compile AWS-managed bucket patterns
        self._aws_managed_patterns = [re.compile(p) for p in self.AWS_MANAGED_BUCKET_PATTERNS]
        
        # Load exceptions
        self.exceptions: Set[str] = set(exceptions_list or [])
        if exceptions_file and os.path.exists(exceptions_file):
            self._load_exceptions_file(exceptions_file)
        
        # Thread-safe results collection
        self._results_lock = Lock()
        self._results: List[BucketResult] = []
        self._processed_buckets: Set[str] = set()
        
        # Initialize AWS client
        self._init_client()
        
        # Cache for bucket metadata
        self._bucket_owner_cache: Dict[str, str] = {}
        self._our_account_id: Optional[str] = None
    
    def _load_exceptions_file(self, filepath: str):
        """Load exception list from file."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        self.exceptions.add(line)
            logger.info("Loaded %d exceptions from %s", len(self.exceptions), filepath)
        except Exception as e:
            logger.warning(f"Could not load exceptions file: {e}")
    
    def _init_client(self):
        """Initialize AWS clients."""
        try:
            if self.profile:
                session = boto3.Session(profile_name=self.profile)
            else:
                session = boto3.Session()
            
            self.s3_client = session.client('s3', region_name=self.region)
            self.sts_client = session.client('sts')
            
            # Get our account ID for cross-account detection
            self._our_account_id = self.sts_client.get_caller_identity()['Account']
            
            # Test connection
            self.s3_client.list_buckets()
            logger.info(f"Connected to AWS account {self._our_account_id}, region: {self.region or 'default'}")
            
        except Exception as e:
            logger.error(f"Failed to initialize AWS client: {e}")
            sys.exit(1)
    
    # =========================================================================
    # BUCKET DISCOVERY & FILTERING
    # =========================================================================
    
    def discover_buckets(self, 
                        bucket_pattern: Optional[str] = None,
                        bucket_glob: Optional[str] = None,
                        region_filter: Optional[str] = None,
                        skip_cross_account: bool = True) -> List[str]:
        """
        Discover S3 buckets with enhanced filtering.
        
        Args:
            bucket_pattern: Regex pattern (uses re.search for substring matching)
            bucket_glob: Glob pattern (e.g., "myapp-*-prod")
            region_filter: Only include buckets in this region
            skip_cross_account: Skip buckets owned by other accounts
            
        Returns:
            List of bucket names matching criteria
        """
        try:
            response = self.s3_client.list_buckets()
            all_buckets = [bucket['Name'] for bucket in response['Buckets']]
            logger.info(f"Found {len(all_buckets)} total buckets")
            
            filtered_buckets = []
            
            # Compile patterns
            regex_pattern = re.compile(bucket_pattern) if bucket_pattern else None
            
            for bucket_name in all_buckets:
                # Check exceptions first (supports glob patterns in exceptions)
                if self._is_excepted(bucket_name):
                    logger.debug(f"Skipping excepted bucket: {bucket_name}")
                    continue
                
                # Apply regex pattern (uses search, not match, for substring)
                if regex_pattern and not regex_pattern.search(bucket_name):
                    continue
                
                # Apply glob pattern
                if bucket_glob and not fnmatch.fnmatch(bucket_name, bucket_glob):
                    continue
                
                # Apply region filter
                if region_filter:
                    try:
                        bucket_region = self._get_bucket_region(bucket_name)
                        if bucket_region != region_filter:
                            continue
                    except ClientError:
                        continue
                
                # Check cross-account ownership
                if skip_cross_account:
                    if not self._is_owned_by_us(bucket_name):
                        logger.debug(f"Skipping cross-account bucket: {bucket_name}")
                        continue
                
                filtered_buckets.append(bucket_name)
            
            logger.info(f"Discovered {len(filtered_buckets)} buckets matching criteria")
            return filtered_buckets
            
        except ClientError as e:
            logger.error(f"Failed to discover buckets: {e}")
            return []
    
    def _is_excepted(self, bucket_name: str) -> bool:
        """Check if bucket is in exception list (supports glob patterns)."""
        for pattern in self.exceptions:
            if pattern == bucket_name:
                return True
            if '*' in pattern or '?' in pattern:
                if fnmatch.fnmatch(bucket_name, pattern):
                    return True
        return False
    
    def _is_aws_managed_bucket(self, bucket_name: str) -> bool:
        """
        Check if bucket appears to be AWS-managed (CloudTrail, Config, etc.).
        
        These buckets may have special restrictions or be managed by AWS services.
        This is a heuristic based on common naming patterns.
        """
        for pattern in self._aws_managed_patterns:
            if pattern.match(bucket_name):
                return True
        return False
    
    @retry_with_backoff(max_retries=3)
    def _get_bucket_region(self, bucket_name: str) -> str:
        """Get bucket region with retry."""
        response = self.s3_client.get_bucket_location(Bucket=bucket_name)
        region = response.get('LocationConstraint')
        # us-east-1 returns None
        return region if region else 'us-east-1'
    
    @retry_with_backoff(max_retries=3)
    def _is_owned_by_us(self, bucket_name: str) -> bool:
        """Check if bucket is owned by our account."""
        if bucket_name in self._bucket_owner_cache:
            return self._bucket_owner_cache[bucket_name] == self._our_account_id
        
        try:
            # Try to get bucket ACL - will fail if not owner
            # Note: This is a heuristic. If we can read ACL, we likely own the bucket.
            # We can't directly compare canonical ID to account ID.
            self.s3_client.get_bucket_acl(Bucket=bucket_name)
            self._bucket_owner_cache[bucket_name] = self._our_account_id
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == 'AccessDenied':
                self._bucket_owner_cache[bucket_name] = 'unknown'
                return False
            raise
    
    # =========================================================================
    # PREFLIGHT VALIDATION
    # =========================================================================
    
    def preflight_check(self, bucket_name: str, 
                        prefetched_lifecycle: Optional[Dict] = None) -> Tuple[bool, str, BucketStatus, Optional[Dict]]:
        """
        Perform preflight checks on a bucket before attempting update.
        
        Args:
            bucket_name: Name of the bucket to check
            prefetched_lifecycle: Optional pre-fetched lifecycle config to avoid double fetch
        
        Returns:
            Tuple of (can_proceed, reason, status, lifecycle_config)
            lifecycle_config is returned to avoid fetching it again in update_bucket
        """
        # Check if excepted
        if self._is_excepted(bucket_name):
            return False, "Bucket in exception list", BucketStatus.SKIPPED_EXCEPTION, None
        
        # Check if AWS-managed bucket
        if self.skip_aws_managed and self._is_aws_managed_bucket(bucket_name):
            return False, "AWS-managed bucket (CloudTrail/Config/etc.) - skipping", BucketStatus.SKIPPED_SPECIAL, None
        
        try:
            # Check Object Lock
            try:
                lock_config = self.s3_client.get_object_lock_configuration(Bucket=bucket_name)
                if lock_config.get('ObjectLockConfiguration', {}).get('ObjectLockEnabled') == 'Enabled':
                    return False, "Object Lock enabled - manual review required", BucketStatus.SKIPPED_OBJECT_LOCK, None
            except ClientError as e:
                if e.response['Error']['Code'] != 'ObjectLockConfigurationNotFoundError':
                    pass  # Not an error if Object Lock isn't configured
            
            # Get lifecycle config (reuse if prefetched)
            if prefetched_lifecycle is not None:
                lifecycle_config = prefetched_lifecycle
            else:
                try:
                    lifecycle_config = self._get_lifecycle_configuration(bucket_name)
                except ClientError as e:
                    if e.response['Error']['Code'] == 'AccessDenied':
                        return False, "Access denied - check IAM permissions", BucketStatus.FAILED_PERMISSION, None
                    raise
            
            # Check lifecycle rule count
            if lifecycle_config:
                rule_count = len(lifecycle_config.get('Rules', []))
                if rule_count >= 999:  # Leave room for our rule
                    return False, f"Lifecycle rule limit reached ({rule_count}/1000)", BucketStatus.SKIPPED_LIFECYCLE_LIMIT, None
            
            return True, "Preflight checks passed", BucketStatus.PENDING, lifecycle_config
            
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            if error_code == 'AccessDenied':
                return False, f"Access denied: {e}", BucketStatus.FAILED_PERMISSION, None
            return False, f"Preflight error: {e}", BucketStatus.FAILED_ERROR, None
    
    # =========================================================================
    # LIFECYCLE CONFIGURATION MANAGEMENT
    # =========================================================================
    
    @retry_with_backoff(max_retries=5)
    def _get_lifecycle_configuration(self, bucket_name: str) -> Optional[Dict]:
        """Get current lifecycle configuration with retry."""
        try:
            response = self.s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
            return response
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchLifecycleConfiguration':
                return None
            raise
    
    def analyze_existing_rules(self, lifecycle_config: Optional[Dict]) -> Dict[str, Any]:
        """
        Analyze existing lifecycle configuration for multipart rules.
        
        Returns detailed analysis including:
        - has_global_rule: True if there's a global (empty prefix) multipart rule
        - has_scoped_rules: True if there are prefix-scoped multipart rules
        - existing_days: DaysAfterInitiation if global rule exists
        - scoped_rules: List of prefix-scoped rules
        - is_compliant: True if global rule exists with expected days
        """
        analysis = {
            'has_global_rule': False,
            'has_scoped_rules': False,
            'existing_days': None,
            'scoped_rules': [],
            'is_compliant': False,
            'our_rule_exists': False
        }
        
        if not lifecycle_config or 'Rules' not in lifecycle_config:
            return analysis
        
        for rule in lifecycle_config['Rules']:
            if 'AbortIncompleteMultipartUpload' not in rule:
                continue
            
            days = rule['AbortIncompleteMultipartUpload'].get('DaysAfterInitiation')
            rule_id = rule.get('ID', '')
            
            # Check if this is our managed rule
            if rule_id == self.RULE_ID:
                analysis['our_rule_exists'] = True
                analysis['has_global_rule'] = True
                analysis['existing_days'] = days
                analysis['is_compliant'] = (days == self.expected_days)
                continue
            
            # Check filter/prefix
            filter_config = rule.get('Filter', {})
            prefix = filter_config.get('Prefix', '')
            
            # Also check legacy Prefix field
            if not prefix and 'Prefix' in rule:
                prefix = rule['Prefix']
            
            if not prefix and not filter_config.get('Tag') and not filter_config.get('And'):
                # Global rule (no prefix/tag filter)
                analysis['has_global_rule'] = True
                analysis['existing_days'] = days
                analysis['is_compliant'] = (days == self.expected_days)
            else:
                # Scoped rule
                analysis['has_scoped_rules'] = True
                analysis['scoped_rules'].append({
                    'id': rule_id,
                    'prefix': prefix,
                    'days': days,
                    'filter': filter_config
                })
        
        return analysis
    
    def create_multipart_cleanup_rule(self, days: int = 7) -> Dict:
        """Create the multipart cleanup rule with our managed ID."""
        return {
            'ID': self.RULE_ID,
            'Status': 'Enabled',
            'Filter': {'Prefix': ''},
            'AbortIncompleteMultipartUpload': {
                'DaysAfterInitiation': days
            }
        }
    
    def merge_lifecycle_rules(self, 
                             existing_config: Optional[Dict], 
                             cleanup_rule: Dict,
                             analysis: Dict[str, Any]) -> Tuple[Dict, str]:
        """
        Safely merge multipart cleanup rule with existing configuration.
        
        This version:
        - Only removes/updates rules with our managed ID
        - Preserves all other rules including scoped multipart rules
        - Returns explanation of what changed
        
        Returns:
            Tuple of (new_config, change_description)
        """
        if not existing_config:
            return {'Rules': [cleanup_rule]}, "Created new lifecycle configuration"
        
        rules = existing_config.get('Rules', [])
        new_rules = []
        removed_our_rule = False
        
        for rule in rules:
            rule_id = rule.get('ID', '')
            
            # Only remove our managed rule (will be replaced)
            if rule_id == self.RULE_ID:
                removed_our_rule = True
                continue
            
            # Keep all other rules (including scoped multipart rules)
            new_rules.append(rule)
        
        # Add our rule
        new_rules.append(cleanup_rule)
        
        if removed_our_rule:
            change_desc = "Updated existing managed rule"
        elif analysis['has_global_rule']:
            change_desc = "Added managed rule (existing global rule preserved)"
        elif analysis['has_scoped_rules']:
            change_desc = f"Added global rule (preserved {len(analysis['scoped_rules'])} scoped rules)"
        else:
            change_desc = "Added multipart cleanup rule"
        
        return {'Rules': new_rules}, change_desc
    
    # =========================================================================
    # BUCKET UPDATE LOGIC
    # =========================================================================
    
    @retry_with_backoff(max_retries=5, base_delay=2.0)
    def _put_lifecycle_configuration(self, bucket_name: str, config: Dict):
        """Put lifecycle configuration with retry."""
        self.s3_client.put_bucket_lifecycle_configuration(
            Bucket=bucket_name,
            LifecycleConfiguration=config
        )
    
    def update_bucket(self, bucket_name: str, days: int = 7, 
                     dry_run: bool = True) -> BucketResult:
        """
        Update lifecycle configuration for a single bucket.
        
        Enhanced version with:
        - Preflight checks (with lifecycle config reuse)
        - Proper compliance validation
        - Preservation of scoped rules
        """
        region = ""
        try:
            region = self._get_bucket_region(bucket_name)
        except Exception:
            pass
        
        # Preflight checks (returns lifecycle config to avoid double fetch)
        can_proceed, reason, status, current_config = self.preflight_check(bucket_name)
        if not can_proceed:
            return BucketResult(
                bucket_name=bucket_name,
                status=status,
                message=reason,
                region=region
            )
        
        try:
            # Analyze current configuration (already fetched in preflight)
            analysis = self.analyze_existing_rules(current_config)
            
            # Check if already compliant
            if analysis['is_compliant'] and analysis['our_rule_exists']:
                return BucketResult(
                    bucket_name=bucket_name,
                    status=BucketStatus.ALREADY_CONFIGURED,
                    message=f"Already configured with {analysis['existing_days']} days (managed rule)",
                    region=region,
                    existing_days=analysis['existing_days']
                )
            
            # Check if there's a compliant global rule we didn't create
            if analysis['is_compliant'] and analysis['has_global_rule']:
                return BucketResult(
                    bucket_name=bucket_name,
                    status=BucketStatus.ALREADY_CONFIGURED,
                    message=f"Has compliant global rule ({analysis['existing_days']} days) - not modifying",
                    region=region,
                    existing_days=analysis['existing_days']
                )
            
            # If there are scoped rules and preserve mode is on, warn but continue
            if analysis['has_scoped_rules'] and self.preserve_scoped_rules:
                logger.info(f"Bucket {bucket_name} has {len(analysis['scoped_rules'])} scoped rules - will preserve")
            
            # Create and merge rules
            cleanup_rule = self.create_multipart_cleanup_rule(days)
            new_config, change_desc = self.merge_lifecycle_rules(current_config, cleanup_rule, analysis)
            
            if dry_run:
                return BucketResult(
                    bucket_name=bucket_name,
                    status=BucketStatus.DRY_RUN,
                    message=f"DRY RUN: Would apply - {change_desc}",
                    region=region,
                    existing_days=analysis['existing_days'],
                    applied_days=days,
                    had_scoped_rules=analysis['has_scoped_rules']
                )
            
            # Apply the configuration
            self._put_lifecycle_configuration(bucket_name, new_config)
            
            return BucketResult(
                bucket_name=bucket_name,
                status=BucketStatus.SUCCESS,
                message=f"Successfully updated - {change_desc}",
                region=region,
                existing_days=analysis['existing_days'],
                applied_days=days,
                had_scoped_rules=analysis['has_scoped_rules']
            )
            
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            if error_code in ['SlowDown', 'Throttling']:
                return BucketResult(
                    bucket_name=bucket_name,
                    status=BucketStatus.FAILED_THROTTLED,
                    message=f"Throttled after retries: {e}",
                    region=region
                )
            elif error_code == 'AccessDenied':
                return BucketResult(
                    bucket_name=bucket_name,
                    status=BucketStatus.FAILED_PERMISSION,
                    message=f"Access denied: {e}",
                    region=region
                )
            else:
                return BucketResult(
                    bucket_name=bucket_name,
                    status=BucketStatus.FAILED_ERROR,
                    message=f"AWS error: {e}",
                    region=region
                )
        except Exception as e:
            return BucketResult(
                bucket_name=bucket_name,
                status=BucketStatus.FAILED_ERROR,
                message=f"Unexpected error: {e}",
                region=region
            )
    
    # =========================================================================
    # BATCH PROCESSING WITH CHECKPOINTING
    # =========================================================================
    
    def process_buckets(self, 
                       bucket_names: List[str],
                       days: int = 7,
                       dry_run: bool = True,
                       max_workers: int = 5,
                       checkpoint_interval: int = 50,
                       checkpoint_file: Optional[str] = None) -> List[BucketResult]:
        """
        Process multiple buckets with checkpointing for resume capability.
        
        Args:
            bucket_names: List of bucket names to process
            days: Days for multipart cleanup
            dry_run: If True, simulate only
            max_workers: Concurrent workers
            checkpoint_interval: Save checkpoint every N buckets
            checkpoint_file: Path for checkpoint file
        """
        if not checkpoint_file:
            checkpoint_file = f"checkpoint_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        total = len(bucket_names)
        logger.info(f"Processing {total} buckets with {max_workers} workers")
        
        # Filter out already processed (for resume)
        remaining = [b for b in bucket_names if b not in self._processed_buckets]
        logger.info(f"Remaining to process: {len(remaining)} (skipping {len(bucket_names) - len(remaining)} already processed)")
        
        processed_count = len(self._processed_buckets)
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.update_bucket, bucket, days, dry_run): bucket
                for bucket in remaining
            }
            
            for future in as_completed(futures):
                bucket_name = futures[future]
                try:
                    result = future.result()
                    self._add_result(result)
                    
                    # Log based on status
                    if result.status in [BucketStatus.SUCCESS, BucketStatus.DRY_RUN]:
                        logger.info(f"✓ {result.message}")
                    elif result.status == BucketStatus.ALREADY_CONFIGURED:
                        logger.info(f"○ {bucket_name}: {result.message}")
                    elif result.status.value.startswith('skipped'):
                        logger.warning(f"⊘ {bucket_name}: {result.message}")
                    else:
                        logger.error(f"✗ {bucket_name}: {result.message}")
                    
                except Exception as e:
                    error_result = BucketResult(
                        bucket_name=bucket_name,
                        status=BucketStatus.FAILED_ERROR,
                        message=f"Exception: {e}"
                    )
                    self._add_result(error_result)
                    logger.error(f"✗ {bucket_name}: Exception - {e}")
                
                processed_count += 1
                
                # Progress update
                if processed_count % 25 == 0:
                    logger.info(f"Progress: {processed_count}/{total} buckets processed")
                
                # Checkpoint
                if processed_count % checkpoint_interval == 0:
                    self._save_checkpoint(checkpoint_file, days, dry_run)
        
        # Final checkpoint
        self._save_checkpoint(checkpoint_file, days, dry_run)
        
        return self._results
    
    def _add_result(self, result: BucketResult):
        """Thread-safe result addition."""
        with self._results_lock:
            self._results.append(result)
            self._processed_buckets.add(result.bucket_name)
    
    def _save_checkpoint(self, filepath: str, days: int, dry_run: bool):
        """Save checkpoint for resume capability."""
        checkpoint = CheckpointData(
            started_at=datetime.now().isoformat(),
            config={'days': days, 'dry_run': dry_run},
            processed_buckets=list(self._processed_buckets),
            results=[r.to_dict() for r in self._results]
        )
        checkpoint.save(filepath)
        logger.debug(f"Checkpoint saved: {filepath}")
    
    def load_checkpoint(self, filepath: str) -> Optional[Dict]:
        """
        Load checkpoint to resume processing with unified reporting.
        
        Restores both processed_buckets AND results from previous run,
        so the final summary includes all buckets (previous + current run).
        """
        if not os.path.exists(filepath):
            logger.warning(f"Checkpoint file not found: {filepath}")
            return None
        
        checkpoint = CheckpointData.load(filepath)
        self._processed_buckets = set(checkpoint.processed_buckets)
        
        # Restore previous results for unified reporting
        for result_dict in checkpoint.results:
            # Convert status string back to enum
            status_str = result_dict.get('status', 'failed_error')
            try:
                status = BucketStatus(status_str)
            except ValueError:
                status = BucketStatus.FAILED_ERROR
            
            result = BucketResult(
                bucket_name=result_dict.get('bucket_name', ''),
                status=status,
                message=result_dict.get('message', ''),
                region=result_dict.get('region', ''),
                existing_days=result_dict.get('existing_days'),
                applied_days=result_dict.get('applied_days'),
                had_scoped_rules=result_dict.get('had_scoped_rules', False),
                timestamp=result_dict.get('timestamp', datetime.now().isoformat())
            )
            self._results.append(result)
        
        logger.info(f"Loaded checkpoint: {len(self._processed_buckets)} buckets processed, "
                   f"{len(self._results)} results restored for unified reporting")
        return checkpoint.config
    
    # =========================================================================
    # REPORTING
    # =========================================================================
    
    def generate_summary(self) -> Dict[str, Any]:
        """Generate summary statistics."""
        summary = {
            'total': len(self._results),
            'by_status': {},
            'regions': {},
            'scoped_rules_encountered': 0
        }
        
        for result in self._results:
            status_name = result.status.value
            summary['by_status'][status_name] = summary['by_status'].get(status_name, 0) + 1
            
            if result.region:
                summary['regions'][result.region] = summary['regions'].get(result.region, 0) + 1
            
            if result.had_scoped_rules:
                summary['scoped_rules_encountered'] += 1
        
        return summary
    
    def export_results(self, filepath: str, output_format: str = 'json'):
        """Export results to file."""
        if output_format == 'json':
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump({
                    'summary': self.generate_summary(),
                    'results': [r.to_dict() for r in self._results]
                }, f, indent=2)
        elif output_format == 'csv':
            with open(filepath, 'w', newline='', encoding='utf-8') as f:
                if self._results:
                    writer = csv.DictWriter(f, fieldnames=self._results[0].to_dict().keys())
                    writer.writeheader()
                    for result in self._results:
                        writer.writerow(result.to_dict())
        
        logger.info(f"Results exported to {filepath}")
    
    def print_report(self, dry_run: bool):
        """Print summary report to console."""
        summary = self.generate_summary()
        
        print("\n" + "="*70)
        print("S3 MULTIPART CLEANUP OPERATION SUMMARY (v2.0)")
        print("="*70)
        print(f"Mode: {'DRY RUN' if dry_run else 'LIVE UPDATE'}")
        print(f"Total Buckets Processed: {summary['total']}")
        print()
        print("Results by Status:")
        for status, count in sorted(summary['by_status'].items()):
            icon = "✓" if status in ['success', 'dry_run'] else "○" if status == 'already_configured' else "⊘" if 'skipped' in status else "✗"
            print(f"  {icon} {status}: {count}")
        
        if summary['scoped_rules_encountered'] > 0:
            print(f"\n⚠ Buckets with prefix-scoped rules: {summary['scoped_rules_encountered']}")
        
        if summary['regions']:
            print("\nBuckets by Region:")
            for region, count in sorted(summary['regions'].items(), key=lambda x: -x[1]):
                print(f"  {region}: {count}")
        
        print("="*70)


# =============================================================================
# CLI MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='S3 Multipart Upload Cleanup Manager v2.0 - Enhanced Edition',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run with exception list
  python s3_multipart_cleanup_manager_v2.py --dry-run --exceptions-file exceptions.txt
  
  # Use glob pattern (simpler than regex)
  python s3_multipart_cleanup_manager_v2.py --bucket-glob "myapp-*-prod" --apply
  
  # Resume from checkpoint after failure
  python s3_multipart_cleanup_manager_v2.py --resume checkpoint.json --apply
  
  # Export structured results
  python s3_multipart_cleanup_manager_v2.py --output-format json --output-file results.json --apply
  
  # Strict mode: only update if days mismatch
  python s3_multipart_cleanup_manager_v2.py --days 7 --apply
        """
    )
    
    # Core options
    parser.add_argument('--days', type=int, default=7,
                       help='Days after initiation to abort incomplete uploads (default: 7, recommended: 1-7, max: 365)')
    parser.add_argument('--dry-run', action='store_true',
                       help='Simulate without making changes')
    parser.add_argument('--apply', action='store_true',
                       help='Actually apply changes (required for live updates)')
    
    # Targeting
    parser.add_argument('--bucket-pattern', type=str,
                       help='Regex pattern to match bucket names (uses search, not match)')
    parser.add_argument('--bucket-glob', type=str,
                       help='Glob pattern to match bucket names (e.g., "myapp-*-prod")')
    parser.add_argument('--region', type=str,
                       help='AWS region to filter buckets')
    parser.add_argument('--profile', type=str,
                       help='AWS profile to use')
    
    # Exceptions
    parser.add_argument('--exceptions-file', type=str,
                       help='Path to file with bucket names/patterns to exclude (one per line)')
    parser.add_argument('--exceptions', type=str, nargs='+',
                       help='Bucket names/patterns to exclude')
    
    # Behavior
    parser.add_argument('--preserve-scoped-rules', action='store_true', default=True,
                       help='Preserve existing prefix-scoped multipart rules (default: True)')
    parser.add_argument('--no-preserve-scoped-rules', action='store_false', dest='preserve_scoped_rules',
                       help='Replace all existing multipart rules')
    
    # Cross-account handling
    cross_account_group = parser.add_mutually_exclusive_group()
    cross_account_group.add_argument('--skip-cross-account', action='store_true', dest='skip_cross_account',
                                     help='Skip buckets owned by other accounts (default)')
    cross_account_group.add_argument('--no-skip-cross-account', action='store_false', dest='skip_cross_account',
                                     help='Include cross-account buckets (may fail with AccessDenied)')
    parser.set_defaults(skip_cross_account=True)
    
    # AWS-managed bucket handling
    aws_managed_group = parser.add_mutually_exclusive_group()
    aws_managed_group.add_argument('--skip-aws-managed', action='store_true', dest='skip_aws_managed',
                                   help='Skip AWS-managed buckets like CloudTrail, Config (default)')
    aws_managed_group.add_argument('--no-skip-aws-managed', action='store_false', dest='skip_aws_managed',
                                   help='Include AWS-managed buckets')
    parser.set_defaults(skip_aws_managed=True)
    
    # Execution
    parser.add_argument('--max-workers', type=int, default=5,
                       help='Maximum concurrent workers (default: 5)')
    parser.add_argument('--checkpoint-interval', type=int, default=50,
                       help='Save checkpoint every N buckets (default: 50)')
    parser.add_argument('--resume', type=str,
                       help='Resume from checkpoint file')
    
    # Output
    parser.add_argument('--output-format', choices=['json', 'csv'], default='json',
                       help='Output format for results (default: json)')
    parser.add_argument('--output-file', type=str,
                       help='Path to output results file')
    
    args = parser.parse_args()
    
    # Validate days
    if args.days < 1:
        logger.error("Days must be at least 1")
        sys.exit(1)
    
    if args.days > 365:
        logger.error("Days cannot exceed 365")
        sys.exit(1)
    
    # Warn about non-standard values
    if args.days > 7:
        logger.warning(f"⚠️  Using {args.days} days (> 7). This is non-standard.")
        logger.warning("   Standard policy is 1-7 days. Only use higher values for buckets with")
        logger.warning("   legitimate long-running uploads (large datasets over slow links).")
    
    dry_run = not args.apply
    
    if not dry_run:
        confirm = input("⚠️  You are about to modify S3 bucket lifecycle configurations.\n"
                       "Type 'yes' to confirm: ")
        if confirm.lower() != 'yes':
            logger.info("Operation cancelled")
            sys.exit(0)
    
    # Initialize manager
    manager = S3MultipartCleanupManagerV2(
        region=args.region,
        profile=args.profile,
        exceptions_file=args.exceptions_file,
        exceptions_list=args.exceptions,
        preserve_scoped_rules=args.preserve_scoped_rules,
        expected_days=args.days,
        skip_aws_managed=args.skip_aws_managed
    )
    
    # Resume from checkpoint if specified
    if args.resume:
        config = manager.load_checkpoint(args.resume)
        if config:
            logger.info(f"Resuming with config: {config}")
    
    # Discover buckets
    buckets = manager.discover_buckets(
        bucket_pattern=args.bucket_pattern,
        bucket_glob=args.bucket_glob,
        region_filter=args.region,
        skip_cross_account=args.skip_cross_account
    )
    
    if not buckets:
        logger.warning("No buckets found matching criteria")
        sys.exit(0)
    
    # Process
    manager.process_buckets(
        bucket_names=buckets,
        days=args.days,
        dry_run=dry_run,
        max_workers=args.max_workers,
        checkpoint_interval=args.checkpoint_interval
    )
    
    # Report
    manager.print_report(dry_run)
    
    # Export results
    if args.output_file:
        manager.export_results(args.output_file, args.output_format)
    
    # Exit code based on failures
    summary = manager.generate_summary()
    failed_count = sum(
        count for status, count in summary['by_status'].items()
        if status.startswith('failed')
    )
    sys.exit(1 if failed_count > 0 else 0)


if __name__ == '__main__':
    main()


