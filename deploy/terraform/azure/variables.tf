variable "prefix" {
  type    = string
  default = "mig"
}
variable "location" {
  type    = string
  default = "centralindia"
}
variable "storage_account_name" {
  type = string
}
variable "container_name" {
  type    = string
  default = "migration-landing"
}
variable "landing_prefix" {
  type    = string
  default = "landing"
}
variable "key_vault_name" {
  type = string
}
variable "data_factory_name" {
  type = string
}
variable "databricks_host" {
  type    = string
  default = "https://adb-0000000000000000.0.azuredatabricks.net"
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
