# S3 Multipart Upload Cleanup - Terraform Variables
# For integration into modules/provider/aws/s3/bucket/variables.tf

variable "abort_incomplete_multipart_upload_days" {
  description = "Number of days after which incomplete multipart uploads are aborted (1-365). Default is 7 days."
  type        = number
  default     = 7

  validation {
    condition     = var.abort_incomplete_multipart_upload_days >= 1 && var.abort_incomplete_multipart_upload_days <= 365
    error_message = "abort_incomplete_multipart_upload_days must be between 1 and 365 days."
  }
}

variable "enable_multipart_cleanup" {
  description = "Enable automatic cleanup of incomplete multipart uploads"
  type        = bool
  default     = true
}
