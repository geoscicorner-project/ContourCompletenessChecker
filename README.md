# Contour Completeness Checker

A QGIS plugin for identifying potential missing contour levels using horizontal and vertical scan slices.

## Features

- Automatically selects a working WGS84 / UTM zone based on the greatest cumulative contour-line length, including multi-zone datasets.
- Transforms the AOI into the same working CRS, repairs invalid polygon geometry, and clips global scan slices to valid AOI segments.

- Uses a selected contour polyline layer from the current QGIS project.
- Selectable elevation attribute, contour interval, and scan-slice interval.
- Horizontal and vertical scan-slice analysis.
- Local endpoint checks using short horizontal and vertical slices, each 2 × checking interval long.
- Uses an AOI boundary when supplied; otherwise builds a convex-hull boundary and applies the source algorithm's 10 cm endpoint tolerance.
- Displays progress percentage, elapsed time, and estimated remaining time.
- Writes one classified polyline dataset containing both output classes:
  - `Missing_Contour_Marker`
  - `Estimated_Missing_Contour_Lines`
- Output formats: ESRI Shapefile, GeoPackage, and GeoJSON.

## Output attributes

| Field | Description |
|---|---|
| `ERR_TYPE` | Output class: `Missing_Contour_Marker` or `Estimated_Missing_Contour_Lines` |
| `ERR_ID` | Primary error event identifier |\n| `ERR_IDS` | All contributing event identifiers (especially for reconstructed lines) |
| `ELEVATION` | Elevation associated with the marker source contour or estimated contour |
| `MISS_ELEV` | Missing contour elevation(s) identified by the scan analysis |
| `SRC_ID` | Source contour feature ID, when applicable |
| `PAIR_ID` | Source contour pair identifier, when applicable |

Both output classes are polylines. Therefore, they can be stored together in one Shapefile and filtered or styled by `ERR_TYPE`.

## Installation

1. Download `ContourCompletenessChecker.zip` from the GitHub repository release or repository assets.
2. In QGIS, open **Plugins → Manage and Install Plugins…**.
3. Choose **Install from ZIP**.
4. Select the ZIP file.
5. Enable **Contour Completeness Checker**.

## Usage

1. Load the contour polyline layer into QGIS.
2. Open **Plugins → Contour Completeness Checker**.
3. Select the contour layer and its elevation field.
4. Set the contour interval and scan-slice interval in metres.
5. Choose an optional AOI boundary layer for endpoint filtering.
6. Select the output format and output folder.
7. Click **Run check**.

## Output filtering

Use the `ERR_TYPE` field to filter the output:

- `ERR_TYPE = 'Missing_Contour_Marker'`
- `ERR_TYPE = 'Estimated_Missing_Contour_Lines'`

## Notes and limitations

- Analysis distances are performed in metres. Geographic or non-metre input layers are transformed to a suitable WGS 84 / UTM working CRS.
- The output is written in the working CRS used by the analysis.
- The estimated missing contour lines are the result of the reconstruction logic already present in the source checking script; they are estimates and should be reviewed before authoritative use.
- Shapefile field names are limited to 10 characters; `ERR_TYPE` is used instead of `ERROR_TYPE`.
- The progress dialog is updated during scan processing. Large datasets may take substantial time.
- This initial package has been syntax-checked, but should be tested in the target QGIS installation with representative datasets before production use.

## Repository

https://github.com/geosciconer-project/ContourCompletenessChecker

## License

GPL-2.0-or-later. See [LICENSE](LICENSE).


## Development diagnostics in version 0.2.4

Version 0.2.4 introduces a unified processing monitor showing the progress,
processed/total count, and estimated remaining time for Global Check, Local
Endpoint Check, and Local Small-Feature Check in one dialog. It also reports
system RAM usage and, when the optional `psutil` module is available, the
QGIS process RAM usage.

For temporary performance evaluation, the plugin adds two diagnostic memory
layers to the current QGIS project after a successful run:
- `CCC Diagnostic - Slice Lines`: every generated horizontal and vertical slice,
  with direction, source stage, and slice position.
- `CCC Diagnostic - Intersection Points`: all contour/slice intersection points,
  with source feature, elevation, direction, slice source, and candidate details.

These diagnostic layers are temporary project layers and are not written into
the main output dataset.


## Adaptive local slice extension in version 0.2.5

Local Endpoint Check and Local Small-Feature Check now retain their source
contour feature IDs. Each local slice is extended independently in both
directions, one slice interval at a time, until an intersection with a
different contour feature ID is found or the AOI/layer extent is reached.
The final assembled local slice is evaluated once by the existing contour
intersection and candidate-pair logic. Global Check is unchanged.
