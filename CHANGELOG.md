# Changelog

## 0.2.15
- Dissolve `Missing_Contour_Marker` output by original contour ID (`SRC_ID`), producing one multipart feature per source contour.
- Dissolve `Estimated_Missing_Contour_Lines` output by `ERR_ID`, producing one multipart feature per error ID.
- Preserve disconnected geometry parts and retain associated event, pair, and missing-elevation references in the output attributes.

## 0.2.14
- Only adds the final “Contour Completeness Check” output layer to the QGIS project.
- Keeps slice-line and intersection-point diagnostic layers internal; they are no longer plotted or added to the Layers panel.
- No changes to analysis, AOI handling, result classification, or output feature generation.

## 0.2.13
- Updated AOI checkbox label to Indonesian: “Gunakan AOI boundary untuk meningkatkan akurasi hasil”.
- Added AOI source selection: loaded project layer or external vector file.
- Added AOI file picker for Shapefile, GeoPackage, GeoJSON, GML, and KML sources.
- Disabled the temporary Global Check, Diagnostic Layer Materialization, and Pre-export Data Summary diagnostic popups.
- Retained diagnostic logging and all analysis algorithms.

## 0.2.12
- Increased the `SLICE_SRC` string field length from 16 to 64 characters in the temporary Global Slice Lines and Contour Intersection Points diagnostic layers.
- Fixes QGIS memory-provider insertion failures when AOI-clipped slice source identifiers exceed 16 characters.
- No changes to contour analysis, AOI clipping, local checks, or output feature generation.

## 0.2.11
- Expanded diagnostic layer materialization reporting with layer/provider validity, declared and actual WKB geometry types, geometry validity, field/attribute counts, provider lastError/errors, and an isolated single-feature scratch-layer test.
- No changes to contour analysis, AOI clipping, endpoint checks, small-feature checks, or output feature generation.

## 0.2.10
- Added stage-by-stage diagnostics for temporary Global Slice Lines and Contour Intersection Points memory-layer materialization.
- Reports source record counts, successfully constructed QgsFeature counts, construction errors, addFeatures provider result, provider-reported inserted counts when available, and final memory-layer featureCount.
- Does not change contour checking, AOI clipping, local checks, or output feature generation.

## 0.2.9
- Added a pre-export summary immediately after all checking and output-feature preparation are complete.
- Reports the exact total number of features prepared for the selected output format, including a breakdown by `LINE_TYPE`.
- Reports Global Slice Lines, Contour Intersection Points, Contour Errors, Candidate Markers, and Estimated Missing-Contour Lines separately; these diagnostic layers remain temporary memory layers and are not included in the unified output file.
- Does not change Global Check, Local Endpoint Check, Local Small-Feature Check, or output geometry generation.

## 0.2.7
- Selects the working WGS84 / UTM zone by the greatest cumulative contour-line length, including when the source layer is already projected or spans multiple UTM zones.
- Transforms contour and AOI geometries into the same working CRS before analysis.
- Repairs invalid AOI polygon geometries and dissolves polygon features using unary union before clipping global slices.
- Restricts the global scan extent to the overlap between the contour extent and AOI extent.
- Adds diagnostics for empty AOI-clipped slices and extracted in-AOI segments.

# Changelog

## 0.2.5
- Added adaptive extension for Local Endpoint and Local Small-Feature slices.
- Each directional ray extends by one slice interval until it intersects a different source contour ID or reaches the AOI/layer extent.
- Endpoint source contour IDs are retained for adaptive stopping criteria.

## 0.2.4
- Replaced the single-stage progress dialog with a unified three-stage processing monitor.
- Added system RAM and QGIS process RAM monitoring (process RAM uses psutil when available).
- Added temporary diagnostic slice-line and intersection-point layers to the QGIS project.


