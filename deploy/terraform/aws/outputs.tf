output "landing_url"        { value = "s3://${aws_s3_bucket.landing.bucket}/${var.landing_prefix}" }
output "dms_s3_role_arn"    { value = aws_iam_role.dms_s3.arn }
output "dms_secrets_role_arn" { value = aws_iam_role.dms_secrets.arn }
output "uc_role_arn"        { value = aws_iam_role.uc.arn }
