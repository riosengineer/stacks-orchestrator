using './monitoring.bicep'

param parResourceGroupName = 'rg-monitoring-dev'
param parLocation = 'uksouth'
param parLawName = 'loganalytics-monitoring-dev'
param parApplicationInsightsName = 'appInsights-monitoring-dev'
param parImpactedRegions = [
  'UK South'
  'Global'
]
