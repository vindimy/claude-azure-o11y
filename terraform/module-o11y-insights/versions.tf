terraform {
  required_version = ">= 1.9"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    # Logic App workflow definitions and their trigger callback URLs have no complete azurerm resource.
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
  }
}
