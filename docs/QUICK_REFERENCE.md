# S3 Multipart Cleanup - Quick Reference

## 🚀 Ready-to-Execute Commands

### Testing (Always Start Here)
```bash
# 1. Dry run to see what would be changed
python scripts/s3_multipart_cleanup_manager.py --dry-run --days 7

# 2. Test on a few dev buckets first
python scripts/s3_multipart_cleanup_manager.py --bucket-pattern ".*-dev-.*" --days 7 --apply
```

### Production Rollout
```bash
# 3. Staging environment
python scripts/s3_multipart_cleanup_manager.py --bucket-pattern ".*-staging-.*" --days 7 --apply

# 4. Production by region (recommended)
python scripts/s3_multipart_cleanup_manager.py --region us-west-2 --days 7 --apply
python scripts/s3_multipart_cleanup_manager.py --region us-east-1 --days 7 --apply

# 5. Complete rollout
python scripts/s3_multipart_cleanup_manager.py --days 7 --apply
```

## 🔍 Validation Commands

```bash
# Check if bucket has lifecycle rule
aws s3api get-bucket-lifecycle-configuration --bucket BUCKET_NAME

# Count incomplete uploads
aws s3api list-multipart-uploads --bucket BUCKET_NAME

# Check script logs
tail -f s3_multipart_cleanup_*.log
```

## ⚙️ Configuration Quick Guide

| Parameter | Default | Range | Purpose |
|-----------|---------|-------|---------|
| `--days` | 7 | 1-7 | Days after which to cleanup |
| `--max-workers` | 5 | 1-10 | Concurrent operations |
| `--dry-run` | False | - | Simulate only |
| `--apply` | False | - | Actually execute |

## 🚨 Emergency Stop
```bash
# Stop script execution
Ctrl+C

# Remove lifecycle rule if needed
aws s3api delete-bucket-lifecycle --bucket BUCKET_NAME
```

## ✅ Success Criteria
- [ ] Dry run completes without errors
- [ ] Dev buckets updated successfully  
- [ ] Lifecycle rules visible in AWS console
- [ ] No Terraform drift detected
- [ ] Storage costs begin decreasing








