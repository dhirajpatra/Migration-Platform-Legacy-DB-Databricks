variable "region"                {
  type    = string
  default = "ap-south-1"
}
variable "prefix"                {
  type    = string
  default = "mig"
}
variable "bucket_name"           {
  type = string
}
variable "landing_prefix"        {
  type    = string
  default = "landing"
}
variable "secrets_prefix"        {
  type    = string
  default = "migration"
}
variable "source_ids"            {
  type    = list(string)
  default = ["mssql_demo", "oracle_demo"]
}
variable "aws_account_id"        {
  type = string
}
variable "databricks_account_id" {
  type = string
}
variable "databricks_host"       {
  type    = string
  default = "https://mock.cloud.databricks.com"
}
variable "databricks_token" {
  type      = string
  sensitive = true
  default   = "dapi-mock-0000"
}
variable "catalog_name" {
  type    = string
  default = "migration_dev"
}
