# S3 Multipart Cleanup - Execution Strategy

## Overview

**Total Buckets:** ~5,304  
**Tool:** `s3_multipart_cleanup_manager_v2.py`  
**Ticket:** [CLOUD-3981](https://abnormalsecurity.atlassian.net/browse/CLOUD-3981)

---

## 1. Phased Rollout Plan

### Phase 0: Pre-Flight (Day 1)
```bash
# Generate full audit without making changes
python scripts/s3_multipart_cleanup_manager_v2.py \
  --dry-run \
  --output-file audit_full_$(date +%Y%m%d).json

# Review output for:
# - Total bucket count
# - Buckets that would be skipped (Object Lock, cross-account, etc.)
# - Any unexpected failures
```

**Expected Output:** JSON with breakdown of all ~5,304 buckets by status

### Phase 1: Test Environment (Day 1-2)
**Scope:** `absec-test` account only (~50-100 buckets)

```bash
# Target only test account buckets
python scripts/s3_multipart_cleanup_manager_v2.py \
  --bucket-glob "absec-test-*" \
  --days 7 \
  --output-file results_phase1_test.json \
  --apply
```

**Validation:**
- [ ] Verify lifecycle rules added correctly
- [ ] Check no unintended side effects
- [ ] Confirm checkpointing works

### Phase 2: Non-Production (Day 3-4)
**Scope:** Dev/Staging accounts (~500 buckets per batch)

```bash
# Batch 1: Dev buckets
python scripts/s3_multipart_cleanup_manager_v2.py \
  --bucket-glob "*-dev-*" \
  --days 7 \
  --output-file results_phase2_batch1.json \
  --apply

# Batch 2: Staging buckets  
python scripts/s3_multipart_cleanup_manager_v2.py \
  --bucket-glob "*-staging-*" \
  --days 7 \
  --output-file results_phase2_batch2.json \
  --apply
```

### Phase 3: Production - Batch Rollout (Day 5-10)
**Scope:** Production buckets in controlled batches of ~500

```bash
# Use region-based batching for controlled rollout
REGIONS=("us-east-1" "us-west-2" "eu-west-1" "ap-northeast-1")

for region in "${REGIONS[@]}"; do
  echo "Processing region: $region"
  python scripts/s3_multipart_cleanup_manager_v2.py \
    --region "$region" \
    --days 7 \
    --output-file "results_prod_${region}_$(date +%Y%m%d).json" \
    --apply
  
  # Wait and verify before next region
  echo "Verify results before continuing..."
  sleep 300  # 5 min pause for verification
done
```

### Phase 4: Remaining Buckets (Day 11-12)
```bash
# Process any remaining buckets
python scripts/s3_multipart_cleanup_manager_v2.py \
  --days 7 \
  --output-file results_final.json \
  --apply
```

---

## 2. Batch Size Recommendations

| Environment | Batch Size | Pause Between Batches |
|-------------|------------|----------------------|
| Test | All (~100) | N/A |
| Dev/Staging | 500 | 10 minutes |
| Production | 500 | 30 minutes |

**Rationale:**
- 500 buckets ≈ 10-15 minutes execution time
- Allows for monitoring and quick rollback if needed
- Respects AWS API rate limits with built-in exponential backoff

---

## 3. Rollback Strategy

### Immediate Rollback (< 1 hour after change)

The script adds a lifecycle rule with a **unique managed ID**: `auto-multipart-cleanup-managed`

To remove the rule from a specific bucket:
```bash
# Get current lifecycle config
aws s3api get-bucket-lifecycle-configuration --bucket BUCKET_NAME > lifecycle.json

# Remove the managed rule (keep others)
jq '.Rules = [.Rules[] | select(.ID != "auto-multipart-cleanup-managed")]' lifecycle.json > lifecycle_fixed.json

# Apply fixed config
aws s3api put-bucket-lifecycle-configuration --bucket BUCKET_NAME --lifecycle-configuration file://lifecycle_fixed.json
```

### Bulk Rollback Script
```python
#!/usr/bin/env python3
"""Rollback script to remove auto-multipart-cleanup-managed rules"""
import boto3
import json

MANAGED_RULE_ID = "auto-multipart-cleanup-managed"

def rollback_bucket(bucket_name):
    s3 = boto3.client('s3')
    try:
        config = s3.get_bucket_lifecycle_configuration(Bucket=bucket_name)
        original_rules = config.get('Rules', [])
        
        # Filter out our managed rule
        filtered_rules = [r for r in original_rules if r.get('ID') != MANAGED_RULE_ID]
        
        if len(filtered_rules) < len(original_rules):
            if filtered_rules:
                s3.put_bucket_lifecycle_configuration(
                    Bucket=bucket_name,
                    LifecycleConfiguration={'Rules': filtered_rules}
                )
            else:
                s3.delete_bucket_lifecycle(Bucket=bucket_name)
            print(f"✓ Rolled back: {bucket_name}")
        else:
            print(f"- No managed rule found: {bucket_name}")
    except Exception as e:
        print(f"✗ Error rolling back {bucket_name}: {e}")

# Usage: rollback_bucket("my-bucket-name")
```

### Rollback Decision Matrix

| Issue Detected | Action | Urgency |
|---------------|--------|---------|
| Wrong DaysAfterInitiation value | Update rule with correct value | Low |
| Rule added to wrong bucket | Remove rule from specific bucket | Medium |
| Widespread failures | Stop execution, investigate | High |
| Breaking active uploads | Rollback affected buckets immediately | Critical |

---

## 4. Timeline

| Day | Phase | Buckets | Cumulative |
|-----|-------|---------|------------|
| 1 | Pre-flight audit | 0 (dry-run) | 0 |
| 1-2 | Test environment | ~100 | ~100 |
| 3-4 | Dev/Staging | ~800 | ~900 |
| 5-6 | Prod: us-east-1 | ~1,500 | ~2,400 |
| 7-8 | Prod: us-west-2 | ~1,200 | ~3,600 |
| 9-10 | Prod: Other regions | ~1,200 | ~4,800 |
| 11-12 | Cleanup & remaining | ~500 | ~5,300 |

**Total Estimated Duration:** 10-12 business days

---

## 5. Monitoring & Verification

### During Execution
- Monitor script output in real-time
- Check checkpoint files for progress
- Watch for `FAILED_*` statuses

### Post-Execution Verification
```bash
# Verify a sample of buckets
for bucket in $(cat results.json | jq -r '.results[] | select(.status == "success") | .bucket' | head -10); do
  echo "Checking: $bucket"
  aws s3api get-bucket-lifecycle-configuration --bucket "$bucket" | jq '.Rules[] | select(.ID == "auto-multipart-cleanup-managed")'
done
```

### Success Criteria
- [ ] All target buckets have the lifecycle rule
- [ ] No active uploads were disrupted
- [ ] Exception list buckets were skipped
- [ ] Structured output captured all results
- [ ] No unexpected errors in logs

---

## 6. Emergency Contacts

| Role | Contact | When to Escalate |
|------|---------|------------------|
| Script Owner | Shabana Sulthana | Any script issues |
| Cloud Team | Chris Huegle | Architecture/approval questions |
| SRE On-Call | PagerDuty | Production incidents |

---

## 7. Approval Checklist

Before proceeding to each phase:

- [ ] Previous phase completed successfully
- [ ] Results reviewed and validated
- [ ] No open incidents related to S3
- [ ] Stakeholders notified (if production)
- [ ] Rollback plan understood by on-call
