# STARTER module (not applied/validated by the generator — run `terraform init && terraform validate` first).
# Creates: S3 landing bucket, DMS roles, Secrets Manager shells for source credentials,
#          and the Unity Catalog storage credential + external location + catalog.
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 5.0" }
    databricks = { source = "databricks/databricks", version = "~> 1.50" }
  }
}

provider "aws" { region = var.region }
provider "databricks" {
  host  = var.databricks_host
  token = var.databricks_token   # mock value works for `plan` of AWS-only resources; real token needed for databricks_* resources
}

locals {
  uc_role_name = "${var.prefix}-uc-landing"
}

# ---------------------------------------------------------------- landing zone
resource "aws_s3_bucket" "landing" { bucket = var.bucket_name }
resource "aws_s3_bucket_public_access_block" "landing" {
  bucket                  = aws_s3_bucket.landing.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "landing" {
  bucket = aws_s3_bucket.landing.id
  rule { apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" } }
}

# ---------------------------------------------------------------- source credentials (values set out-of-band)
resource "aws_secretsmanager_secret" "source" {
  for_each = toset(var.source_ids)
  name     = "${var.secrets_prefix}/${each.key}"
}

# ---------------------------------------------------------------- DMS roles
data "aws_iam_policy_document" "dms_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["dms.amazonaws.com", "dms.${var.region}.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "dms_s3" {
  name               = "${var.prefix}-dms-s3"
  assume_role_policy = data.aws_iam_policy_document.dms_trust.json
}
resource "aws_iam_role_policy" "dms_s3" {
  role = aws_iam_role.dms_s3.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:PutObject", "s3:DeleteObject", "s3:PutObjectTagging"], Resource = "${aws_s3_bucket.landing.arn}/${var.landing_prefix}/*" },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.landing.arn }]})
}
resource "aws_iam_role" "dms_secrets" {
  name               = "${var.prefix}-dms-secrets"
  assume_role_policy = data.aws_iam_policy_document.dms_trust.json
}
resource "aws_iam_role_policy" "dms_secrets" {
  role = aws_iam_role.dms_secrets.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [for s in aws_secretsmanager_secret.source : s.arn] }]})
}

# ---------------------------------------------------------------- Unity Catalog access to the landing zone
data "databricks_aws_unity_catalog_assume_role_policy" "this" {
  aws_account_id = var.aws_account_id
  role_name      = local.uc_role_name
  external_id    = var.databricks_account_id
}
data "databricks_aws_unity_catalog_policy" "this" {
  aws_account_id = var.aws_account_id
  bucket_name    = aws_s3_bucket.landing.bucket
  role_name      = local.uc_role_name
}
resource "aws_iam_policy" "uc" { policy = data.databricks_aws_unity_catalog_policy.this.json }
resource "aws_iam_role" "uc" {
  name               = local.uc_role_name
  assume_role_policy = data.databricks_aws_unity_catalog_assume_role_policy.this.json
}
resource "aws_iam_role_policy_attachment" "uc" {
  role       = aws_iam_role.uc.name
  policy_arn = aws_iam_policy.uc.arn
}
resource "databricks_storage_credential" "landing" {
  name = "${var.prefix}-landing"
  aws_iam_role { role_arn = aws_iam_role.uc.arn }
  depends_on = [aws_iam_role_policy_attachment.uc]
}
resource "databricks_external_location" "landing" {
  name            = "${var.prefix}-landing"
  url             = "s3://${aws_s3_bucket.landing.bucket}/${var.landing_prefix}"
  credential_name = databricks_storage_credential.landing.id
}
resource "databricks_catalog" "migration" {
  name    = var.catalog_name
  comment = "Migration target (bronze/silver/gold)"
}
