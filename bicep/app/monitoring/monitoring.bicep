targetScope = 'subscription'
param parResourceGroupName string
param parLocation string
param parLawName string
param parApplicationInsightsName string
param parImpactedRegions array = [
  'West Europe'
  'Global'
]

module modResourceGroup 'br/public:avm/res/resources/resource-group:0.4.2' = {
  scope: subscription()
  params: {
    name: parResourceGroupName
    location: parLocation
  }
}

module modLaw 'br/public:avm/res/operational-insights/workspace:0.12.0' = {
  scope: resourceGroup(parResourceGroupName)
  params: {
    name: parLawName
    location: parLocation
  }
  dependsOn: [
    modResourceGroup
  ]
}

module appInsights 'br/public:avm/res/insights/component:0.6.1' = {
  scope: resourceGroup(parResourceGroupName)
  params: {
    workspaceResourceId: modLaw.outputs.resourceId
    location: parLocation
    applicationType: 'web'
    name: parApplicationInsightsName
  }
  dependsOn: [
    modResourceGroup
  ]
}

module modActionGroup 'br/public:avm/res/insights/action-group:0.8.0' = {
  scope: resourceGroup(parResourceGroupName)
  params: {
    name: 'action-group'
    groupShortName: 'ag'
    enabled: true
    emailReceivers: [
      {
        name: 'admin'
        emailAddress: 'admin@example.com'
      }
    ]
  }
  dependsOn: [
    modResourceGroup
  ]
}

module modSvcAlert 'br/public:avm/res/insights/activity-log-alert:0.4.1' = {
  scope: resourceGroup(parResourceGroupName)
  params: {
    name: 'service-health-alert'
    location: 'global'
    conditions: [
        {
          field: 'category'
          equals: 'ServiceHealth'
        }
        {
          anyOf: [
            {
              field: 'properties.incidentType'
              equals: 'Incident'
            }
            {
              field: 'properties.incidentType'
              equals: 'Maintenance'
            }
          ]
        }
        {
          field: 'properties.impactedServices[*].ServiceName'
          containsAny: [
            'Action Groups'
            'Activity Logs & Alerts'
          ]
        }
        {
          field: 'properties.impactedServices[*].ImpactedRegions[*].RegionName'
          containsAny: [
            'West Europe'
            'Global'
          ]
        }
      ]
    scopes: [
      subscription().id
    ]
  }
  dependsOn: [
    modResourceGroup
  ]
}

output resourceGroupName string = modResourceGroup.outputs.name
output lawResourceId string = modLaw.outputs.resourceId
output lawName string = modLaw.outputs.name
output applicationInsightsResourceId string = appInsights.outputs.resourceId
output applicationInsightsName string = appInsights.outputs.name
output applicationInsightsConnectionString string = appInsights.outputs.connectionString
