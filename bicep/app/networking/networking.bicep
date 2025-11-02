targetScope = 'subscription'
param parResourceGroupName string
param parLocation string = 'uksouth'

module modResourceGroup 'br/public:avm/res/resources/resource-group:0.4.2' = {
  scope: subscription()
  params: {
    name: parResourceGroupName
    location: parLocation
  }
}

module modVirtualNetwork 'br/public:avm/res/network/virtual-network:0.7.1' = {
  scope: resourceGroup(parResourceGroupName)
  params: {
    name: 'vnet'
    addressPrefixes: [
      '10.0.0.0/24'
    ]
    subnets: [
      {
        name: 'snet-webapp'
        addressPrefix: '10.0.0.0/24'
        delegation: 'Microsoft.Web/serverFarms'
        serviceEndpoints: [
          'Microsoft.Sql'
        ]
      }
    ]
  }
  dependsOn: [
    modResourceGroup
  ]
}

module modNsg 'br/public:avm/res/network/network-security-group:0.5.2' = {
  scope: resourceGroup(parResourceGroupName)
  params: {
    name: 'nsg'
    location: parLocation
  }
  dependsOn: [
    modResourceGroup
  ]
}

module modPrivateDns 'br/public:avm/res/network/private-dns-zone:0.8.0' = {
  scope: resourceGroup(parResourceGroupName)
  params: {
    name: 'privatelink.azurewebsites.net'
    location: 'global'
  }
  dependsOn: [
    modResourceGroup
  ]
}

output virtualNetworkId string = modVirtualNetwork.outputs.resourceId
output subnetId string = modVirtualNetwork.outputs.subnetResourceIds[0]
output networkSecurityGroupId string = modNsg.outputs.resourceId
output privateDnsZoneId string = modPrivateDns.outputs.resourceId
