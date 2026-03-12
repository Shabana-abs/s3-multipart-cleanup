"""
Constants for S3 multipart cleanup: rule ID and AWS-managed bucket patterns.
"""
# Lifecycle rule ID we create/update (so we only touch our own rule)
RULE_ID = "auto-multipart-cleanup-managed"

# Bucket name patterns for AWS-managed/service buckets we skip by default
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
