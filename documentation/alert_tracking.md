# Alert Tracking

> This documentation applies to Weather Alerts version 2026.1.0 and newer.
>  
> Behavior and configuration may differ in earlier versions.

The integration maintains a persistent `alert_tracking` list to track the lifecycle of alerts.

## Status Values

- new: Alert first observed
- old: Alert still active
- delete: Alert removed from the API feed

## Persistence

Alert tracking data is preserved across Home Assistant restarts.

The first successful update reconciles restored IDs before the sensor publishes them. When Home moves or the query mode changes, an ID present in both feeds stays `old`. An ID seen only at the new location is `new`. An ID left behind does not generate a `delete` notification. Within the same feed the usual deletion window applies.

Home Assistant keeps the last successfully queried point in private entity restore data to distinguish a restart from a move. The point and NWS geometry are omitted from sensor attributes.

## Deletion Behavior

Alerts marked as delete remain in the tracking list for 10 minutes before being removed.

---

## Documentation Navigation

- [Overview](https://github.com/custom-components/weatheralerts/blob/master/documentation/overview.md)
- [Installation](https://github.com/custom-components/weatheralerts/blob/master/documentation/installation.md)
- [Configuration](https://github.com/custom-components/weatheralerts/blob/master/documentation/configuration.md)
- [Sensor Behavior](https://github.com/custom-components/weatheralerts/blob/master/documentation/sensor.md)
- [Alert Tracking](https://github.com/custom-components/weatheralerts/blob/master/documentation/alert_tracking.md) **<-- You are here**
- [Alert Deduplication](https://github.com/custom-components/weatheralerts/blob/master/documentation/deduplication.md)
- [Alert Icon Configuration](https://github.com/custom-components/weatheralerts/blob/master/documentation/icons.md)
- [Error Handling](https://github.com/custom-components/weatheralerts/blob/master/documentation/error_handling.md)
- [Automation Examples](https://github.com/custom-components/weatheralerts/blob/master/documentation/examples_automations.md)
- [Dashboard Examples](https://github.com/custom-components/weatheralerts/blob/master/documentation/examples_dashboard.md)
- [Alert Card](https://github.com/custom-components/weatheralerts/blob/master/documentation/alert_card.md)
- [Troubleshooting](https://github.com/custom-components/weatheralerts/blob/master/documentation/troubleshooting.md)
- [Migration from YAML](https://github.com/custom-components/weatheralerts/blob/master/documentation/migration.md)
- [Documentation Versioning Policy](https://github.com/custom-components/weatheralerts/blob/master/documentation/versioning.md)

## Support and Issues

- [Support Forum](https://github.com/custom-components/weatheralerts/discussions)
- [GitHub Repository Home](https://github.com/custom-components/weatheralerts)
- [View Issues/Feature Requests](https://github.com/custom-components/weatheralerts/issues)
- [Report an Issue/Feature Request](https://github.com/custom-components/weatheralerts/issues/new/choose)
