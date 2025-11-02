using './main.bicep'

param parLocation = 'uksouth'
param parResourceGroupName = 'rg-shared-dev'
param parFrontDoorProfileName = 'afd-shared-profile-dev'
param parFrontDoorEndpointName = 'afd-endpoint-dev'
param parPrimaryAppStackName = 'az-stack-app-dev'
param parAdditionalAppStackNames = []
