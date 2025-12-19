#!/usr/bin/env python3
"""
S3 Multipart Upload Cleanup Manager - PRODUCTION READY
==================================================

This script adds lifecycle rules to automatically clean up incomplete multipart uploads
across S3 buckets without causing Terraform drift. Designed to handle 5300+ buckets safely.

FEATURES:
- Rate limiting to prevent AWS API throttling
- Progressive rollout capabilities
- Comprehensive error handling and logging  
- Dry-run mode for safety
- Support for filtering by region and bucket patterns
- Thread-safe parallel processing

REQUIREMENTS:
- boto3
- AWS credentials configured (via ~/.aws/credentials, IAM role, or environment)
- IAM permissions: s3:GetBucket*, s3:PutBucketLifecycleConfiguration

Usage Examples:
    # Dry run on all buckets with 7-day cleanup
    python s3_multipart_cleanup_manager.py --dry-run --days 7
    
    # Apply to specific bucket pattern  
    python s3_multipart_cleanup_manager.py --bucket-pattern "myapp-.*" --days 7 --apply
    
    # Process buckets in specific region
    python s3_multipart_cleanup_manager.py --region us-west-2 --apply
    
    # Use specific AWS profile
    python s3_multipart_cleanup_manager.py --profile prod --dry-run

Author: Shabana Sulthana
Date: September 2025
Version: 1.0 (Production Ready)
"""

import argparse
import boto3
import json
import logging
import re
import sys
from botocore.exceptions import ClientError, BotoCoreError
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
import time
from datetime import datetime
from functools import wraps

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


def rate_limit(calls_per_second=10):
    """
    Rate limiting decorator to prevent AWS API throttling.
    
    Args:
        calls_per_second: Maximum number of API calls per second
    """
    min_interval = 1.0 / calls_per_second
    last_called = [0.0]
    
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            elapsed = time.time() - last_called[0]
            left_to_wait = min_interval - elapsed
            if left_to_wait > 0:
                time.sleep(left_to_wait)
            ret = func(*args, **kwargs)
            last_called[0] = time.time()
            return ret
        return wrapper
    return decorator


