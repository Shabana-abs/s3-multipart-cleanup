# S3 Multipart Upload Cleanup - Terraform Main Logic
# For integration into modules/provider/aws/s3/bucket/main.tf

# Enhanced lifecycle rules including multipart cleanup
locals {
  # Base lifecycle rules (existing logic)
  base_lifecycle_rules = var.lifecycle_rules != null ? var.lifecycle_rules : []
  
  # Multipart cleanup rule
  multipart_cleanup_rule = var.enable_multipart_cleanup ? {
    id     = "Clean up incomplete multipart uploads"
    status = "Enabled"
    filter = {}
    
    abort_incomplete_multipart_upload = {
      days_after_initiation = var.abort_incomplete_multipart_upload_days
    }
  } : null
  
  # Combine rules - add multipart cleanup if enabled and not already present
  lifecycle_rules_with_multipart = var.enable_multipart_cleanup ? concat(
    local.base_lifecycle_rules,
    length([for rule in local.base_lifecycle_rules : rule if can(rule.abort_incomplete_multipart_upload)]) == 0 && local.multipart_cleanup_rule != null ? [local.multipart_cleanup_rule] : []
  ) : local.base_lifecycle_rules
}

# Update the lifecycle configuration resource
resource "aws_s3_bucket_lifecycle_configuration" "this" {
  count  = length(local.lifecycle_rules_with_multipart) > 0 ? 1 : 0
  bucket = aws_s3_bucket.this.bucket

  dynamic "rule" {
    for_each = local.lifecycle_rules_with_multipart
    content {
      id     = rule.value.id
      status = rule.value.status

      # Handle filter block
      dynamic "filter" {
        for_each = can(rule.value.filter) ? [rule.value.filter] : [{}]
        content {
          # Add filter content if present
          dynamic "prefix" {
            for_each = can(filter.value.prefix) ? [filter.value.prefix] : []
            content {
              prefix = prefix.value
            }
          }
          
          dynamic "tag" {
            for_each = can(filter.value.tags) ? filter.value.tags : []
            content {
              key   = tag.value.key
              value = tag.value.value
            }
          }
        }
      }

      # Existing rule content blocks for expiration, transitions, etc.
      dynamic "expiration" {
        for_each = can(rule.value.expiration) ? [rule.value.expiration] : []
        content {
          days                         = can(expiration.value.days) ? expiration.value.days : null
          date                         = can(expiration.value.date) ? expiration.value.date : null
          expired_object_delete_marker = can(expiration.value.expired_object_delete_marker) ? expiration.value.expired_object_delete_marker : null
        }
      }

      dynamic "transition" {
        for_each = can(rule.value.transitions) ? rule.value.transitions : []
        content {
          days          = can(transition.value.days) ? transition.value.days : null
          date          = can(transition.value.date) ? transition.value.date : null
          storage_class = transition.value.storage_class
        }
      }

      dynamic "noncurrent_version_expiration" {
        for_each = can(rule.value.noncurrent_version_expiration) ? [rule.value.noncurrent_version_expiration] : []
        content {
          noncurrent_days = noncurrent_version_expiration.value.noncurrent_days
        }
      }

      dynamic "noncurrent_version_transition" {
        for_each = can(rule.value.noncurrent_version_transitions) ? rule.value.noncurrent_version_transitions : []
        content {
          noncurrent_days = noncurrent_version_transition.value.noncurrent_days
          storage_class   = noncurrent_version_transition.value.storage_class
        }
      }

      # NEW: Abort incomplete multipart upload block
      dynamic "abort_incomplete_multipart_upload" {
        for_each = can(rule.value.abort_incomplete_multipart_upload) ? [rule.value.abort_incomplete_multipart_upload] : []
        content {
          days_after_initiation = abort_incomplete_multipart_upload.value.days_after_initiation
        }
      }
    }
  }
}








