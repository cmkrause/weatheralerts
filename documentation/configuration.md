# Configuration

> This documentation applies to Weather Alerts version 2026.1.0 and newer.
>  
> Behavior and configuration may differ in earlier versions.

YAML configuration is deprecated and no longer supported for new setups.

All configuration is performed through the Home Assistant UI.

## Configuration Flow

Choose **Settings → Devices & services → Integrations → Weather Alerts**. Select the gear beside the entry you want to change. On a new entry, setup opens the same coverage choice first.

### Choose alert coverage

| Coverage | How it works |
| --- | --- |
| **All alerts for configured areas** | Monitor fixed NWS codes. On new entries, the optional location lookup sends the coordinates you submit to NWS to suggest codes. Clear both coordinate fields to enter codes manually. |
| **Follow Home — NWS matches** | Send the current coordinates of `zone.home` to NWS on each poll. This is the setting used by the earlier **Only show alerts for the Home location** checkbox. |
| **Follow Home — match privately on this device** | Discover Home’s county or marine feed and test alert boundaries on the Home Assistant server. NWS receives requests for public areas. The sensor exposes the discovered feed in `monitored_codes`. |

Both Follow Home options skip the optional setup lookup. Moving Home takes effect on the next poll. An existing entry keeps its sensor and its configured codes when you change how it follows Home. [Private coverage and alert maps](local_geometry.md)

### Configured areas

Enter a forecast zone such as `WIZ038`. You may also include a county such as `WIC087` and a comma-separated list of marine zones such as `LMZ043,LMZ080`.

### Sensor update settings

- Update Interval: 30–600 seconds
- API Timeout: 10–60 seconds
  Must be at least 5 seconds less than the update interval
- Remove duplicate alerts with identical NWS alert IDs (optional, recommended)
- Remove duplicate alerts with identical descriptions (optional, not recommended)

Both deduplication options are disabled by default. NWS alert ID deduplication only removes repeated alerts that share the same usable NWS alert ID; alerts without a usable ID are preserved. See [Alert Deduplication](https://github.com/custom-components/weatheralerts/blob/master/documentation/deduplication.md) for details.

Changes take effect immediately after submitting.

After the initial setup of a Weather Alerts config entry is done, you can click the gear icon of your new config entry to bring up the options flow. The options flow configuration will allow you to reconfigure the existing config entry settings (the settings from the config flow above) and will also allow you to add or change any alert event icons. See [Alert Icon Configuration](https://github.com/custom-components/weatheralerts/blob/master/documentation/icons.md) for details.

---

## Documentation Navigation

- [Overview](https://github.com/custom-components/weatheralerts/blob/master/documentation/overview.md)
- [Installation](https://github.com/custom-components/weatheralerts/blob/master/documentation/installation.md)
- [Configuration](https://github.com/custom-components/weatheralerts/blob/master/documentation/configuration.md) **<-- You are here**
- [Sensor Behavior](https://github.com/custom-components/weatheralerts/blob/master/documentation/sensor.md)
- [Alert Tracking](https://github.com/custom-components/weatheralerts/blob/master/documentation/alert_tracking.md)
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
