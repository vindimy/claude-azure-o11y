terraform {
  required_version = ">= 1.9"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    # Log Analytics custom tables have no azurerm resource; azapi manages them.
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
  }
}
