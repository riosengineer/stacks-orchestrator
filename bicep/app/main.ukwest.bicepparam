using './main.bicep'

param parLocation = 'ukwest'
param parWebAppName = 'rioswebapp001-ukw'
param parAppServicePlanName = 'appserviceplan-ukw'
param parResourceGroupName = 'rg-app-ukwest'
param parKeyVaultName = 'kvdanstacksapp002ukw'
param parSqlServerName = 'sqlserverstacksapp001ukw'
param parSqlDatabaseName = 'sqldb-ukw'
param parSqlServerAdminLogin = 'sqladminuser'
param parMonitoringStackName = 'az-stack-monitoring-ukwest'
param parNetworkingStackName = 'az-stack-networking-ukwest'
param parDeploySql = false // I am waiting for sub quota via support :(
