"""
CLI for S3 multipart cleanup: argparse, validation, single/multi-account orchestration.
"""
import argparse
import os
import sys

from .manager import S3MultipartCleanupManagerV2
from .models import BucketStatus


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='S3 Multipart Upload Cleanup Manager v2.1 - Org-level support',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python s3_multipart_cleanup_manager_v2.py --dry-run --exceptions-file config/exceptions.txt
  python s3_multipart_cleanup_manager_v2.py --bucket-glob "myapp-*-prod" --apply
  python s3_multipart_cleanup_manager_v2.py --resume checkpoint.json --apply
  python s3_multipart_cleanup_manager_v2.py --profiles-file config/profiles.txt --output-file results.json --apply
        """,
    )
    parser.add_argument('--days', type=int, default=7, help='Days to abort incomplete multipart (1-365, default 7)')
    parser.add_argument('--dry-run', action='store_true', help='Simulate only')
    parser.add_argument('--apply', action='store_true', help='Apply changes')
    parser.add_argument('--bucket-pattern', type=str, help='Regex for bucket names')
    parser.add_argument('--bucket-glob', type=str, help='Glob for bucket names (e.g. myapp-*-prod)')
    parser.add_argument('--region', type=str, help='Filter by region')
    parser.add_argument('--profile', type=str, help='AWS profile (single account)')
    parser.add_argument('--exceptions-file', type=str, help='File with bucket names/patterns to exclude')
    parser.add_argument('--exceptions', type=str, nargs='+', help='Inline exceptions')
    parser.add_argument('--preserve-scoped-rules', action='store_true', default=True, help='Keep prefix-scoped rules (default)')
    parser.add_argument('--no-preserve-scoped-rules', action='store_false', dest='preserve_scoped_rules')
    parser.add_argument('--profiles-file', type=str, help='One AWS profile per line for multi-account run')
    parser.add_argument('--skip-aws-managed', action='store_true', dest='skip_aws_managed', default=True)
    parser.add_argument('--no-skip-aws-managed', action='store_false', dest='skip_aws_managed')
    parser.add_argument('--max-workers', type=int, default=5, help='Concurrent workers')
    parser.add_argument('--checkpoint-interval', type=int, default=50)
    parser.add_argument('--resume', type=str, help='Resume from checkpoint file')
    parser.add_argument('--output-format', choices=['json', 'csv'], default='json')
    parser.add_argument('--output-file', type=str)
    return parser


def run_single_account(args: argparse.Namespace, dry_run: bool) -> S3MultipartCleanupManagerV2:
    """Run in one account (profile or default creds)."""
    manager = S3MultipartCleanupManagerV2(
        region=args.region,
        profile=args.profile,
        exceptions_file=args.exceptions_file,
        exceptions_list=args.exceptions,
        preserve_scoped_rules=args.preserve_scoped_rules,
        expected_days=args.days,
        skip_aws_managed=args.skip_aws_managed,
    )
    if args.resume:
        manager.load_checkpoint(args.resume)
    buckets = manager.discover_buckets(
        bucket_pattern=args.bucket_pattern,
        bucket_glob=args.bucket_glob,
        region_filter=args.region,
    )
    if not buckets:
        return manager
    manager.process_buckets(
        bucket_names=buckets,
        days=args.days,
        dry_run=dry_run,
        max_workers=args.max_workers,
        checkpoint_interval=args.checkpoint_interval,
    )
    return manager


def run_multi_account(args: argparse.Namespace, dry_run: bool) -> S3MultipartCleanupManagerV2:
    """Run in each profile from --profiles-file; combine results into one manager."""
    with open(args.profiles_file, 'r', encoding='utf-8') as f:
        profiles = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
    if not profiles:
        raise SystemExit("No profile names in %s" % args.profiles_file)
    import logging
    logger = logging.getLogger(__name__)
    logger.info("Multi-account: %d profiles from %s", len(profiles), args.profiles_file)
    if args.resume:
        logger.warning("--resume ignored with --profiles-file")
    combined = None
    for idx, profile_name in enumerate(profiles, 1):
        logger.info("=== Account %d/%d: profile %s ===", idx, len(profiles), profile_name)
        manager = S3MultipartCleanupManagerV2(
            region=args.region,
            profile=profile_name,
            exceptions_file=args.exceptions_file,
            exceptions_list=args.exceptions,
            preserve_scoped_rules=args.preserve_scoped_rules,
            expected_days=args.days,
            skip_aws_managed=args.skip_aws_managed,
        )
        buckets = manager.discover_buckets(
            bucket_pattern=args.bucket_pattern,
            bucket_glob=args.bucket_glob,
            region_filter=args.region,
        )
        if not buckets:
            if combined is None:
                combined = manager
            continue
        manager.process_buckets(
            bucket_names=buckets,
            days=args.days,
            dry_run=dry_run,
            max_workers=args.max_workers,
            checkpoint_interval=args.checkpoint_interval,
        )
        if combined is None:
            combined = manager
        else:
            combined._results.extend(manager._results)
            combined._processed_buckets.update(manager._processed_buckets)
    if combined is None:
        raise SystemExit("No buckets processed in any account")
    return combined


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.days < 1 or args.days > 365:
        raise SystemExit("Days must be 1-365")
    if args.days > 7:
        import logging
        logging.getLogger(__name__).warning("Using %d days (>7) is non-standard.", args.days)
    dry_run = not args.apply
    if not dry_run:
        confirm = input("Modify S3 lifecycle configurations? Type 'yes' to confirm: ")
        if confirm.lower() != 'yes':
            print("Cancelled")
            sys.exit(0)
    if args.profiles_file:
        if not os.path.exists(args.profiles_file):
            raise SystemExit("Profiles file not found: %s" % args.profiles_file)
        manager = run_multi_account(args, dry_run)
    else:
        manager = run_single_account(args, dry_run)
        if not manager._results and not getattr(args, 'resume', None):
            print("No buckets found")
            sys.exit(0)
    manager.print_report(dry_run)
    if args.output_file:
        manager.export_results(args.output_file, args.output_format)
    summary = manager.generate_summary()
    failed = sum(c for st, c in summary['by_status'].items() if st.startswith('failed'))
    sys.exit(1 if failed > 0 else 0)