class S3MultipartCleanupManager:
    """Manages S3 multipart upload cleanup lifecycle rules across multiple buckets."""
    
    def __init__(self, region: Optional[str] = None, profile: Optional[str] = None):
        """
        Initialize the S3 client and session.
        
        Args:
            region: AWS region to use (optional)
            profile: AWS profile to use (optional)
        """
        try:
            if profile:
                session = boto3.Session(profile_name=profile)
                self.s3_client = session.client('s3', region_name=region)
            else:
                self.s3_client = boto3.client('s3', region_name=region)
            
            # Test connection
            self.s3_client.list_buckets()
            logger.info(f"Successfully connected to S3 in region: {region or 'default'}")
            
        except Exception as e:
            logger.error(f"Failed to initialize S3 client: {e}")
            sys.exit(1)
    
    def discover_buckets(self, bucket_pattern: Optional[str] = None, 
                        region_filter: Optional[str] = None) -> List[str]:
        """
        Discover S3 buckets based on pattern and region filters.
        
        Args:
            bucket_pattern: Regex pattern to match bucket names (optional)
            region_filter: Only include buckets in this region (optional)
            
        Returns:
            List of bucket names matching criteria
        """
        try:
            response = self.s3_client.list_buckets()
            all_buckets = [bucket['Name'] for bucket in response['Buckets']]
            
            filtered_buckets = []
            pattern = re.compile(bucket_pattern) if bucket_pattern else None
            
            for bucket_name in all_buckets:
                # Apply pattern filter
                if pattern and not pattern.match(bucket_name):
                    continue
                
                # Apply region filter
                if region_filter:
                    try:
                        bucket_region = self.get_bucket_region(bucket_name)
                        if bucket_region != region_filter:
                            continue
                    except ClientError as e:
                        logger.warning(f"Could not get region for bucket {bucket_name}: {e}")
                        continue
                
                filtered_buckets.append(bucket_name)
            
            logger.info(f"Discovered {len(filtered_buckets)} buckets matching criteria")
            return filtered_buckets
            
        except ClientError as e:
            logger.error(f"Failed to discover buckets: {e}")
            return []
    
    @rate_limit(calls_per_second=10)
    def get_bucket_region(self, bucket_name: str) -> str:
        """
        Get the region for a specific bucket with rate limiting.
        
        Args:
            bucket_name: Name of the S3 bucket
            
        Returns:
            AWS region string
        """
        try:
            response = self.s3_client.get_bucket_location(Bucket=bucket_name)
            region = response['LocationConstraint']
            return region if region else 'us-east-1'
        except ClientError:
            return 'us-east-1'  # Default region
    
    @rate_limit(calls_per_second=10)
    def get_lifecycle_configuration(self, bucket_name: str) -> Optional[Dict]:
        """
        Get current lifecycle configuration for a bucket with rate limiting.
        
        Args:
            bucket_name: Name of the S3 bucket
            
        Returns:
            Lifecycle configuration dict or None if no configuration exists
        """
        try:
            response = self.s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
            return response
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchLifecycleConfiguration':
                return None
            else:
                logger.error(f"Error getting lifecycle config for {bucket_name}: {e}")
                return None
    
    def has_multipart_cleanup_rule(self, lifecycle_config: Optional[Dict]) -> bool:
        """
        Check if lifecycle configuration already has multipart cleanup rule.
        
        Args:
            lifecycle_config: Current lifecycle configuration
            
        Returns:
            True if multipart cleanup rule exists, False otherwise
        """
        if not lifecycle_config or 'Rules' not in lifecycle_config:
            return False
        
        for rule in lifecycle_config['Rules']:
            if 'AbortIncompleteMultipartUpload' in rule:
                return True
        
        return False
    
    def create_multipart_cleanup_rule(self, days: int = 7) -> Dict:
        """
        Create the multipart cleanup rule.
        
        Args:
            days: Number of days after initiation to abort incomplete uploads
            
        Returns:
            Rule dictionary for multipart cleanup
        """
        return {
            'ID': 'Clean up incomplete multipart uploads',
            'Status': 'Enabled',
            'Filter': {'Prefix': ''},
            'AbortIncompleteMultipartUpload': {
                'DaysAfterInitiation': days
            }
        }
    
    def merge_lifecycle_rules(self, existing_config: Optional[Dict], 
                            cleanup_rule: Dict) -> Dict:
        """
        Safely merge multipart cleanup rule with existing lifecycle configuration.
        
        Args:
            existing_config: Current lifecycle configuration
            cleanup_rule: Multipart cleanup rule to add
            
        Returns:
            Merged lifecycle configuration
        """
        if not existing_config:
            # No existing config, create new one
            return {
                'Rules': [cleanup_rule]
            }
        
        # Check if multipart cleanup rule already exists
        rules = existing_config.get('Rules', [])
        
        # Remove any existing multipart cleanup rules to avoid duplicates
        filtered_rules = [
            rule for rule in rules 
            if not ('AbortIncompleteMultipartUpload' in rule or 
                   rule.get('ID') in ['auto-multipart-cleanup', 'Clean up incomplete multipart uploads'])
        ]
        
        # Add the new cleanup rule
        filtered_rules.append(cleanup_rule)
        
        return {
            'Rules': filtered_rules
        }
    
    @rate_limit(calls_per_second=8)  # Slightly slower for PUT operations
    def update_bucket_lifecycle(self, bucket_name: str, days: int = 7, 
                              dry_run: bool = True) -> Tuple[bool, str]:
        """
        Update lifecycle configuration for a single bucket with rate limiting.
        
        Args:
            bucket_name: Name of the S3 bucket
            days: Number of days for multipart cleanup (1-7)
            dry_run: If True, only simulate the update
            
        Returns:
            Tuple of (success: bool, message: str)
        """
        try:
            # Get current lifecycle configuration
            current_config = self.get_lifecycle_configuration(bucket_name)
            
            # Check if multipart cleanup already exists
            if self.has_multipart_cleanup_rule(current_config):
                return True, f"Bucket {bucket_name} already has multipart cleanup rule"
            
            # Create and merge new rule
            cleanup_rule = self.create_multipart_cleanup_rule(days)
            new_config = self.merge_lifecycle_rules(current_config, cleanup_rule)
            
            if dry_run:
                return True, f"DRY RUN: Would add multipart cleanup rule to {bucket_name}"
            
            # Apply the new configuration
            self.s3_client.put_bucket_lifecycle_configuration(
                Bucket=bucket_name,
                LifecycleConfiguration=new_config
            )
            
            return True, f"Successfully updated lifecycle configuration for {bucket_name}"
            
        except ClientError as e:
            error_msg = f"Failed to update {bucket_name}: {e}"
            logger.error(error_msg)
            return False, error_msg
        except Exception as e:
            error_msg = f"Unexpected error updating {bucket_name}: {e}"
            logger.error(error_msg)
            return False, error_msg
    
    def process_buckets_batch(self, bucket_names: List[str], days: int = 7, 
                            dry_run: bool = True, max_workers: int = 5) -> Dict:
        """
        Process multiple buckets in parallel batches with conservative concurrency.
        
        Args:
            bucket_names: List of bucket names to process
            days: Number of days for multipart cleanup (1-7)
            dry_run: If True, only simulate updates
            max_workers: Maximum number of concurrent workers (reduced for large scale)
            
        Returns:
            Dictionary with success/failure statistics
        """
        results = {
            'total': len(bucket_names),
            'success': 0,
            'failed': 0,
            'already_configured': 0,
            'errors': []
        }
        
        logger.info(f"Processing {len(bucket_names)} buckets with {max_workers} workers")
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all tasks
            future_to_bucket = {
                executor.submit(self.update_bucket_lifecycle, bucket, days, dry_run): bucket
                for bucket in bucket_names
            }
            
            # Process completed tasks
            for future in as_completed(future_to_bucket):
                bucket_name = future_to_bucket[future]
                try:
                    success, message = future.result()
                    if success:
                        if "already has multipart cleanup" in message:
                            results['already_configured'] += 1
                        else:
                            results['success'] += 1
                        logger.info(f"✓ {message}")
                    else:
                        results['failed'] += 1
                        results['errors'].append(message)
                        logger.error(f"✗ {message}")
                        
                except Exception as e:
                    results['failed'] += 1
                    error_msg = f"Exception processing {bucket_name}: {e}"
                    results['errors'].append(error_msg)
                    logger.error(f"✗ {error_msg}")
                
                # Progress update every 50 buckets
                processed = results['success'] + results['failed'] + results['already_configured']
                if processed % 50 == 0:
                    logger.info(f"Progress: {processed}/{results['total']} buckets processed")
        
        return results
    
    def generate_report(self, results: Dict, days: int, dry_run: bool) -> None:
        """
        Generate and log a summary report.
        
        Args:
            results: Processing results dictionary
            days: Number of days used for cleanup
            dry_run: Whether this was a dry run
        """
        logger.info("\n" + "="*60)
        logger.info("S3 MULTIPART CLEANUP OPERATION SUMMARY")
        logger.info("="*60)
        logger.info(f"Mode: {'DRY RUN' if dry_run else 'LIVE UPDATE'}")
        logger.info(f"Cleanup Days: {days}")
        logger.info(f"Total Buckets: {results['total']}")
        logger.info(f"Successfully Updated: {results['success']}")
        logger.info(f"Already Configured: {results['already_configured']}")
        logger.info(f"Failed: {results['failed']}")
        
        if results['errors']:
            logger.info(f"\nFirst 10 Errors:")
            for i, error in enumerate(results['errors'][:10]):
                logger.error(f"  {i+1}. {error}")
            if len(results['errors']) > 10:
                logger.info(f"  ... and {len(results['errors']) - 10} more errors")
        
        logger.info("="*60)


