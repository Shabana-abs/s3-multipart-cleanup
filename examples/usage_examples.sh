#!/bin/bash

# S3 Multipart Upload Cleanup - Usage Examples
# ==================================================

# BASIC USAGE
echo "=== Basic Usage Examples ==="

# Dry run on all buckets (recommended first step)
python scripts/s3_multipart_cleanup_manager.py --dry-run --days 7

# Apply to all buckets with 7-day cleanup
python scripts/s3_multipart_cleanup_manager.py --days 7 --apply

# FILTERED USAGE
echo "=== Filtered Usage Examples ==="

# Filter by bucket pattern (regex)
python scripts/s3_multipart_cleanup_manager.py --bucket-pattern "myapp-.*" --days 7 --apply

# Filter by region
python scripts/s3_multipart_cleanup_manager.py --region us-west-2 --days 7 --apply

# Combine filters
python scripts/s3_multipart_cleanup_manager.py --bucket-pattern "prod-.*" --region us-east-1 --days 7 --apply

# PROGRESSIVE ROLLOUT EXAMPLES
echo "=== Progressive Rollout Strategy ==="

# Stage 1: Test on dev buckets
python scripts/s3_multipart_cleanup_manager.py --bucket-pattern ".*-dev-.*" --days 7 --apply

# Stage 2: Staging environment  
python scripts/s3_multipart_cleanup_manager.py --bucket-pattern ".*-staging-.*" --days 7 --apply

# Stage 3: Production by region
python scripts/s3_multipart_cleanup_manager.py --region us-west-2 --days 7 --apply
python scripts/s3_multipart_cleanup_manager.py --region us-east-1 --days 7 --apply
python scripts/s3_multipart_cleanup_manager.py --region eu-west-1 --days 7 --apply

# Stage 4: Remaining buckets
python scripts/s3_multipart_cleanup_manager.py --days 7 --apply

# CUSTOM CONFIGURATIONS
echo "=== Custom Configuration Examples ==="

# Aggressive cleanup (1 day)
python scripts/s3_multipart_cleanup_manager.py --days 1 --apply

# Conservative cleanup with limited concurrency
python scripts/s3_multipart_cleanup_manager.py --days 7 --max-workers 2 --apply

# Use specific AWS profile
python scripts/s3_multipart_cleanup_manager.py --profile production --days 7 --apply

# VALIDATION EXAMPLES
echo "=== Validation Commands ==="

# Check if specific bucket has lifecycle rule
aws s3api get-bucket-lifecycle-configuration --bucket my-test-bucket

# List incomplete uploads for a bucket
aws s3api list-multipart-uploads --bucket my-test-bucket

# Count buckets with multipart cleanup rules
aws s3api list-buckets --query 'Buckets[*].Name' --output text | \
while read bucket; do
  if aws s3api get-bucket-lifecycle-configuration --bucket "$bucket" 2>/dev/null | grep -q "AbortIncompleteMultipartUpload"; then
    echo "✓ $bucket has multipart cleanup rule"
  else
    echo "✗ $bucket missing multipart cleanup rule"
  fi
done

# MONITORING EXAMPLES
echo "=== Monitoring Commands ==="

# Check script logs
tail -f s3_multipart_cleanup_*.log

# Monitor AWS costs (requires AWS CLI with billing permissions)
aws ce get-cost-and-usage \
  --time-period Start=2024-01-01,End=2024-01-31 \
  --granularity MONTHLY \
  --metrics BlendedCost \
  --group-by Type=DIMENSION,Key=SERVICE

# EMERGENCY PROCEDURES
echo "=== Emergency Procedures ==="

# Stop script execution
# Ctrl+C

# Remove lifecycle rule from specific bucket
# aws s3api delete-bucket-lifecycle --bucket problematic-bucket-name

# Check Terraform state for drift
# terraform plan

echo "=== Example Complete ==="
