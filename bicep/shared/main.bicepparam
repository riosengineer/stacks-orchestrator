using './main.bicep'

param parLocation = 'uksouth'
param parResourceGroupName = 'rg-shared'
param parFrontDoorProfileName = 'afd-shared-profile'
param parFrontDoorEndpointName = 'afd-endpoint'
param parPrimaryAppStackName = 'az-stack-app'
param parAdditionalAppStackNames = [
	'az-stack-app-ukwest'
]
param parEnableDevRoute = false
param parDevAppStackName = 'az-stack-app-dev'
