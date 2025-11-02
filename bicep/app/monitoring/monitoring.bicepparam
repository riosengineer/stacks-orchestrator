using './monitoring.bicep'

param parResourceGroupName = 'rg-monitoring'
param parLocation = 'uksouth'
param parLawName = 'loganalytics-monitoring'
param parApplicationInsightsName = 'appInsights-monitoring'
param parImpactedRegions = [
	'West Europe'
	'Global'
]

