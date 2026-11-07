output "landing_url" {
  value = "abfss://${azurerm_storage_container.landing.name}@${azurerm_storage_account.landing.name}.dfs.core.windows.net/${var.landing_prefix}"
}
output "key_vault_url" {
  value = azurerm_key_vault.kv.vault_uri
}
output "data_factory" {
  value = azurerm_data_factory.adf.name
}
