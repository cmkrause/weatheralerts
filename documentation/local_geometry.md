# Private coverage and alert maps

Choose **Follow Home — match privately on this device** to follow the center of `zone.home`. Both setup and polling keep the coordinates on your Home Assistant server. NWS receives your connection's IP address, the public area codes requested, and requests for individual zone boundaries. This reveals the general region.

## How an alert reaches the sensor

1. A small local index of bounding rectangles narrows the possible counties and marine zones. The integration fetches their detailed boundaries by code and checks which ones cover Home. A point on a shared boundary can have multiple feeds. If at least one feed is confirmed, a neighboring candidate with an unavailable boundary is also included as a precaution. Moving Home triggers fresh discovery on the next poll.
2. The integration requests active alerts for those codes. For an alert with its own polygon, it tests Home against that polygon. For an alert issued by zone, it tests the boundary of each zone named by that particular advisory. A second rectangle index puts likely forecast and fire zones first. One confirmed match is sufficient.
3. A point on a polygon edge counts as covered. Holes and separate islands retain their geographic meaning. If an alert's coverage is uncertain, the sensor keeps it and counts it in `unresolved_alerts`. If Home moves and the new feed cannot be established, the sensor reports unavailable. The `monitored_codes` attribute identifies the feed used on a successful private poll.

Discovery covers indexed NWS counties, coastal and Great Lakes waters, and offshore marine zones. The index excludes high-seas areas. The precision and coverage of the results depend on NWS feed assignments and boundary data. An invalid polygon or an unsplit polygon crossing the date line is treated as uncertain.

## Maps, storage and history

Choose **View affected area** on an active alert in the WeatherAlerts Alert Card. The map shows the entire issued area, including portions outside the monitored county. It uses the issued polygon when present. For a zone-based advisory, Shapely dissolves the full set of available zone polygons so adjacent zones share one shaded outline. Separate pieces and holes remain visible. If a boundary is unavailable or the lookup reaches its time or vertex budget, the card gives the number of zones shown or reports that the area is unavailable. Coastlines and place labels are bundled with the card. Viewing the map makes no requests to an external map provider.

Detailed zone shapes are fetched on demand, cached locally for 24 hours, and reused across entries. The cache in `.storage/weatheralerts_boundaries/` is capped at 16 MiB, evicting the oldest files first. Up to four complete alert maps per entry can be reused in memory for an hour while their source alerts are current. Map geometry is delivered through an authenticated Home Assistant WebSocket request. Sensor state and attributes contain no coordinate arrays. Opening, panning, or zooming a map does not add history rows. No Recorder configuration is required.

## Updating the shipped data

The discovery and priority indexes use April 16, 2026 NWS GIS boundaries. Download the county archive (`c_16ap26.zip`), coastal and offshore marine archives (`mz16ap26.zip`, `oz16ap26.zip`), forecast zones (`z_16ap26.zip`), and fire zones (`fz16ap26.zip`). The build script verifies the archive checksums. When adopting a new NWS release, update the file names and checksums together and regenerate the indexes. [NWS counties](https://www.weather.gov/gis/Counties) · [Marine zones](https://www.weather.gov/gis/MarineZones) · [Public forecast zones](https://www.weather.gov/gis/PublicZones) · [Fire weather zones](https://www.weather.gov/gis/FireZones)

Place these archives alongside the two Natural Earth basemap archives. With `pyshp==3.0.3` and `shapely==2.1.2` installed, run `python scripts/build_geometry_data.py DIRECTORY` from the repository root. The generated files ship with the integration. [Natural Earth land](https://naturalearth.s3.amazonaws.com/50m_physical/ne_50m_land.zip) · [Natural Earth populated places](https://naturalearth.s3.amazonaws.com/10m_cultural/ne_10m_populated_places_simple.zip)