## 0.1.0 — Initial development package
- Added QGIS plugin interface for selecting contour input, elevation field, analysis intervals, optional AOI, output format, and destination folder.
- Integrated horizontal and vertical scan-slice contour checking.
- Retained endpoint-driven supplementary scan logic and convex-hull boundary handling from the source script.
- Added progress percentage, elapsed time, and estimated remaining time display.
- Added a unified polyline output with `ERR_TYPE`, `ERR_ID`, `ELEVATION`, and `MISS_ELEV` attributes.
- Added Shapefile, GeoPackage, and GeoJSON output options.


## 0.1.1
- Fixed and clarified input-source selection for loaded QGIS layers, Shapefiles, and File Geodatabases.
- Added File Geodatabase polyline feature-class discovery.
- Connected source and output Browse controls and refreshed elevation-field selection when the source changes.


## 0.1.2
- Added the plugin icon to the QGIS toolbar action; the toolbar button uses the icon without a visible text label.


## 0.1.4
- Fixed a post-processing NameError caused by obsolete output-path variables after the unified output file had been written.

\n\n## 0.1.5
- Renamed the unified output attribute `ERR_TYPE` to `LINE_TYPE`.
- Renamed the scan spacing control to “Checking interval (metres)” and set its default to 100 m.
- Added an automatic contour-interval estimate based on the most common positive adjacent elevation difference in the selected elevation field; the value remains manually editable.
- Added an estimate of regular horizontal and vertical slice counts, with an explanation of later endpoint and small-feature checks.


## 0.1.6
- Simplified the regular-check estimate to show only the total number of checks, without horizontal and vertical subtotals.
\n\n## 0.1.7\n- Added a Cancel button to the progress dialog with cooperative cancellation checkpoints during regular, endpoint, and small-feature analysis.\n- Cancelled runs return to the main dialog and do not add incomplete output layers to the QGIS project.\n
\n\n## 0.1.8
- Final output geometries are transformed from the working analysis CRS back to the original input layer CRS before writing Shapefile, GeoPackage, or GeoJSON outputs.
- The contour analysis itself remains in a metric working CRS when reprojection is required.

\n\n## 0.1.9
- Added a WGS 84 (EPSG:4326) fallback when the input layer CRS is invalid or has no recognized authority ID.
- The fallback assigns CRS metadata without changing coordinate values; analysis still uses a metric UTM working CRS and output is transformed back to the resolved input CRS.

\n\n## 0.2.0
- Added input CRS detection and display, including CRS description, authority ID, ellipsoid and map units.
- Added a native QGIS CRS selection dialog for undefined or unrecognized input CRS definitions.
- Removed the automatic WGS 84 fallback. The user must confirm/select the correct input CRS before analysis.
- Analysis CRS selection and final output CRS now use the confirmed input CRS.

\n\n## 0.2.1
- Renamed the regular slice-checking phase to Global Check.
- Changed endpoint checks to local horizontal and vertical slices, each 2 × the checking interval long and centered on each derived endpoint location.
- Changed small-feature checks to local horizontal and vertical slices, each 2 × the checking interval long and centered on each unchecked feature bounding-box centre.
- Removed full-extent rescans from endpoint and small-feature phases to reduce unnecessary processing.


## 0.2.2
- Changed progress reporting to use a stage-relative 0–100% scale.
- Global Check, Local Endpoint Check, and Local Small-Feature Check each start at 0% and finish at 100%.
- Added explicit stage-start messages and completion handling for stages with no work.


## 0.2.3
- Optimized convex-hull vertex collection to iterate geometry vertices directly instead of converting multipart geometries into nested Python coordinate lists.
- Reduces temporary Python memory allocation during endpoint-boundary preparation on large contour datasets.

## 0.2.8
- Added an on-screen Global Check diagnostic summary (original slices, AOI-intersecting slices, extracted segments, empty slices, and contour intersection points).
- Improved extraction of line components from multipart and geometry-collection intersection results, with visible extraction exception counts.
- Kept Local Endpoint and Local Small-Feature algorithms unchanged.
