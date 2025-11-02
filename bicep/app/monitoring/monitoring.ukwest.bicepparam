using './monitoring.bicep'

param parResourceGroupName = 'rg-monitoring-ukwest'
param parLocation = 'ukwest'
param parLawName = 'loganalytics-monitoring-ukw'
param parApplicationInsightsName = 'appInsights-monitoring-ukw'
param parImpactedRegions = [
  'UK West'
  'Global'
]
