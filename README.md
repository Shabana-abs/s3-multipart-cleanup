# S3 Multipart Upload Cleanup Solution

[![AWS](https://img.shields.io/badge/AWS-S3-orange)](https://aws.amazon.com/s3/)
[![Terraform](https://img.shields.io/badge/Terraform-Infrastructure-blue)](https://terraform.io/)
[![Python](https://img.shields.io/badge/Python-3.8+-green)](https://python.org/)
[![Version](https://img.shields.io/badge/Version-2.1-blue)](scripts/s3_multipart_cleanup/README.md)

## 🎯 Overview

Production-ready solution to automatically clean up incomplete S3 multipart uploads across thousands of buckets, preventing storage cost accumulation and operational overhead.

### Problem Solved
- **Incomplete multipart uploads** in S3 buckets accumulate without expiration by default
- **~5300+ buckets** affected in large-scale deployments
- **Storage costs** continuously increase from abandoned uploads
- **Operational complexity** in managing bucket lifecycles

### Solution Benefits
- ✅ **Automatic cleanup** after configurable days (1–365; default 7)
- ✅ **Terraform integration** for new buckets
- ✅ **Python script** for existing bucket remediation
- ✅ **Zero Terraform drift** - script works alongside IaC
- ✅ **Production-safe** with rate limiting and progressive rollout

### v2.1 Enhancements
- ✅ **Exception lists** - exclude specific buckets/patterns
- ✅ **Exponential backoff** - handles API throttling gracefully  
- ✅ **Checkpointing** - resume from failures
- ✅ **Structured output** - JSON/CSV export for observability
- ✅ **Preflight validation** - checks permissions, Object Lock, lifecycle limits
- ✅ **Preserves scoped rules** - doesn't replace prefix-specific rules
- ✅ **Multi-account support** - run across all AWS accounts via `--profiles-file`
- ✅ **list_buckets** only returns current account; no redundant cross-account check (per AWS API)

## 🚀 Quick Start

### 1. For New Buckets (Terraform)
```hcl
# Add to your S3 bucket module
abort_incomplete_multipart_upload_days = 7
enable_multipart_cleanup = true
```

### 2. For Existing Buckets (Python Script v2.1)
```bash
# Dry run first (with exception list)
python scripts/s3_multipart_cleanup_manager_v2.py --dry-run --exceptions-file config/exceptions.txt

# Apply to production with structured output
python scripts/s3_multipart_cleanup_manager_v2.py --days 7 --output-file results.json --apply

# Resume from checkpoint after failure
python scripts/s3_multipart_cleanup_manager_v2.py --resume checkpoint.json --apply

# Run across all AWS accounts (one profile per line in config/profiles.txt)
python scripts/s3_multipart_cleanup_manager_v2.py --dry-run --profiles-file config/profiles.txt
python scripts/s3_multipart_cleanup_manager_v2.py --profiles-file config/profiles.txt --output-file results.json --apply
```

## 📁 Repository Structure

```
s3-multipart-cleanup/
├── README.md                              # This file
├── scripts/
│   ├── s3_multipart_cleanup/               # v2.1 package (manager, CLI, models)
│   └── s3_multipart_cleanup_manager_v2.py # v2.1 entrypoint
├── terraform/
│   ├── variables.tf                       # Terraform variable definitions
│   └── main.tf                            # Lifecycle configuration logic
├── config/
│   ├── exceptions.txt.example             # Exception list template
│   └── profiles.txt.example               # Multi-account profile list (--profiles-file)
├── docs/
│   └── TEST_EVIDENCE.md                   # Test run evidence (sanitized)
├── tests/                                 # pytest unit tests
├── examples/
│   └── usage_examples.sh                  # Example commands
└── requirements.txt                       # Python dependencies
```

## 🔧 Implementation Approach

### Two-Part Solution

#### Part 1: Infrastructure (Terraform)
- **Purpose**: Ensure all new S3 buckets get multipart cleanup rules automatically
- **Location**: Integrate into existing S3 bucket modules
- **Impact**: Future-proof against the issue

#### Part 2: Remediation (Python)
- **Purpose**: Add lifecycle rules to existing ~5300 buckets
- **Method**: Boto3 script with rate limiting and safety features
- **Impact**: Fixes current accumulation immediately

## 📊 Expected Results

| Timeframe | Impact |
|-----------|--------|
| **Immediate** | Cleanup of uploads older than specified days |
| **1–365 days** | Complete cleanup of incomplete uploads once they exceed the configured threshold |
| **Ongoing** | Automatic prevention of future accumulation |

## 🛡️ Safety Features

- **Dry-run mode** for testing before execution
- **Rate limiting** with exponential backoff to prevent AWS API throttling
- **Progressive rollout** by environment/region
- **Comprehensive logging** for audit trails
- **Error handling** with detailed reporting and structured output
- **Terraform drift prevention** - works alongside IaC
- **Exception lists** - exclude buckets that need special handling
- **Preflight checks** - validates permissions, Object Lock, lifecycle limits
- **Checkpointing** - resume from failures without re-processing
- **Multi-account runs** - use `--profiles-file` to iterate profiles (each account’s buckets are listed separately)
- **Preserves scoped rules** - doesn't overwrite prefix-specific lifecycle rules

## 📋 Prerequisites

- AWS CLI configured with appropriate permissions
- Python 3.8+ with boto3
- Terraform (for infrastructure changes)
- IAM permissions for S3 lifecycle management

## 🚨 Production Rollout Strategy

1. **Test Environment**: Validate on dev buckets first
2. **Staging**: Roll out to staging environment  
3. **Production**: Deploy region by region
4. **Monitor**: Track cleanup progress and storage cost reduction

## 📖 Documentation

- **[Package overview](scripts/s3_multipart_cleanup/README.md)** - v2.1 module layout
- **[Test evidence](docs/TEST_EVIDENCE.md)** - Validated behavior (sanitized)
- **[Usage Examples](examples/usage_examples.sh)** - Common usage patterns
- **[Exception List Template](config/exceptions.txt.example)** - Bucket exclusion patterns

## 🤝 Contributing

This solution is designed for enterprise deployment. Please test thoroughly in non-production environments before applying to production buckets.

## 📞 Support

**Primary Contact**: Shabana Sulthana  
**Team**: Cloud Infrastructure / SecOps  
**Ticket**: [CLOUD-3981](https://abnormalsecurity.atlassian.net/browse/CLOUD-3981)

## 🏷️ Version

**Version**: 2.1 (Enhanced Production Ready)  
**Last Updated**: December 2025  
**Tested On**: AWS accounts with 5300+ S3 buckets

### Changelog
- **v2.1** (Dec 2025): Review fixes - extended days support (1-365), AWS-managed bucket detection, unified resume reporting, optimized API calls
- **v2.0** (Dec 2025): Exception lists, exponential backoff, checkpointing, structured output, preflight checks, preserves scoped rules
- **v1.0** (Sep 2025): Initial production release

---

*This solution addresses the specific requirement to add `abort_incomplete_multipart_upload_days` to S3 buckets while maintaining Terraform state consistency.*




