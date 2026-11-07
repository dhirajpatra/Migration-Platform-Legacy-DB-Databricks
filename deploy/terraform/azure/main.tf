# STARTER module (not applied/validated by the generator — run `terraform init && terraform validate` first).
# Creates: resource group, ADLS Gen2 landing zone, Key Vault, Data Factory (managed identity),
#          Databricks Access Connector + Unity Catalog storage credential, external location and catalog.
terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm    = { source = "hashicorp/azurerm", version = "~> 3.100" }
    databricks = { source = "databricks/databricks", version = "~> 1.50" }
  }
}

provider "azurerm" { features {} }
provider "databricks" {
  host  = var.databricks_host
  token = var.databricks_token   # mock value is fine for azurerm-only plans; real token needed for databricks_* resources
}

data "azurerm_client_config" "current" {}

resource "azurerm_resource_group" "rg" {
  name     = "${var.prefix}-rg"
  location = var.location
}

# ---------------------------------------------------------------- landing zone (ADLS Gen2)
resource "azurerm_storage_account" "landing" {
  name                     = var.storage_account_name
  resource_group_name      = azurerm_resource_group.rg.name
  location                 = azurerm_resource_group.rg.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  is_hns_enabled           = true
  min_tls_version          = "TLS1_2"
}
resource "azurerm_storage_container" "landing" {
  name                  = var.container_name
  storage_account_name  = azurerm_storage_account.landing.name
  container_access_type = "private"
}

# ---------------------------------------------------------------- secrets
resource "azurerm_key_vault" "kv" {
  name                      = var.key_vault_name
  resource_group_name       = azurerm_resource_group.rg.name
  location                  = azurerm_resource_group.rg.location
  tenant_id                 = data.azurerm_client_config.current.tenant_id
  sku_name                  = "standard"
  enable_rbac_authorization = true
}
# Secret VALUES (e.g. "<source_id>-connection-string") are set out-of-band; never in Terraform state/code.

# ---------------------------------------------------------------- Data Factory (bulk + CDC orchestration)
resource "azurerm_data_factory" "adf" {
  name                = var.data_factory_name
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  identity { type = "SystemAssigned" }
}
resource "azurerm_role_assignment" "adf_blob" {
  scope                = azurerm_storage_account.landing.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_data_factory.adf.identity[0].principal_id
}
resource "azurerm_role_assignment" "adf_kv" {
  scope                = azurerm_key_vault.kv.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_data_factory.adf.identity[0].principal_id
}

# ---------------------------------------------------------------- Unity Catalog access to the landing zone
resource "azurerm_databricks_access_connector" "uc" {
  name                = "${var.prefix}-uc-connector"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  identity { type = "SystemAssigned" }
}
resource "azurerm_role_assignment" "uc_blob" {
  scope                = azurerm_storage_account.landing.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_databricks_access_connector.uc.identity[0].principal_id
}
resource "databricks_storage_credential" "landing" {
  name = "${var.prefix}-landing"
  azure_managed_identity { access_connector_id = azurerm_databricks_access_connector.uc.id }
  depends_on = [azurerm_role_assignment.uc_blob]
}
resource "databricks_external_location" "landing" {
  name            = "${var.prefix}-landing"
  url             = "abfss://${azurerm_storage_container.landing.name}@${azurerm_storage_account.landing.name}.dfs.core.windows.net/${var.landing_prefix}"
  credential_name = databricks_storage_credential.landing.id
}
resource "databricks_catalog" "migration" {
  name    = var.catalog_name
  comment = "Migration target (bronze/silver/gold)"
}