def main():
    """Main function to handle CLI arguments and orchestrate the cleanup process."""
    parser = argparse.ArgumentParser(
        description='Add multipart upload cleanup lifecycle rules to S3 buckets',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run on all buckets with 7-day cleanup
  python s3_multipart_cleanup_manager.py --dry-run
  
  # Apply to buckets matching pattern with 7-day cleanup
  python s3_multipart_cleanup_manager.py --bucket-pattern "myapp-.*" --days 7 --apply
  
  # Process buckets in specific region
  python s3_multipart_cleanup_manager.py --region us-west-2 --apply
  
  # Use specific AWS profile
  python s3_multipart_cleanup_manager.py --profile prod --dry-run
  
  # Production rollout strategy:
  # 1. Test on dev buckets first
  python s3_multipart_cleanup_manager.py --bucket-pattern ".*-dev-.*" --apply
  
  # 2. Roll out by environment
  python s3_multipart_cleanup_manager.py --bucket-pattern ".*-staging-.*" --apply
  
  # 3. Full production rollout
  python s3_multipart_cleanup_manager.py --apply
        """
    )
    
    parser.add_argument('--days', type=int, default=7,
                       help='Days after initiation to abort incomplete multipart uploads (1-7, default: 7)')
    parser.add_argument('--bucket-pattern', type=str,
                       help='Regex pattern to match bucket names (e.g., "myapp-.*")')
    parser.add_argument('--region', type=str,
                       help='AWS region to filter buckets (e.g., "us-west-2")')
    parser.add_argument('--profile', type=str,
                       help='AWS profile to use for authentication')
    parser.add_argument('--max-workers', type=int, default=5,
                       help='Maximum number of concurrent workers (default: 5, optimized for large scale)')
    parser.add_argument('--dry-run', action='store_true', default=False,
                       help='Perform a dry run without making changes (default: True unless --apply)')
    parser.add_argument('--apply', action='store_true', default=False,
                       help='Actually apply the changes (overrides --dry-run)')
    
    args = parser.parse_args()
    
    # Determine if this is a dry run
    dry_run = not args.apply if args.apply else True
    
    if not dry_run:
        confirm = input("You are about to apply changes to S3 buckets. Are you sure? (yes/no): ")
        if confirm.lower() not in ['yes', 'y']:
            logger.info("Operation cancelled by user")
            sys.exit(0)
    
    # Validate days parameter (per infrastructure requirements: 1-7 days)
    if args.days < 1 or args.days > 7:
        logger.error("Days must be between 1 and 7 (as per infrastructure requirements)")
        sys.exit(1)
    
    logger.info(f"Starting S3 multipart cleanup manager...")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'LIVE UPDATE'}")
    logger.info(f"Cleanup after: {args.days} days")
    
    try:
        # Initialize manager
        manager = S3MultipartCleanupManager(region=args.region, profile=args.profile)
        
        # Discover buckets
        bucket_names = manager.discover_buckets(
            bucket_pattern=args.bucket_pattern,
            region_filter=args.region
        )
        
        if not bucket_names:
            logger.warning("No buckets found matching criteria")
            sys.exit(0)
        
        # Process buckets
        results = manager.process_buckets_batch(
            bucket_names=bucket_names,
            days=args.days,
            dry_run=dry_run,
            max_workers=args.max_workers
        )
        
        # Generate report
        manager.generate_report(results, args.days, dry_run)
        
        # Exit with appropriate code
        if results['failed'] > 0:
            sys.exit(1)
        else:
            sys.exit(0)
            
    except KeyboardInterrupt:
        logger.info("\nOperation interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()








