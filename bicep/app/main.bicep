targetScope = 'subscription'
param parLocation string
param parMonitoringStackName string 
param parNetworkingStackName string
param parWebAppName string
param parAppServicePlanName string
param parResourceGroupName string
param parKeyVaultName string
param parSqlServerName string
param parSqlDatabaseName string
@secure()
param parSqlServerAdminLogin string
@secure()
param parSqlServerAdminPassword string = newGuid()
param parDeploySql bool = true

// Monitoring Stack
resource resMonitoringStack 'Microsoft.Resources/deploymentStacks@2024-03-01' existing = {
  scope: subscription()
  name: parMonitoringStackName
}

// Monitoring Stack Dependencies
var resMonitoringStackOutputs = resMonitoringStack.properties.outputs
var resMonitoringAppInsightsConnectionString string = contains(
    resMonitoringStackOutputs,
    'applicationInsightsConnectionString'
  )
  ? resMonitoringStackOutputs.applicationInsightsConnectionString.value
  : ''

// Networking stack
resource resNetworkingStack 'Microsoft.Resources/deploymentStacks@2024-03-01' existing = {
  scope: subscription()
  name: parNetworkingStackName
}
// Networking Stack Dependencies
var resNetworkingStackOutputs = resNetworkingStack.properties.outputs
var resNetworkingSubnetId string = resNetworkingStackOutputs.subnetId.value

module modResourceGroup 'br/public:avm/res/resources/resource-group:0.4.2' = {
  scope: subscription()
  params: {
    name: parResourceGroupName
    location: parLocation
  }
}

module modAppServicePlan 'br/public:avm/res/web/serverfarm:0.5.0' = {
  scope: resourceGroup(parResourceGroupName)
  params: {
    name: parAppServicePlanName
    location: parLocation
    kind: 'linux'
    skuName: 'B1'
  }
  dependsOn: [
    modResourceGroup
  ]
}

module modWebApp 'br/public:avm/res/web/site:0.19.3' = {
  scope: resourceGroup(parResourceGroupName)
  params: {
    name: parWebAppName
    location: parLocation
    kind: 'app'
    serverFarmResourceId: modAppServicePlan.outputs.resourceId
    virtualNetworkSubnetResourceId: resNetworkingSubnetId
    siteConfig: {
      linuxFxVersion: 'NODE|22-lts'
      appSettings: union(
        [],
        !empty(resMonitoringAppInsightsConnectionString)
          ? [
              {
                name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
                value: resMonitoringAppInsightsConnectionString
              }
            ]
          : []
      )
      alwaysOn: false
    }
  }
  dependsOn: [
    modResourceGroup
  ]
}

module modKeyVault 'br/public:avm/res/key-vault/vault:0.13.3' = {
  scope: resourceGroup(parResourceGroupName)
  params: {
    name: parKeyVaultName
    location: parLocation
    enableRbacAuthorization: true
    sku: 'standard'
    enablePurgeProtection: false // I hate this bloody thing most the time
  }
  dependsOn: [
    modResourceGroup
  ]
}

module modAzureSqlDb 'br/public:avm/res/sql/server:0.20.3' = if (parDeploySql) {
  scope: resourceGroup(parResourceGroupName)
  params: {
    name: parSqlServerName
    location: parLocation
    administratorLogin: parSqlServerAdminLogin
    administratorLoginPassword: parSqlServerAdminPassword
    databases: [
      {
        name: parSqlDatabaseName
        availabilityZone: -1
        sku: {
          name: 'GP_S_Gen5_2'
          tier: 'GeneralPurpose'
        }
        minCapacity: '0.5'
        zoneRedundant: false
        useFreeLimit: true
        freeLimitExhaustionBehavior: 'AutoPause'
        autoPauseDelay: 60
      }
    ]
    virtualNetworkRules: [
      {
        name: 'snet-database'
        virtualNetworkSubnetResourceId: resNetworkingSubnetId
        ignoreMissingVnetServiceEndpoint: false
      }
    ]
  }
  dependsOn: [
    modResourceGroup
  ]
}

output outWebAppResourceId string = modWebApp.outputs.resourceId
output outWebAppDefaultHostName string = modWebApp.outputs.defaultHostname
output outAppServicePlanResourceId string = modAppServicePlan.outputs.resourceId
