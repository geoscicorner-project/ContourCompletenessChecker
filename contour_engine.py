# -*- coding: utf-8 -*-
"""
Contour Slice Checker FINAL - QGIS 3.x

Run from the QGIS Python Console:
    exec(open(r'C:\\path\\to\\contour_slice_checker.py', encoding='utf-8').read())

The script:
1. Loads Kontur_Clip from the FileGDB (feature dataset Hipsografi).
2. Creates horizontal and vertical scan slices.
3. Orders contour intersections on each slice and flags adjacent crossings
   whose elevation difference is greater than one contour interval.
4. Creates radial buffers around each candidate crossing, but clips each
   buffer against ONLY the source contour that generated that crossing.
5. Merges extracted marker pieces by source feature ID (multipart allowed).
6. Exports Shapefiles and a CSV report.

Review the parameters below before running.

Final: Reconstructs missing-contour lines from H/V slice estimates, separates disconnected runs, and checks sampled links against the two source contours. AOI and convex-hull endpoint tolerances are both 10 cm.
"""

import os
import csv
import math
import traceback
import time
from collections import defaultdict

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsVectorLayer, QgsMapLayer, QgsFeature, QgsGeometry, QgsPointXY, QgsPoint,
    QgsRectangle, QgsSpatialIndex, QgsFields, QgsField, QgsWkbTypes,
    QgsVectorFileWriter, QgsCoordinateTransformContext, QgsProject,
    QgsFeatureRequest, QgsVectorLayerSimpleLabeling, QgsPalLayerSettings,
    QgsTextFormat, QgsUnitTypes, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsProject
)
from qgis.PyQt.QtWidgets import (QInputDialog, QMessageBox, QFileDialog, QDialog, QDoubleSpinBox, QDialogButtonBox, QVBoxLayout, QHBoxLayout, QLabel, QFrame)

# -----------------------------------------------------------------------------
# USER PARAMETERS
# -----------------------------------------------------------------------------
GDB_PATH = ""
FEATURE_DATASET = ""
FEATURE_CLASS = ""
ELEVATION_FIELD_DEFAULT = "VALKNT"

contour_interval = 2.0       # Elevation interval, in map units (normally metres)
slice_interval = 20.0        # Distance between horizontal/vertical slices
ELEVATION_TOLERANCE = 0.001   # Tolerance when comparing elevation intervals
GEOMETRY_TOLERANCE = 0.001    # Used only for duplicate intersection coordinates

OUTPUT_FOLDER = os.path.join(os.path.expanduser("~"), "Downloads", "Contour_Completeness_Check")
ADD_OUTPUTS_TO_QGIS = True

# If True, an elevation difference that is not an integer multiple of the
# contour interval is also reported as an anomaly. It is NOT labelled as a
# definite missing contour.
REPORT_NON_MULTIPLE_INTERVAL = True


PLUGIN_CONFIG = {}
PROGRESS_CALLBACK = None
CANCEL_CALLBACK = None


class AnalysisCancelled(Exception):
    """Raised when the user requests cancellation of the running analysis."""


def set_plugin_config(config, progress_callback=None, cancel_callback=None):
    global PLUGIN_CONFIG, PROGRESS_CALLBACK, CANCEL_CALLBACK, OUTPUT_FOLDER
    PLUGIN_CONFIG = config or {}
    PROGRESS_CALLBACK = progress_callback
    CANCEL_CALLBACK = cancel_callback
    OUTPUT_FOLDER = PLUGIN_CONFIG.get("output_folder", OUTPUT_FOLDER)


def check_cancelled():
    if CANCEL_CALLBACK and CANCEL_CALLBACK():
        raise AnalysisCancelled("Analysis cancelled by user.")


def report_progress(value, message, eta_text="", stage=None, count=None, total=None):
    if PROGRESS_CALLBACK:
        try:
            progress_value = -1 if int(value) < 0 else max(0, min(100, int(value)))
            PROGRESS_CALLBACK(progress_value, message, eta_text,
                              stage, count, total)
        except TypeError:
            # Backward compatibility with older three-argument callbacks.
            PROGRESS_CALLBACK(max(0, min(100, int(value))), message, eta_text)
    log("%d%% - %s %s" % (value, message, eta_text))

# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------

def log(msg):
    print(u"[Contour Slice Checker] " + str(msg))


def make_output_folder(path):
    if not os.path.isdir(path):
        os.makedirs(path)


def load_contour_layer():
    """Try common OGR FileGDB layer-name conventions."""
    candidates = [
        GDB_PATH + "|layername=" + FEATURE_DATASET + "/" + FEATURE_CLASS,
        GDB_PATH + "|layername=" + FEATURE_CLASS,
        GDB_PATH + "|layername=" + FEATURE_DATASET + "_" + FEATURE_CLASS,
    ]
    for uri in candidates:
        lyr = QgsVectorLayer(uri, FEATURE_CLASS, "ogr")
        if lyr.isValid() and lyr.featureCount() > 0:
            log("Layer berhasil dibuka: " + uri)
            return lyr
        if lyr.isValid():
            # An empty source may still be the right layer, but continue to
            # test other URI conventions first.
            empty_layer = lyr
    if 'empty_layer' in locals():
        return empty_layer
    raise Exception(
        "Layer tidak berhasil dibuka. Periksa nama feature dataset/feature class. "
        "URI yang dicoba: " + " ; ".join(candidates)
    )


def numeric_value(value):
    try:
        if value is None:
            return None
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except Exception:
        return None


def add_point_intersections(geom, result):
    """Recursively collect point parts from a line/slice intersection."""
    if geom is None or geom.isEmpty():
        return
    gtype = QgsWkbTypes.geometryType(geom.wkbType())
    if gtype == QgsWkbTypes.PointGeometry:
        if geom.isMultipart():
            for p in geom.asMultiPoint():
                result.append(QgsPointXY(p))
        else:
            result.append(QgsPointXY(geom.asPoint()))
        return
    # Geometry collections and line overlaps are handled recursively.
    if hasattr(geom, "constGet"):
        try:
            for i in range(geom.constGet().numGeometries()):
                part = QgsGeometry(geom.constGet().geometryN(i))
                add_point_intersections(part, result)
        except Exception:
            pass


def add_line_parts(geom, result, diagnostics=None):
    """Collect line components from LineString/MultiLineString/GeometryCollection.

    QgsGeometry.asGeometryCollection() is used for collection recursion; this is
    more reliable across QGIS 3.x Python bindings than relying only on
    constGet().geometryN(). Optional diagnostics retain extraction failures.
    """
    if geom is None or geom.isEmpty():
        return
    try:
        gtype = QgsWkbTypes.geometryType(geom.wkbType())
        if gtype == QgsWkbTypes.LineGeometry:
            if geom.isMultipart():
                parts = geom.asMultiPolyline()
            else:
                part = geom.asPolyline()
                parts = [part] if part else []
            for part in parts:
                if len(part) >= 2:
                    result.append(QgsGeometry.fromPolylineXY(
                        [QgsPointXY(p) for p in part]))
            return

        # GeometryCollection may contain lines mixed with points/polygons.
        try:
            children = geom.asGeometryCollection()
        except Exception:
            children = []
        if children:
            for child in children:
                add_line_parts(child, result, diagnostics)
            return

        # Fallback for QGIS builds exposing collection members only via
        # the underlying abstract geometry API.
        if hasattr(geom, "constGet"):
            abstract = geom.constGet()
            count = abstract.numGeometries()
            for i in range(count):
                child = QgsGeometry(abstract.geometryN(i))
                add_line_parts(child, result, diagnostics)
    except Exception as ex:
        if diagnostics is not None:
            diagnostics["errors"] += 1
            if len(diagnostics["messages"]) < 5:
                diagnostics["messages"].append(str(ex))


def get_intersection_points(feature_geom, slice_geom):
    try:
        inter = feature_geom.intersection(slice_geom)
        pts = []
        add_point_intersections(inter, pts)
        return pts, (inter is not None and not inter.isEmpty() and
                     QgsWkbTypes.geometryType(inter.wkbType()) == QgsWkbTypes.LineGeometry)
    except Exception:
        return [], False


def create_memory_layer(geometry_name, crs, fields):
    lyr = QgsVectorLayer(geometry_name + "?crs=" + crs.authid(),
                         "temporary_output", "memory")
    pr = lyr.dataProvider()
    pr.addAttributes(fields)
    lyr.updateFields()
    return lyr


def write_shapefile(layer, path):
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "ESRI Shapefile"
    options.fileEncoding = "UTF-8"
    options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, QgsCoordinateTransformContext(), options
    )
    # QGIS 3 returns (error_code, error_message, new_file, new_layer)
    if result[0] != QgsVectorFileWriter.NoError:
        raise Exception("Gagal menulis " + path + ": " + str(result[1]))
    return path


def safe_delete_shapefile(path):
    base = os.path.splitext(path)[0]
    for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix"):
        p = base + ext
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


def make_line_feature(layer, geom, attrs):
    f = QgsFeature(layer.fields())
    g = QgsGeometry(geom)
    if not g.isEmpty() and not g.isMultipart():
        g.convertToMultiType()
    f.setGeometry(g)
    f.setAttributes(attrs)
    return f


def make_point_feature(layer, point, attrs):
    f = QgsFeature(layer.fields())
    f.setGeometry(QgsGeometry.fromPointXY(point))
    f.setAttributes(attrs)
    return f


def extract_line_parts(geom, parts):
    """Collect line coordinate arrays from line, multiline or collection geometry."""
    if geom is None or geom.isEmpty():
        return
    gtype = QgsWkbTypes.geometryType(geom.wkbType())
    if gtype == QgsWkbTypes.LineGeometry:
        if geom.isMultipart():
            for line in geom.asMultiPolyline():
                if len(line) >= 2:
                    parts.append(line)
        else:
            line = geom.asPolyline()
            if len(line) >= 2:
                parts.append(line)
        return
    try:
        for subgeom in geom.asGeometryCollection():
            extract_line_parts(subgeom, parts)
    except Exception:
        pass


def get_line_parts(geom):
    parts = []
    extract_line_parts(geom, parts)
    return parts


def choose_utm_crs(layer):
    """Choose WGS84 UTM zone from layer centroid, based on geographic extent."""
    ext = layer.extent()
    center = QgsPointXY((ext.xMinimum() + ext.xMaximum()) / 2.0,
                        (ext.yMinimum() + ext.yMaximum()) / 2.0)
    source_crs = layer.crs()
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    if source_crs != wgs84:
        transform = QgsCoordinateTransform(source_crs, wgs84,
                                           QgsProject.instance())
        center = transform.transform(center)

    lon, lat = center.x(), center.y()
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    zone = max(1, min(60, zone))
    epsg = (32600 + zone) if lat >= 0 else (32700 + zone)
    utm = QgsCoordinateReferenceSystem("EPSG:%d" % epsg)
    return utm, zone, lat, lon


def utm_zone_from_lon(lon):
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    return max(1, min(60, zone))


def haversine_length_m(lon1, lat1, lon2, lat2):
    """Approximate geodesic segment length on WGS84, in metres."""
    radius = 6371008.8
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2.0) ** 2 +
         math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2)
    return 2.0 * radius * math.asin(min(1.0, math.sqrt(max(0.0, a))))


def choose_dominant_utm_crs(layer):
    """
    Select the UTM zone containing the greatest total contour-line length.
    Each consecutive contour segment is assigned to the zone of its
    geographic midpoint. This works for both geographic and projected input.
    """
    source_crs = layer.crs()
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    transform = None
    if source_crs != wgs84:
        transform = QgsCoordinateTransform(
            source_crs, wgs84, QgsProject.instance()
        )

    length_by_zone = defaultdict(float)
    length_by_hemisphere = defaultdict(float)
    total_segments = 0
    skipped_segments = 0
    total_features = layer.featureCount()

    for feature_no, feature in enumerate(layer.getFeatures()):
        if feature_no % 1000 == 0:
            check_cancelled()
            if feature_no:
                log("UTM zone analysis: %d / %d contour features" %
                    (feature_no, total_features))
        geom = QgsGeometry(feature.geometry())
        if geom is None or geom.isEmpty():
            continue
        if transform is not None:
            try:
                geom.transform(transform)
            except Exception:
                skipped_segments += 1
                continue

        parts = get_line_parts(geom)
        for part in parts:
            for i in range(len(part) - 1):
                p1 = part[i]
                p2 = part[i + 1]
                lon1, lat1 = p1.x(), p1.y()
                lon2, lat2 = p2.x(), p2.y()
                if (not (-180.0 <= lon1 <= 180.0 and
                         -180.0 <= lon2 <= 180.0 and
                         -90.0 <= lat1 <= 90.0 and
                         -90.0 <= lat2 <= 90.0)):
                    skipped_segments += 1
                    continue
                mid_lon = (lon1 + lon2) / 2.0
                mid_lat = (lat1 + lat2) / 2.0
                zone = utm_zone_from_lon(mid_lon)
                hemi = "N" if mid_lat >= 0 else "S"
                length = haversine_length_m(lon1, lat1, lon2, lat2)
                if length <= 0:
                    continue
                length_by_zone[(zone, hemi)] += length
                length_by_hemisphere[hemi] += length
                total_segments += 1

    if not length_by_zone:
        raise Exception(
            "Unable to determine the dominant UTM zone from contour vertices. "
            "Check the input CRS and coordinate values."
        )

    dominant = max(length_by_zone, key=length_by_zone.get)
    zone, hemi = dominant
    epsg = (32600 + zone) if hemi == "N" else (32700 + zone)
    target = QgsCoordinateReferenceSystem("EPSG:%d" % epsg)
    if not target.isValid():
        raise Exception("Unable to create the selected UTM CRS: EPSG:%d" % epsg)

    ranked = sorted(length_by_zone.items(), key=lambda item: item[1], reverse=True)
    log("Dominant UTM zone selected: %d%s (EPSG:%d)" %
        (zone, hemi, epsg))
    for (z, h), length in ranked[:5]:
        pct = 100.0 * length / sum(length_by_zone.values())
        log("  UTM %d%s: %.3f km (%.2f%% of contour length)" %
            (z, h, length / 1000.0, pct))
    log("UTM zone analysis: %d segments processed; %d skipped." %
        (total_segments, skipped_segments))
    return target, zone, hemi


def make_working_layer(source_layer, target_crs):
    """Copy input features to a memory layer, reprojecting if necessary."""
    geom_name = QgsWkbTypes.displayString(source_layer.wkbType())
    if not geom_name:
        geom_name = "MultiLineString"
    uri = geom_name + "?crs=" + target_crs.authid()
    work = QgsVectorLayer(uri, "Contour_Working_UTM", "memory")
    provider = work.dataProvider()
    provider.addAttributes(list(source_layer.fields()))
    work.updateFields()

    needs_transform = source_layer.crs() != target_crs
    transform = None
    if needs_transform:
        transform = QgsCoordinateTransform(source_layer.crs(), target_crs,
                                           QgsProject.instance())

    batch = []
    total = source_layer.featureCount()
    for i, source_feature in enumerate(source_layer.getFeatures()):
        f = QgsFeature(work.fields())
        f.setAttributes(source_feature.attributes())
        geom = QgsGeometry(source_feature.geometry())
        if transform is not None and not geom.isEmpty():
            geom.transform(transform)
        f.setGeometry(geom)
        batch.append(f)
        if len(batch) >= 1000:
            provider.addFeatures(batch)
            batch = []
            if total:
                log("Reproyeksi/copy fitur: %d / %d" % (i + 1, total))
    if batch:
        provider.addFeatures(batch)
    work.updateExtents()
    return work


def ask_user_parameters(layer):
    """Ask elevation field and analysis intervals; show metric extent."""
    field_names = [field.name() for field in layer.fields()]
    preferred = ELEVATION_FIELD_DEFAULT if ELEVATION_FIELD_DEFAULT in field_names else (
        field_names[0] if field_names else ""
    )
    start_index = field_names.index(preferred) if preferred in field_names else 0
    elev_field, ok = QInputDialog.getItem(
        None, "Contour Slice Checker",
        "Pilih kolom atribut elevasi kontur:", field_names,
        start_index, False
    )
    if not ok or not elev_field:
        raise Exception("Pemilihan field elevasi dibatalkan.")

    contour_interval, ok = QInputDialog.getDouble(
        None, "Contour Slice Checker",
        "Masukkan interval kontur (meter):", 2.0, 0.000001, 1000000.0, 3
    )
    if not ok:
        raise Exception("Input interval kontur dibatalkan.")

    extent = layer.extent()
    if aoi_area_geom is not None and not aoi_area_geom.isEmpty():
        # Restrict the scan grid to the overlap of the contour and AOI extents.
        extent = extent.intersect(aoi_area_geom.boundingBox())
        if extent.isNull() or extent.isEmpty():
            raise Exception("AOI and contour layer have no overlapping extent in the working CRS.")
    xmin, xmax = extent.xMinimum(), extent.xMaximum()
    ymin, ymax = extent.yMinimum(), extent.yMaximum()
    width, height = xmax - xmin, ymax - ymin

    info = (
        "Bounding box dalam CRS kerja (meter):\\n\\n"
        "Xmin: %.3f m\\nXmax: %.3f m\\n"
        "Ymin: %.3f m\\nYmax: %.3f m\\n\\n"
        "Lebar: %.3f m\\nTinggi: %.3f m\\n\\n"
        "Nilai ini digunakan untuk membuat slice horizontal dan vertikal."
    ) % (xmin, xmax, ymin, ymax, width, height)
    QMessageBox.information(None, "Bounding Box Data", info)

    slice_interval, ok = QInputDialog.getDouble(
        None, "Contour Slice Checker",
        "Masukkan interval slice (meter):", 20.0, 0.01, 10000000.0, 2
    )
    if not ok:
        raise Exception("Input interval slice dibatalkan.")

    # Radius buffer otomatis disamakan dengan interval slice.
    buffer_radius = slice_interval
    return elev_field, contour_interval, slice_interval, buffer_radius



def ask_endpoint_boundary_tolerance(raw_endpoint_records, endpoint_coordinate_counts,
                                    has_boundary, default_value=0.5):
    """
    Display a clean English summary and allow the user to adjust the
    endpoint-to-boundary tolerance before Local Endpoint Check begins.
    """
    dialog = QDialog()
    dialog.setWindowTitle("Endpoint Filtering Settings")
    dialog.setMinimumWidth(470)
    dialog.setStyleSheet("""
        QDialog { background: #f7f8fa; }
        QLabel#dialogTitle { color: #202b3c; font-size: 15px; font-weight: 600; }
        QLabel#sectionLabel { color: #526174; font-weight: 600; }
        QDoubleSpinBox { min-height: 27px; padding: 2px 7px; }
        QFrame#summaryPanel {
            background: white; border: 1px solid #d9dee7; border-radius: 5px;
        }
        QLabel#summaryValue { color: #1e293b; font-size: 13px; }
        QLabel#summaryNote { color: #687588; font-size: 10px; }
        QPushButton { min-width: 90px; min-height: 27px; padding: 2px 10px; }
    """)

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(18, 16, 18, 14)
    layout.setSpacing(10)

    title = QLabel("Endpoint Boundary Filtering")
    title.setObjectName("dialogTitle")
    layout.addWidget(title)

    intro = QLabel(
        "Endpoints within the selected distance from the boundary "
        "will be excluded from Local Endpoint Check."
    )
    intro.setWordWrap(True)
    layout.addWidget(intro)

    tolerance_row = QHBoxLayout()
    tolerance_label = QLabel("Boundary tolerance")
    tolerance_label.setObjectName("sectionLabel")
    tolerance_spin = QDoubleSpinBox(dialog)
    tolerance_spin.setDecimals(3)
    tolerance_spin.setRange(0.0, 1000000.0)
    tolerance_spin.setSingleStep(0.1)
    tolerance_spin.setValue(float(default_value))
    tolerance_spin.setSuffix(" m")
    tolerance_spin.setToolTip("Endpoints at or within this distance from the boundary are excluded.")
    tolerance_row.addWidget(tolerance_label)
    tolerance_row.addStretch(1)
    tolerance_row.addWidget(tolerance_spin)
    layout.addLayout(tolerance_row)

    summary_panel = QFrame(dialog)
    summary_panel.setObjectName("summaryPanel")
    summary_layout = QVBoxLayout(summary_panel)
    summary_layout.setContentsMargins(12, 10, 12, 10)
    summary_layout.setSpacing(7)

    threshold_label = QLabel()
    threshold_label.setObjectName("summaryNote")
    boundary_count_label = QLabel()
    boundary_count_label.setObjectName("summaryValue")
    process_count_label = QLabel()
    process_count_label.setObjectName("summaryValue")
    coincident_count_label = QLabel()
    coincident_count_label.setObjectName("summaryNote")

    summary_layout.addWidget(threshold_label)
    summary_layout.addWidget(boundary_count_label)
    summary_layout.addWidget(process_count_label)
    summary_layout.addWidget(coincident_count_label)
    layout.addWidget(summary_panel)

    unique_records = []
    coincident_count = 0
    for rec in raw_endpoint_records:
        if endpoint_coordinate_counts[rec["coord_key"]] > 1:
            coincident_count += 1
        else:
            unique_records.append(rec)

    def refresh_summary(value=None):
        tolerance = tolerance_spin.value()
        if not has_boundary:
            boundary_count = 0
        else:
            boundary_count = sum(
                1 for rec in unique_records
                if rec["distance"] >= 0.0 and rec["distance"] <= tolerance
            )
        process_count = len(unique_records) - boundary_count
        threshold_label.setText(
            "Exclusion rule: distance ≤ %.3f m from the boundary" % tolerance
        )
        boundary_count_label.setText(
            "Endpoints excluded by boundary: %s" % format(boundary_count, ",")
        )
        process_count_label.setText(
            "Endpoints to be checked: %s" % format(process_count, ",")
        )
        coincident_count_label.setText(
            "Coincident endpoints excluded separately: %s" %
            format(coincident_count, ",")
        )

    tolerance_spin.valueChanged.connect(refresh_summary)
    refresh_summary()

    button_box = QDialogButtonBox(
        QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dialog
    )
    button_box.button(QDialogButtonBox.Ok).setText("Continue")
    button_box.button(QDialogButtonBox.Cancel).setText("Cancel")
    button_box.accepted.connect(dialog.accept)
    button_box.rejected.connect(dialog.reject)
    layout.addWidget(button_box)

    if dialog.exec_() != QDialog.Accepted:
        raise AnalysisCancelled("Endpoint filtering settings were cancelled.")
    return float(tolerance_spin.value())


# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------




# AOI selection helper (defined before run_checker is invoked)
def select_aoi_boundary(target_crs):
    """Ask whether an AOI boundary file should be used, then load and transform it."""
    answer = QMessageBox.question(
        None,
        "Batas AOI",
        "Apakah Anda ingin menggunakan file batas AOI untuk menyaring endpoint?",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No
    )
    if answer != QMessageBox.Yes:
        log("AOI tidak dipilih; seluruh endpoint akan diproses.")
        return None

    file_path, _ = QFileDialog.getOpenFileName(
        None,
        "Pilih file batas AOI",
        os.path.expanduser("~"),
        "Vector files (*.shp *.gpkg *.geojson *.json *.gml *.kml);;All files (*)"
    )
    if not file_path:
        log("Pemilihan file AOI dibatalkan; seluruh endpoint akan diproses.")
        return None

    aoi_layer = QgsVectorLayer(file_path, "AOI_boundary_input", "ogr")
    if not aoi_layer.isValid():
        raise Exception("File AOI tidak dapat dibuka: " + file_path)
    if aoi_layer.geometryType() not in (QgsWkbTypes.LineGeometry,
                                        QgsWkbTypes.PolygonGeometry):
        raise Exception("Layer AOI harus berupa garis atau poligon.")
    if not aoi_layer.crs().isValid():
        raise Exception("CRS file AOI tidak valid. Tetapkan CRS AOI terlebih dahulu.")

    boundary_parts = []
    transform = None
    if aoi_layer.crs() != target_crs:
        transform = QgsCoordinateTransform(
            aoi_layer.crs(), target_crs, QgsProject.instance()
        )

    for feat in aoi_layer.getFeatures():
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        if transform is not None:
            geom = QgsGeometry(geom)
            geom.transform(transform)

        if aoi_layer.geometryType() == QgsWkbTypes.LineGeometry:
            boundary_parts.append(geom)
        else:
            # Extract polygon rings explicitly; this avoids relying on
            # QgsGeometry.boundary(), which is unavailable in some QGIS builds.
            try:
                if geom.isMultipart():
                    polygons = geom.asMultiPolygon()
                else:
                    poly = geom.asPolygon()
                    polygons = [poly] if poly else []
                for polygon in polygons:
                    for ring in polygon:
                        if len(ring) >= 2:
                            boundary_parts.append(
                                QgsGeometry.fromPolylineXY([QgsPointXY(pt) for pt in ring])
                            )
            except Exception as ex:
                raise Exception("Gagal membaca ring poligon AOI: " + str(ex))

    if not boundary_parts:
        raise Exception("File AOI tidak memiliki geometri batas yang dapat digunakan.")

    boundary = QgsGeometry.collectGeometry(boundary_parts)
    if boundary is None or boundary.isEmpty():
        raise Exception("Geometri batas AOI kosong setelah diproses.")
    log("AOI dipilih: %s | Jenis: %s | CRS kerja: %s" %
        (file_path, "poligon" if aoi_layer.geometryType() == QgsWkbTypes.PolygonGeometry else "garis",
         target_crs.authid()))
    return boundary


def run_checker():
    make_output_folder(OUTPUT_FOLDER)
    report_progress(2, "Validating input layer")
    source_layer = PLUGIN_CONFIG.get("source_layer") or load_contour_layer()

    if source_layer.geometryType() != QgsWkbTypes.LineGeometry:
        raise Exception("Layer input harus berupa garis/polyline.")

    # Use the CRS explicitly confirmed by the user (or detected from the layer).
    # Never guess a datum for an undefined/custom input CRS.
    source_crs = PLUGIN_CONFIG.get("input_crs") or source_layer.crs()
    if not source_crs.isValid() or not source_crs.authid():
        raise Exception(
            "Input CRS is undefined or has no recognized authority ID. "
            "Select the correct input CRS and datum in the plugin dialog."
        )
    if source_layer.crs() != source_crs:
        # Assign the selected CRS definition to the layer object; coordinate
        # values are not modified by setCrs().
        source_layer.setCrs(source_crs)

    # All spatial analysis is performed in one metric UTM working CRS.
    # Select the zone with the greatest total contour-line length, including
    # when the source layer is already projected or crosses UTM zones.
    target_crs, zone, hemisphere = choose_dominant_utm_crs(source_layer)
    log("Input CRS: %s (%s); source units: %s." %
        (source_crs.description(), source_crs.authid(),
         str(source_crs.mapUnits())))
    log("Working CRS selected from dominant contour length: %s (UTM %d%s)." %
        (target_crs.authid(), zone, hemisphere))
    if source_crs == target_crs:
        layer = source_layer
        log("Input layer already uses the selected working CRS.")
    else:
        layer = make_working_layer(source_layer, target_crs)

    if layer.featureCount() == 0:
        raise Exception("Layer kerja tidak memiliki fitur.")

    if PLUGIN_CONFIG:
        elev_field = PLUGIN_CONFIG["elevation_field"]
        contour_interval = float(PLUGIN_CONFIG["contour_interval"])
        slice_interval = float(PLUGIN_CONFIG["slice_interval"])
        buffer_radius = slice_interval
        aoi_boundary = None
        aoi_area_geom = None
        aoi_layer = PLUGIN_CONFIG.get("aoi_layer")
        if aoi_layer is not None:
            boundary_parts = []
            aoi_polygon_geometries = []
            transform = None
            if aoi_layer.crs() != layer.crs():
                transform = QgsCoordinateTransform(
                    aoi_layer.crs(), layer.crs(), QgsProject.instance()
                )
            aoi_feature_count = 0
            aoi_invalid_count = 0
            for af in aoi_layer.getFeatures():
                ag = QgsGeometry(af.geometry())
                if ag is None or ag.isEmpty():
                    continue
                if transform is not None:
                    ag.transform(transform)
                if not ag.isGeosValid():
                    aoi_invalid_count += 1
                    ag = ag.makeValid()
                if ag is None or ag.isEmpty():
                    continue
                aoi_feature_count += 1
                if aoi_layer.geometryType() == QgsWkbTypes.LineGeometry:
                    boundary_parts.append(ag)
                elif aoi_layer.geometryType() == QgsWkbTypes.PolygonGeometry:
                    # makeValid can return a geometry collection; retain polygon parts.
                    polygon_parts = []
                    def collect_polygons(g):
                        if g is None or g.isEmpty():
                            return
                        if QgsWkbTypes.geometryType(g.wkbType()) == QgsWkbTypes.PolygonGeometry:
                            polygon_parts.append(QgsGeometry(g))
                            return
                        try:
                            for sub in g.asGeometryCollection():
                                collect_polygons(sub)
                        except Exception:
                            pass
                    collect_polygons(ag)
                    for pg in polygon_parts:
                        aoi_polygon_geometries.append(pg)
                        polygons = pg.asMultiPolygon() if pg.isMultipart() else [pg.asPolygon()]
                        for polygon in polygons:
                            for ring in polygon:
                                if len(ring) >= 2:
                                    boundary_parts.append(
                                        QgsGeometry.fromPolylineXY(
                                            [QgsPointXY(pt) for pt in ring]
                                        )
                                    )

            if boundary_parts:
                aoi_boundary = QgsGeometry.collectGeometry(boundary_parts)
            if aoi_polygon_geometries:
                # Unary union dissolves overlaps and internal boundaries robustly.
                aoi_area_geom = QgsGeometry.unaryUnion(aoi_polygon_geometries)
                if aoi_area_geom is None or aoi_area_geom.isEmpty():
                    raise Exception("AOI polygon union returned an empty geometry.")
                if not aoi_area_geom.isGeosValid():
                    aoi_area_geom = aoi_area_geom.makeValid()
                if aoi_area_geom is None or aoi_area_geom.isEmpty():
                    raise Exception("AOI polygon is invalid after repair.")
                if not aoi_area_geom.boundingBox().intersects(layer.extent()):
                    raise Exception(
                        "AOI does not overlap the contour layer after CRS transformation. "
                        "Check the AOI CRS and source coordinates."
                    )
            log("AOI validation: %d valid feature(s), %d repaired feature(s); CRS %s." %
                (aoi_feature_count, aoi_invalid_count, layer.crs().authid()))
            if aoi_layer.geometryType() == QgsWkbTypes.PolygonGeometry and aoi_area_geom is None:
                raise Exception("AOI polygon contains no usable polygon geometry.")

    else:
        elev_field, contour_interval, slice_interval, buffer_radius = ask_user_parameters(layer)
        aoi_boundary = select_aoi_boundary(layer.crs())
        aoi_area_geom = None

    field_names = [f.name() for f in layer.fields()]
    if elev_field not in field_names:
        raise Exception("Field elevasi tidak ditemukan: " + elev_field)

    extent = layer.extent()
    xmin, xmax = extent.xMinimum(), extent.xMaximum()
    ymin, ymax = extent.yMinimum(), extent.yMaximum()
    if extent.isNull() or extent.isEmpty():
        raise Exception("Bounding box layer kosong.")

    log("Layer: %s | Fitur: %s" % (layer.name(), layer.featureCount()))
    log("CRS sumber: %s | CRS kerja: %s" %
        (source_crs.authid(), layer.crs().authid()))
    log("Extent meter: X %.3f..%.3f, Y %.3f..%.3f" %
        (xmin, xmax, ymin, ymax))
    log("Lebar: %.3f m | Tinggi: %.3f m" % (xmax - xmin, ymax - ymin))
    log("Field elevasi: %s | Interval kontur: %s | Interval slice: %s | Radius buffer: %s" %
        (elev_field, contour_interval, slice_interval, buffer_radius))

    # Build spatial index once. Candidate geometries are cached lazily.
    report_progress(30, "Building spatial index")
    log("Membangun spatial index...")
    index = QgsSpatialIndex(layer.getFeatures())
    feature_cache = {}
    elevation_cache = {}

    def fetch_feature(fid):
        if fid not in feature_cache:
            req = QgsFeatureRequest().setFilterFid(fid)
            f = next(layer.getFeatures(req), None)
            if f is not None:
                feature_cache[fid] = QgsFeature(f)
                elevation_cache[fid] = numeric_value(f[elev_field])
        return feature_cache.get(fid)

    events = []
    all_intersections = []
    error_records = []
    missing_points = []
    marker_geoms_by_fid = defaultdict(list)
    marker_elev_by_fid = {}
    seen_candidate_keys = set()
    checked_fids = set()  # fitur yang benar-benar terkena intersection slice
    overlap_count = 0
    invalid_elevation_count = 0
    slice_count_h = 0
    slice_count_v = 0
    intersection_count = 0
    candidate_count = 0
    err_seq = 0

    def process_slice(direction, position, slice_geom, slice_source="GLOBAL"):
        check_cancelled()
        # Temporary diagnostic trace: retain every generated slice geometry.
        slice_diagnostic_records.append({
            "geometry": QgsGeometry(slice_geom),
            "direction": direction,
            "position": position,
            "source": slice_source
        })
        rect = slice_geom.boundingBox()
        pad = max(GEOMETRY_TOLERANCE, 0.000001)
        query_rect = QgsRectangle(rect)
        query_rect.grow(pad)
        fids = index.intersects(query_rect)
        crossings = []
        local_seen = set()

        for fid_index, fid in enumerate(fids):
            if fid_index % 100 == 0:
                check_cancelled()
            f = fetch_feature(fid)
            if f is None:
                continue
            elev = elevation_cache.get(fid)
            geom = f.geometry()
            if geom is None or geom.isEmpty():
                continue
            pts, has_overlap = get_intersection_points(geom, slice_geom)
            if has_overlap:
                overlap_count_local[0] += 1
            # Checked hanya jika benar-benar ada hasil intersection point/line.
            if pts or has_overlap:
                checked_fids.add(fid)
            if elev is None:
                if pts or has_overlap:
                    invalid_elevation_count_local[0] += 1
                continue
            for p in pts:
                key = (fid, round(p.x() / pad), round(p.y() / pad))
                if key in local_seen:
                    continue
                local_seen.add(key)
                event = {"fid": fid, "elev": elev, "point": p,
                         "sort": p.x() if direction == "H" else p.y(),
                         "direction": direction, "slice_pos": position,
                         "slice_source": slice_source,
                         "is_candidate": False, "partner_fid": None,
                         "delta": None, "err_type": ""}
                crossings.append(event)

        crossings.sort(key=lambda item: item["sort"])
        intersection_count_local[0] += len(crossings)

        # Simpan SEMUA titik perpotongan, termasuk yang nantinya bukan kandidat.
        # Status kandidat diperbarui ketika pasangan bersebelahan ditemukan.
        for event in crossings:
            all_intersections.append(event)

        for i in range(len(crossings) - 1):
            a = crossings[i]
            b = crossings[i + 1]
            if a["fid"] == b["fid"]:
                continue
            delta = abs(b["elev"] - a["elev"])
            ratio = delta / contour_interval
            nearest = int(round(ratio))
            is_multiple = abs(ratio - nearest) <= max(0.001, ELEVATION_TOLERANCE)
            if is_multiple and nearest >= 2:
                missing_count = nearest - 1
                missing_values = []
                step = contour_interval if b["elev"] > a["elev"] else -contour_interval
                val = a["elev"] + step
                for j in range(missing_count):
                    missing_values.append(val)
                    val += step
                err_type = "MISSING_LEVEL"
            elif REPORT_NON_MULTIPLE_INTERVAL and delta > contour_interval + ELEVATION_TOLERANCE:
                missing_count = None
                missing_values = []
                err_type = "INTERVAL_ANOMALY"
            else:
                continue

            a["is_candidate"] = True
            b["is_candidate"] = True
            a["partner_fid"] = b["fid"]
            b["partner_fid"] = a["fid"]
            a["delta"] = delta
            b["delta"] = delta
            a["err_type"] = err_type
            b["err_type"] = err_type

            pair_ids = tuple(sorted((a["fid"], b["fid"])))
            coord_key = (round(a["point"].x(), 3), round(a["point"].y(), 3),
                         round(b["point"].x(), 3), round(b["point"].y(), 3))
            candidate_key = (direction, round(position, 3), pair_ids, coord_key)
            if candidate_key in seen_candidate_keys:
                continue
            seen_candidate_keys.add(candidate_key)

            err_seq_local[0] += 1
            err_id = "ERR%06d" % err_seq_local[0]
            pair_id = "%s_%s" % (pair_ids[0], pair_ids[1])
            missing_text = ",".join([("%.3f" % v) for v in missing_values])

            for item in (a, b):
                source_feature = fetch_feature(item["fid"])
                source_geom = source_feature.geometry()
                point_geom = QgsGeometry.fromPointXY(item["point"])
                # Buffer radius is exactly equal to slice interval. Only the
                # contour that generated this intersection is clipped.
                buffer_geom = point_geom.buffer(slice_interval, 8)
                clipped = source_geom.intersection(buffer_geom)
                error_records.append({
                    "err_id": err_id, "src_id": item["fid"], "elev": item["elev"],
                    "pair_id": pair_id, "miss_elev": missing_text,
                    "miss_cnt": missing_count, "act_int": delta,
                    "slice_dir": direction, "slice_pos": position,
                    "err_type": err_type,
                    "clip_geom": QgsGeometry(clipped) if clipped is not None else QgsGeometry(),
                })
                if clipped is not None and not clipped.isEmpty():
                    marker_geoms_by_fid[item["fid"]].append(clipped)
                    marker_elev_by_fid[item["fid"]] = item["elev"]

            mid = QgsPointXY((a["point"].x() + b["point"].x()) / 2.0,
                             (a["point"].y() + b["point"].y()) / 2.0)
            missing_points.append({
                "err_id": err_id, "pair_id": pair_id, "miss_elev": missing_text,
                "miss_cnt": missing_count, "act_int": delta,
                "slice_dir": direction, "slice_pos": position,
                "err_type": err_type, "point": mid,
            })
            candidate_count_local[0] += 1

    # Mutable counters for Python 2.7 nested function compatibility.
    overlap_count_local = [0]
    intersection_count_local = [0]
    slice_diagnostic_records = []
    err_seq_local = [0]
    candidate_count_local = [0]
    invalid_elevation_count_local = [0]

    # Timing/progress helper: statistik dipisahkan per fase.
    phase_stats = {
        "GLOBAL": {"count": 0, "seconds": 0.0},
        "ENDPOINT": {"count": 0, "seconds": 0.0},
        "SMALL": {"count": 0, "seconds": 0.0},
    }

    aoi_clip_empty_count = [0]
    aoi_segment_count = [0]
    global_original_slice_count = [0]
    global_aoi_intersecting_slice_count = [0]
    global_extraction_diagnostics = {"errors": 0, "messages": []}

    def timed_process_slice(phase, direction, position, slice_geom, slice_source="GLOBAL"):
        check_cancelled()
        t0 = time.time()
        if phase == "GLOBAL":
            global_original_slice_count[0] += 1
        if phase == "GLOBAL" and aoi_area_geom is not None and not aoi_area_geom.isEmpty():
            # Clip the logical global slice to the AOI polygon. Each disconnected
            # in-AOI line segment is evaluated independently, so intersections
            # separated by an outside-AOI void can never become adjacent pairs.
            clipped_slice = aoi_area_geom.intersection(slice_geom)
            in_aoi_segments = []
            add_line_parts(clipped_slice, in_aoi_segments, global_extraction_diagnostics)
            if in_aoi_segments:
                global_aoi_intersecting_slice_count[0] += 1
            if not in_aoi_segments:
                aoi_clip_empty_count[0] += 1
            aoi_segment_count[0] += len(in_aoi_segments)
            for segment_index, segment_geom in enumerate(in_aoi_segments):
                check_cancelled()
                process_slice(direction, position, segment_geom,
                              "%s_AOI_SEGMENT_%d" % (slice_source, segment_index + 1))
        else:
            process_slice(direction, position, slice_geom, slice_source)
        check_cancelled()
        elapsed = time.time() - t0
        st = phase_stats[phase]
        st["count"] += 1
        st["seconds"] += elapsed
        remaining = max(0, st.get("total", st["count"]) - st["count"])
        avg = st["seconds"] / float(st["count"]) if st["count"] else 0.0
        eta = avg * remaining
        log("[%s] %s slice %d/%d | waktu %.2fs | rata-rata %.2fs/slice | estimasi sisa %s" %
            (phase, direction, st["count"], st.get("total", st["count"]), elapsed, avg,
             time.strftime("%H:%M:%S", time.gmtime(eta))))
        phase_total = max(1, st.get("total", st["count"]))
        phase_fraction = min(1.0, st["count"] / float(phase_total))
        # Every processing stage has its own 0–100% progress scale.
        pct = phase_fraction * 100.0
        remaining = max(0, st.get("total", st["count"]) - st["count"])
        eta_phase = avg * remaining
        stage_labels = {
            "GLOBAL": "Global Check",
            "ENDPOINT": "Local Endpoint Check",
            "SMALL": "Local Small-Feature Check",
        }
        report_progress(pct, "%s (%d/%d)" %
                        (stage_labels.get(phase, phase),
                         st["count"], st.get("total", st["count"])),
                        "Estimated time remaining: %s" %
                        time.strftime("%H:%M:%S", time.gmtime(eta_phase)),
                        phase, st["count"], st.get("total", st["count"]))

    # Horizontal slices: start one interval below Ymax, moving downward.
    y = ymax - slice_interval
    global_h_total = max(0, int(math.floor((y - ymin) / slice_interval)) + 1) if y >= ymin else 0
    x_start = xmin + slice_interval
    global_v_total = max(0, int(math.floor((xmax - x_start) / slice_interval)) + 1) if x_start <= xmax else 0
    phase_stats["GLOBAL"]["total"] = global_h_total + global_v_total
    report_progress(0, "Global Check (0/%d)" % phase_stats["GLOBAL"]["total"],
                    "Estimated time remaining: calculating…", "GLOBAL", 0,
                    phase_stats["GLOBAL"]["total"])
    while y >= ymin:
        line = QgsGeometry.fromPolylineXY([QgsPointXY(xmin, y), QgsPointXY(xmax, y)])
        timed_process_slice("GLOBAL", "H", y, line)
        slice_count_h += 1
        y -= slice_interval

    # Vertical slices: start one interval right of Xmin, moving rightward.
    x = xmin + slice_interval
    while x <= xmax:
        line = QgsGeometry.fromPolylineXY([QgsPointXY(x, ymin), QgsPointXY(x, ymax)])
        timed_process_slice("GLOBAL", "V", x, line)
        slice_count_v += 1
        x += slice_interval

    # Show a compact diagnostic summary directly in QGIS before local checks.
    global_point_count = len([ev for ev in all_intersections
                              if ev.get("slice_source", "GLOBAL").startswith("GLOBAL")])
    global_diag_text = (
        "Global Check diagnostic\n\n"
        "Original global slices: %d\n" % global_original_slice_count[0] +
        "Slices intersecting AOI: %d\n" % global_aoi_intersecting_slice_count[0] +
        "Extracted in-AOI slice segments: %d\n" % aoi_segment_count[0] +
        "Empty/outside-AOI slices: %d\n" % aoi_clip_empty_count[0] +
        "Contour intersection points found: %d\n" % global_point_count +
        "Geometry extraction exceptions: %d" % global_extraction_diagnostics["errors"]
    )
    if global_extraction_diagnostics["messages"]:
        global_diag_text += "\n\nFirst extraction message: " + global_extraction_diagnostics["messages"][0]
    log(global_diag_text.replace("\n", " | "))

    # ---------------------------------------------------------------------
    # Local endpoint checks. If no AOI is supplied, use a
    # convex-hull boundary only to identify and skip outer-edge endpoints.
    # ---------------------------------------------------------------------
    log("Membangun indeks endpoint berdasarkan grid interval slice...")
    endpoint_grid = defaultdict(list)
    endpoint_records = []
    boundary_endpoint_records = []
    coincident_endpoint_records = []
    endpoint_seq = 0
    endpoint_boundary_tolerance = 0.50  # metre (default, user-adjustable)
    endpoint_boundary = aoi_boundary
    convex_hull_line_geom = None
    boundary_mode = "AOI"

    if endpoint_boundary is None:
        boundary_mode = "CONVEX_HULL"
        log("AOI tidak diberikan; membangun convex hull untuk penyaringan endpoint tepi...")
        hull_vertices = []
        for hf_index, hf in enumerate(layer.getFeatures()):
            if hf_index % 100 == 0:
                check_cancelled()
            hg = hf.geometry()
            if hg is None or hg.isEmpty():
                continue
            # Convex-hull construction only needs vertices; converting every
            # multipart feature to nested Python lists via asMultiPolyline()
            # is memory-intensive and can crash QGIS on very large datasets.
            # QgsGeometry.vertices() streams vertices without materializing
            # every line part as a large Python list.
            for hp in hg.vertices():
                hull_vertices.append(QgsPointXY(hp))
        if len(hull_vertices) >= 3:
            hull_geom = QgsGeometry.fromMultiPointXY(hull_vertices).convexHull()
            if hull_geom is not None and not hull_geom.isEmpty():
                try:
                    hull_polys = hull_geom.asMultiPolygon() if hull_geom.isMultipart() else [hull_geom.asPolygon()]
                    hull_lines = []
                    for poly in hull_polys:
                        if poly and poly[0] and len(poly[0]) >= 2:
                            hull_lines.append([QgsPointXY(pt) for pt in poly[0]])
                    if hull_lines:
                        # Keep the exact hull outline as a line geometry for both
                        # endpoint-distance checks and a diagnostic shapefile.
                        if len(hull_lines) == 1:
                            convex_hull_line_geom = QgsGeometry.fromPolylineXY(hull_lines[0])
                        else:
                            convex_hull_line_geom = QgsGeometry.fromMultiPolylineXY(hull_lines)
                        endpoint_boundary = QgsGeometry(convex_hull_line_geom)
                except Exception as hull_ex:
                    log("Peringatan: ekstraksi batas convex hull gagal: %s" % hull_ex)
        if endpoint_boundary is None or endpoint_boundary.isEmpty():
            log("Peringatan: convex hull tidak terbentuk; seluruh endpoint akan diproses.")
        else:
            log("Batas convex hull terbentuk. Toleransi endpoint tepi: %.3f m" % endpoint_boundary_tolerance)
    else:
        log("Menggunakan batas AOI. Toleransi endpoint tepi: %.3f m" % endpoint_boundary_tolerance)

    # Collect all part endpoints first. Exact coordinate duplicates are
    # excluded from endpoint-driven processing, including endpoints belonging
    # to the same feature (e.g. closed contour first/last vertices).
    raw_endpoint_records = []
    endpoint_coordinate_counts = defaultdict(int)

    for feature_index, feature in enumerate(layer.getFeatures()):
        if feature_index % 100 == 0:
            check_cancelled()
        fid = feature.id()
        geom = feature.geometry()
        for part_no, line_part in enumerate(get_line_parts(geom)):
            if len(line_part) < 2:
                continue
            for end_no, (end_point, inner_point) in enumerate(((line_part[0], line_part[1]),
                                                               (line_part[-1], line_part[-2]))):
                p = QgsPointXY(end_point)
                coord_key = (float(p.x()), float(p.y()))
                endpoint_coordinate_counts[coord_key] += 1

                boundary_distance = -1.0
                is_boundary_endpoint = False
                if endpoint_boundary is not None:
                    pgeom = QgsGeometry.fromPointXY(p)
                    try:
                        boundary_distance = pgeom.distance(endpoint_boundary)
                        is_boundary_endpoint = boundary_distance <= endpoint_boundary_tolerance
                    except Exception as boundary_ex:
                        log("Peringatan pemeriksaan batas endpoint: %s" % boundary_ex)

                raw_endpoint_records.append({
                    "fid": fid, "point": p, "inner": QgsPointXY(inner_point),
                    "part": part_no, "end_no": end_no + 1,
                    "distance": boundary_distance, "coord_key": coord_key,
                    "is_boundary": is_boundary_endpoint
                })

    # Global Check has finished. Let the user review and adjust the boundary
    # tolerance before endpoint classification and Local Endpoint Check.
    endpoint_boundary_tolerance = ask_endpoint_boundary_tolerance(
        raw_endpoint_records, endpoint_coordinate_counts,
        endpoint_boundary is not None, endpoint_boundary_tolerance
    )
    log("Toleransi endpoint boundary dipilih pengguna: %.3f m" %
        endpoint_boundary_tolerance)

    for raw_rec in raw_endpoint_records:
        # Exact coordinate equality is intentional; no tolerance is applied.
        if endpoint_coordinate_counts[raw_rec["coord_key"]] > 1:
            coincident_endpoint_records.append(raw_rec)
            continue

        if (endpoint_boundary is not None and raw_rec["distance"] >= 0.0 and
                raw_rec["distance"] <= endpoint_boundary_tolerance):
            boundary_endpoint_records.append(raw_rec)
            continue

        p = raw_rec["point"]
        cell_x = int(math.floor((p.x() - xmin) / slice_interval))
        cell_y = int(math.floor((p.y() - ymin) / slice_interval))
        rec = {"eid": endpoint_seq, "fid": raw_rec["fid"], "point": p,
               "inner": raw_rec["inner"], "part": raw_rec["part"],
               "end_no": raw_rec["end_no"] - 1, "cell": (cell_x, cell_y),
               "distance": raw_rec["distance"]}
        endpoint_records.append(rec)
        endpoint_grid[(cell_x, cell_y)].append(rec)
        endpoint_seq += 1

    log("Mode penyaringan endpoint: %s | Toleransi batas: %.2f m" %
        (boundary_mode, endpoint_boundary_tolerance))
    log("Endpoint berkoordinat sama dikecualikan: %d | Endpoint tepi dikecualikan: %d | Endpoint diproses: %d | Sel grid: %d" %
        (len(coincident_endpoint_records), len(boundary_endpoint_records),
         len(endpoint_records), len(endpoint_grid)))

    endpoint_eps = max(0.0001, slice_interval / 100.0)
    extrapolate_dist = max(0.0001, slice_interval / 200.0)
    endpoint_pairs_seen = set()
    supplementary_points = []

    # Search the current grid cell and its 8 neighbours. This avoids an
    # all-pairs comparison over the complete contour dataset.
    for endpoint_index, ep in enumerate(endpoint_records):
        if endpoint_index % 100 == 0:
            check_cancelled()
        cx, cy = ep["cell"]
        nearby = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                nearby.extend(endpoint_grid.get((cx + dx, cy + dy), []))
        matches = []
        for other in nearby:
            if other["eid"] == ep["eid"] or other["fid"] == ep["fid"]:
                continue
            dx = other["point"].x() - ep["point"].x()
            dy = other["point"].y() - ep["point"].y()
            dist = math.sqrt(dx * dx + dy * dy)
            if dist <= endpoint_eps:
                matches.append(other)

        if matches:
            for other in matches:
                pair_key = tuple(sorted((ep["eid"], other["eid"])))
                if pair_key in endpoint_pairs_seen:
                    continue
                endpoint_pairs_seen.add(pair_key)
                mid = QgsPointXY((ep["point"].x() + other["point"].x()) / 2.0,
                                 (ep["point"].y() + other["point"].y()) / 2.0)
                supplementary_points.append((mid, "EP_MIDPOINT", set([ep["fid"], other["fid"]])))
        else:
            vx = ep["point"].x() - ep["inner"].x()
            vy = ep["point"].y() - ep["inner"].y()
            length = math.sqrt(vx * vx + vy * vy)
            if length <= 0:
                continue
            out_pt = QgsPointXY(ep["point"].x() + (vx / length) * extrapolate_dist,
                                ep["point"].y() + (vy / length) * extrapolate_dist)
            supplementary_points.append((out_pt, "EP_EXTEND", set([ep["fid"]])))

    def adaptive_local_slice(direction, cx, cy, source_fids, slice_source):
        """Build one local slice, extending each ray by one interval until it
        meets a contour whose feature ID differs from the source contour IDs.
        The final complete line is evaluated once by process_slice().
        """
        source_fids = set(source_fids or [])
        half_initial = float(slice_interval)
        step = float(slice_interval)
        if step <= 0:
            raise Exception("Slice interval harus lebih besar dari nol.")

        def point_at(distance):
            if direction == "H":
                return QgsPointXY(cx + distance, cy)
            return QgsPointXY(cx, cy + distance)

        def max_distance(sign):
            # The layer extent is the hard safety limit. If an AOI boundary is
            # available, use its first intersection along this ray instead.
            if direction == "H":
                limit = (xmax - cx) if sign > 0 else (cx - xmin)
            else:
                limit = (ymax - cy) if sign > 0 else (cy - ymin)
            limit = max(0.0, limit)
            if aoi_boundary is not None and not aoi_boundary.isEmpty():
                ray_end = point_at(sign * limit)
                ray = QgsGeometry.fromPolylineXY([
                    QgsPointXY(cx, cy), ray_end
                ])
                try:
                    boundary_hits, _ = get_intersection_points(aoi_boundary, ray)
                    distances = []
                    for bp in boundary_hits:
                        d = ((bp.x() - cx) if direction == "H" else
                             (bp.y() - cy)) * sign
                        if d >= -GEOMETRY_TOLERANCE:
                            distances.append(max(0.0, d))
                    if distances:
                        limit = min(distances)
                except Exception:
                    pass
            return limit

        def ray_has_different_contour(sign, distance):
            end_pt = point_at(sign * distance)
            ray = QgsGeometry.fromPolylineXY([
                QgsPointXY(cx, cy), end_pt
            ])
            rect = ray.boundingBox()
            rect.grow(max(GEOMETRY_TOLERANCE, 0.000001))
            for fid in index.intersects(rect):
                if fid in source_fids:
                    continue
                f = fetch_feature(fid)
                if f is None:
                    continue
                geom = f.geometry()
                if geom is None or geom.isEmpty():
                    continue
                pts, has_overlap = get_intersection_points(geom, ray)
                if pts or has_overlap:
                    return True
            return False

        limits = {1: max_distance(1), -1: max_distance(-1)}
        reaches = {}
        for sign in (1, -1):
            distance = min(half_initial, limits[sign])
            # Test the initial reach, then extend by exactly one interval.
            found = ray_has_different_contour(sign, distance) if distance > 0 else False
            while not found and distance < limits[sign]:
                check_cancelled()
                distance = min(distance + step, limits[sign])
                found = ray_has_different_contour(sign, distance)
            reaches[sign] = distance

        start_pt = point_at(-reaches[-1])
        end_pt = point_at(reaches[1])
        return QgsGeometry.fromPolylineXY([start_pt, end_pt])

    # Local endpoint checks: each derived endpoint location gets two short
    # slices, one horizontal and one vertical, each exactly 2 * interval long.
    # This avoids rescanning the full dataset extent for every endpoint.
    endpoint_slice_jobs = []
    for point, source_type, source_fids in supplementary_points:
        endpoint_slice_jobs.append(("H", point.x(), point.y(), source_type, source_fids))
        endpoint_slice_jobs.append(("V", point.x(), point.y(), source_type, source_fids))

    phase_stats["ENDPOINT"]["total"] = len(endpoint_slice_jobs)
    report_progress(0, "Local Endpoint Check (0/%d)" % len(endpoint_slice_jobs),
                    "Estimated time remaining: calculating…", "ENDPOINT", 0,
                    len(endpoint_slice_jobs))
    for direction, cx, cy, source_type, source_fids in endpoint_slice_jobs:
        local_line = adaptive_local_slice(
            direction, cx, cy, source_fids, source_type)
        timed_process_slice("ENDPOINT", direction,
                            cy if direction == "H" else cx,
                            local_line, source_type)

    if not endpoint_slice_jobs:
        report_progress(100, "Local Endpoint Check (0/0)",
                        "No endpoint locations required local checking.",
                        "ENDPOINT", 0, 0)
    log("Titik turunan endpoint: %d | local endpoint slices: %d" %
        (len(supplementary_points), len(endpoint_slice_jobs)))

    # ---------------------------------------------------------------------
    # Local small-feature check: contours not yet intersected by global or endpoint checks.
    # For each unchecked contour, create local H/V slices through its bbox centre.
    # ---------------------------------------------------------------------
    unchecked_fids = [f.id() for f in layer.getFeatures() if f.id() not in checked_fids]
    log("Final Contour Check: %d kontur masih unchecked." % len(unchecked_fids))
    small_slice_jobs = []
    for unchecked_index, fid in enumerate(unchecked_fids):
        if unchecked_index % 100 == 0:
            check_cancelled()
        f = fetch_feature(fid)
        if f is None:
            continue
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        bb = g.boundingBox()
        cx = (bb.xMinimum() + bb.xMaximum()) / 2.0
        cy = (bb.yMinimum() + bb.yMaximum()) / 2.0
        # One local H/V pair per unchecked feature, centered on its bbox.
        # Each segment is exactly 2 * slice_interval long.
        small_slice_jobs.append(("H", cx, cy, fid))
        small_slice_jobs.append(("V", cx, cy, fid))

    phase_stats["SMALL"]["total"] = len(small_slice_jobs)
    report_progress(0, "Local Small-Feature Check (0/%d)" % len(small_slice_jobs),
                    "Estimated time remaining: calculating…", "SMALL", 0,
                    len(small_slice_jobs))
    for direction, cx, cy, source_fid in small_slice_jobs:
        sline = adaptive_local_slice(
            direction, cx, cy, set([source_fid]), "SMALL_BBOX")
        pos = cy if direction == "H" else cx
        timed_process_slice("SMALL", direction, pos, sline, "SMALL_BBOX")

    if not small_slice_jobs:
        report_progress(100, "Local Small-Feature Check (0/0)",
                        "No unchecked small features required local checking.",
                        "SMALL", 0, 0)

    checked_after_final = len(checked_fids)
    still_unchecked = max(0, layer.featureCount() - checked_after_final)
    log("Local small-feature check selesai | checked: %d | masih unchecked: %d" %
        (checked_after_final, still_unchecked))

    candidate_count = candidate_count_local[0]
    intersection_count = intersection_count_local[0]
    overlap_count = overlap_count_local[0]

    log("AOI clipping diagnostic | original global slices: %d | intersecting slices: %d | empty slices: %d | extracted in-AOI segments: %d | extraction exceptions: %d" %
        (global_original_slice_count[0], global_aoi_intersecting_slice_count[0],
         aoi_clip_empty_count[0], aoi_segment_count[0],
         global_extraction_diagnostics["errors"]))
    log("Slice horizontal: %d | vertikal: %d" % (slice_count_h, slice_count_v))
    log("Jumlah perpotongan: %d | Kandidat pasangan: %d" % (intersection_count, candidate_count))
    log("Slice yang berimpit/overlap dengan kontur (perlu perhatian): %d" % overlap_count)

    # -------------------------------------------------------------------------
    check_cancelled()
    report_progress(-1, "Preparing output features and writing the result file…",
                    "The output writer does not expose a live percentage or ETA.",
                    "WRITING", None, None)
    # Build output memory layers
    # -------------------------------------------------------------------------
    crs = layer.crs()
    # Analysis is performed in the working CRS (usually UTM), but final
    # deliverables must use exactly the input layer CRS.
    output_crs = source_crs
    if not output_crs.isValid() or not output_crs.authid():
        raise Exception("Output CRS could not be resolved. Expected a valid CRS such as EPSG:4326.")
    output_transform = None
    if crs != output_crs:
        output_transform = QgsCoordinateTransform(
            crs, output_crs, QgsProject.instance()
        )

    # Diagnostic outputs: endpoint classification made by AOI/convex-hull boundary.
    # DIST_M stores the measured distance for both excluded and processed endpoints.
    endpoint_diag_fields = QgsFields()
    endpoint_diag_fields.append(QgsField("SRC_ID", QVariant.LongLong))
    endpoint_diag_fields.append(QgsField("PART_NO", QVariant.Int))
    endpoint_diag_fields.append(QgsField("END_NO", QVariant.Int))
    endpoint_diag_fields.append(QgsField("DIST_M", QVariant.Double, len=20, prec=4))
    endpoint_diag_fields.append(QgsField("CLASS", QVariant.String, len=16))
    boundary_endpoints_layer = create_memory_layer("Point", crs, endpoint_diag_fields)
    processed_endpoints_layer = create_memory_layer("Point", crs, endpoint_diag_fields)
    coincident_endpoints_layer = create_memory_layer("Point", crs, endpoint_diag_fields)

    coincident_diag_features = []
    for rec in coincident_endpoint_records:
        coincident_diag_features.append(make_point_feature(
            coincident_endpoints_layer, rec["point"],
            [rec["fid"], rec["part"] + 1, rec["end_no"], rec["distance"], "COINCIDENT"]))
    coincident_endpoints_layer.dataProvider().addFeatures(coincident_diag_features)
    coincident_endpoints_layer.updateExtents()

    boundary_diag_features = []
    for rec in boundary_endpoint_records:
        boundary_diag_features.append(make_point_feature(
            boundary_endpoints_layer, rec["point"],
            [rec["fid"], rec["part"] + 1, rec["end_no"], rec["distance"], "BOUNDARY"]))
    boundary_endpoints_layer.dataProvider().addFeatures(boundary_diag_features)
    boundary_endpoints_layer.updateExtents()

    processed_diag_features = []
    for rec in endpoint_records:
        processed_diag_features.append(make_point_feature(
            processed_endpoints_layer, rec["point"],
            [rec["fid"], rec["part"] + 1, rec["end_no"] + 1, rec["distance"], "PROCESSED"]))
    processed_endpoints_layer.dataProvider().addFeatures(processed_diag_features)
    processed_endpoints_layer.updateExtents()

    # Convex-hull outline diagnostic. It is created only when the script used
    # convex hull (no AOI); AOI boundaries are not mislabeled as convex hulls.
    convex_hull_layer = None
    if boundary_mode == "CONVEX_HULL" and convex_hull_line_geom is not None and not convex_hull_line_geom.isEmpty():
        convex_hull_layer = create_memory_layer("MultiLineString", crs, QgsFields())
        hull_feature = QgsFeature(convex_hull_layer.fields())
        hull_out_geom = QgsGeometry(convex_hull_line_geom)
        if not hull_out_geom.isMultipart():
            hull_out_geom.convertToMultiType()
        hull_feature.setGeometry(hull_out_geom)
        convex_hull_layer.dataProvider().addFeatures([hull_feature])
        convex_hull_layer.updateExtents()

    err_fields = QgsFields()
    err_fields.append(QgsField("ERR_ID", QVariant.String, len=12))
    err_fields.append(QgsField("SRC_ID", QVariant.LongLong))
    err_fields.append(QgsField("ELEV", QVariant.Double, len=20, prec=6))
    err_fields.append(QgsField("PAIR_ID", QVariant.String, len=30))
    err_fields.append(QgsField("MISS_ELEV", QVariant.String, len=80))
    err_fields.append(QgsField("MISS_CNT", QVariant.Int))
    err_fields.append(QgsField("ACT_INT", QVariant.Double, len=20, prec=6))
    err_fields.append(QgsField("SLICE_DIR", QVariant.String, len=1))
    err_fields.append(QgsField("SLICE_POS", QVariant.Double, len=20, prec=3))
    err_fields.append(QgsField("ERR_TYPE", QVariant.String, len=24))
    errors_layer = create_memory_layer("MultiLineString", crs, err_fields)

    marker_fields = QgsFields()
    marker_fields.append(QgsField("SRC_ID", QVariant.LongLong))
    marker_fields.append(QgsField("ELEV", QVariant.Double, len=20, prec=6))
    marker_fields.append(QgsField("N_PARTS", QVariant.Int))
    marker_layer = create_memory_layer("MultiLineString", crs, marker_fields)

    # Reconstructed missing-contour lines. Estimates are grouped by source
    # contour pair, missing elevation and slice direction, then ordered along
    # the axis perpendicular to the slice direction.
    missing_fields = QgsFields()
    missing_fields.append(QgsField("PAIR_ID", QVariant.String, len=30))
    missing_fields.append(QgsField("MISS_ELEV", QVariant.Double, len=20, prec=6))
    missing_fields.append(QgsField("N_POINTS", QVariant.Int))
    missing_fields.append(QgsField("SLICE_DIR", QVariant.String, len=1))
    missing_fields.append(QgsField("ERR_TYPE", QVariant.String, len=24))
    missing_layer = create_memory_layer("MultiLineString", crs, missing_fields)

    # Diagnostic layer: every contour/slice intersection, candidate or not.
    ix_fields = QgsFields()
    ix_fields.append(QgsField("SRC_ID", QVariant.LongLong))
    ix_fields.append(QgsField("ELEV", QVariant.Double, len=20, prec=6))
    ix_fields.append(QgsField("DIRECTION", QVariant.String, len=1))
    ix_fields.append(QgsField("SLICE_SRC", QVariant.String, len=64))
    ix_fields.append(QgsField("SLICE_POS", QVariant.Double, len=20, prec=3))
    ix_fields.append(QgsField("IS_CAND", QVariant.Int))
    ix_fields.append(QgsField("PARTNER", QVariant.LongLong))
    ix_fields.append(QgsField("DELTA_E", QVariant.Double, len=20, prec=6))
    ix_fields.append(QgsField("ERR_TYPE", QVariant.String, len=24))
    intersections_layer = create_memory_layer("Point", crs, ix_fields)

    # Temporary diagnostic layer: all generated horizontal/vertical slices.
    slice_fields = QgsFields()
    slice_fields.append(QgsField("DIRECTION", QVariant.String, len=1))
    slice_fields.append(QgsField("SLICE_SRC", QVariant.String, len=64))
    slice_fields.append(QgsField("SLICE_POS", QVariant.Double, len=20, prec=3))
    slices_layer = create_memory_layer("LineString", crs, slice_fields)

    # Add original contour geometries: one row for each error pair member.
    error_features = []
    for rec in error_records:
        src = fetch_feature(rec["src_id"])
        if src is None:
            continue
        attrs = [rec["err_id"], rec["src_id"], rec["elev"], rec["pair_id"],
                 rec["miss_elev"], rec["miss_cnt"], rec["act_int"],
                 rec["slice_dir"], rec["slice_pos"], rec["err_type"]]
        clip_geom = rec.get("clip_geom")
        if clip_geom is None or clip_geom.isEmpty():
            continue
        error_features.append(make_line_feature(errors_layer, clip_geom, attrs))
    errors_layer.dataProvider().addFeatures(error_features)
    errors_layer.updateExtents()

    # Merge all clipped pieces by source feature ID. They may remain multipart;
    # no overlap/touch test is performed.
    marker_features = []
    for fid, geoms in marker_geoms_by_fid.items():
        nonempty = [g for g in geoms if g is not None and not g.isEmpty()]
        if not nonempty:
            continue
        # Collect every extracted piece into one MultiLineString feature for
        # this source ID. No overlap/touch test is performed.
        line_parts = []
        for part_geom in nonempty:
            extract_line_parts(part_geom, line_parts)
        if not line_parts:
            continue
        merged = QgsGeometry.fromMultiPolylineXY(line_parts)
        attrs = [fid, marker_elev_by_fid.get(fid), len(line_parts)]
        marker_features.append(make_line_feature(marker_layer, merged, attrs))
    marker_layer.dataProvider().addFeatures(marker_features)
    marker_layer.updateExtents()

    missing_line_groups = defaultdict(list)
    for rec in missing_points:
        # Only definite missing levels can be reconstructed as contour lines.
        if rec["err_type"] != "MISSING_LEVEL" or not rec["miss_elev"]:
            continue
        for elev_text in rec["miss_elev"].split(","):
            try:
                missing_elev = float(elev_text)
            except Exception:
                continue
            # Combine H and V slice estimates; both contribute to the same
            # reconstructed contour for a given source pair and elevation.
            group_key = (rec["pair_id"], missing_elev)
            missing_line_groups[group_key].append(rec["point"])

    missing_features = []
    for group_key in sorted(missing_line_groups.keys()):
        pair_id, missing_elev = group_key
        src_ids = [int(v) for v in pair_id.split("_")]
        src_a = fetch_feature(src_ids[0])
        src_b = fetch_feature(src_ids[1])
        if src_a is None or src_b is None:
            continue
        geom_a = src_a.geometry()
        geom_b = src_b.geometry()

        # Remove duplicate estimates, then build short connected runs.
        unique = {}
        for point in missing_line_groups[group_key]:
            key = (round(point.x(), 3), round(point.y(), 3))
            unique[key] = QgsPointXY(point)
        remaining = list(unique.values())
        if len(remaining) < 2:
            continue

        # Greedy nearest-neighbour chaining avoids joining distant, disconnected
        # occurrences of the same source-feature pair.
        max_link = max(slice_interval * 1.75, 1.0)
        runs = []
        while remaining:
            chain = [remaining.pop(0)]
            while remaining:
                last = chain[-1]
                nearest_idx = None
                nearest_dist = None
                for idx, candidate in enumerate(remaining):
                    dx = candidate.x() - last.x()
                    dy = candidate.y() - last.y()
                    dist = math.sqrt(dx * dx + dy * dy)
                    if nearest_dist is None or dist < nearest_dist:
                        nearest_dist = dist
                        nearest_idx = idx
                if nearest_dist is None or nearest_dist > max_link:
                    break
                chain.append(remaining.pop(nearest_idx))
            if len(chain) >= 2:
                runs.append(chain)

        for chain in runs:
            # Keep only adjacent links whose intermediate points remain in
            # the corridor between the two source contours. At sampled points
            # the distances to both source contours should be comparable.
            valid_parts = []
            current_part = [chain[0]]

            for idx in range(len(chain) - 1):
                p1 = chain[idx]
                p2 = chain[idx + 1]
                link_ok = True
                for sample_no in range(1, 6):
                    t = sample_no / 6.0
                    sample = QgsPointXY(
                        p1.x() + (p2.x() - p1.x()) * t,
                        p1.y() + (p2.y() - p1.y()) * t)
                    sample_geom = QgsGeometry.fromPointXY(sample)
                    try:
                        da = sample_geom.distance(geom_a)
                        db = sample_geom.distance(geom_b)
                    except Exception:
                        link_ok = False
                        break
                    # Midpoints of opposing contour crossings should remain
                    # between, and at broadly similar distance from, both
                    # source contour geometries.
                    balance_tol = max(0.50, 0.30 * (da + db))
                    if abs(da - db) > balance_tol:
                        link_ok = False
                        break

                if link_ok:
                    current_part.append(p2)
                else:
                    if len(current_part) >= 2:
                        valid_parts.append(current_part)
                    current_part = [p2]

            if len(current_part) >= 2:
                valid_parts.append(current_part)

            for part in valid_parts:
                line_geom = QgsGeometry.fromPolylineXY(part)
                attrs = [pair_id, missing_elev, len(part), "M", "MISSING_LEVEL"]
                missing_features.append(make_line_feature(missing_layer, line_geom, attrs))

    missing_layer.dataProvider().addFeatures(missing_features)
    missing_layer.updateExtents()

    # Stage-by-stage diagnostics for temporary memory-layer construction.
    # These counters deliberately distinguish source records, QgsFeature
    # objects prepared, provider response, and final provider featureCount.
    materialization_stats = {}

    intersection_features = []
    intersection_feature_build_errors = 0
    intersection_feature_build_messages = []
    for ev in all_intersections:
        try:
            attrs = [ev["fid"], ev["elev"], ev["direction"], ev.get("slice_source", "GLOBAL"),
                     ev["slice_pos"], 1 if ev["is_candidate"] else 0, ev["partner_fid"],
                     ev["delta"], ev["err_type"]]
            intersection_features.append(make_point_feature(intersections_layer, ev["point"], attrs))
        except Exception as exc:
            intersection_feature_build_errors += 1
            if len(intersection_feature_build_messages) < 3:
                intersection_feature_build_messages.append(str(exc))
    intersection_add_result = intersections_layer.dataProvider().addFeatures(intersection_features)
    intersections_layer.updateExtents()
    if isinstance(intersection_add_result, tuple):
        intersection_add_ok = bool(intersection_add_result[0])
        intersection_added_count = len(intersection_add_result[1]) if len(intersection_add_result) > 1 and intersection_add_result[1] is not None else -1
    else:
        intersection_add_ok = bool(intersection_add_result)
        intersection_added_count = -1
    materialization_stats["intersections"] = {
        "source": len(all_intersections),
        "prepared": len(intersection_features),
        "build_errors": intersection_feature_build_errors,
        "add_ok": intersection_add_ok,
        "provider_added": intersection_added_count,
        "stored": intersections_layer.featureCount(),
        "messages": intersection_feature_build_messages
    }

    slice_features = []
    slice_feature_build_errors = 0
    slice_feature_build_messages = []
    for rec in slice_diagnostic_records:
        try:
            attrs = [rec["direction"], rec["source"], rec["position"]]
            slice_features.append(make_line_feature(
                slices_layer, rec["geometry"], attrs))
        except Exception as exc:
            slice_feature_build_errors += 1
            if len(slice_feature_build_messages) < 3:
                slice_feature_build_messages.append(str(exc))
    slice_add_result = slices_layer.dataProvider().addFeatures(slice_features)
    slices_layer.updateExtents()
    if isinstance(slice_add_result, tuple):
        slice_add_ok = bool(slice_add_result[0])
        slice_added_count = len(slice_add_result[1]) if len(slice_add_result) > 1 and slice_add_result[1] is not None else -1
    else:
        slice_add_ok = bool(slice_add_result)
        slice_added_count = -1
    materialization_stats["slices"] = {
        "source": len(slice_diagnostic_records),
        "prepared": len(slice_features),
        "build_errors": slice_feature_build_errors,
        "add_ok": slice_add_ok,
        "provider_added": slice_added_count,
        "stored": slices_layer.featureCount(),
        "messages": slice_feature_build_messages
    }

    def provider_diagnostic(layer, features, add_result):
        """Collect provider/layer/geometry details without changing analysis output."""
        provider = layer.dataProvider()
        result_ok = False
        added_count = -1
        if isinstance(add_result, tuple):
            result_ok = bool(add_result[0])
            if len(add_result) > 1 and add_result[1] is not None:
                try:
                    added_count = len(add_result[1])
                except Exception:
                    added_count = -1
        else:
            result_ok = bool(add_result)
        details = {
            "layer_valid": bool(layer.isValid()),
            "provider_valid": bool(provider and provider.isValid()),
            "layer_wkb": QgsWkbTypes.displayString(layer.wkbType()),
            "provider_wkb": QgsWkbTypes.displayString(provider.wkbType()) if provider else "unavailable",
            "field_count": layer.fields().count(),
            "first_feature_wkb": "none",
            "first_feature_geometry_valid": "n/a",
            "first_feature_attribute_count": "n/a",
            "last_error": "",
            "provider_errors": [],
            "single_feature_test": "not run"
        }
        if features:
            first = features[0]
            details["first_feature_attribute_count"] = len(first.attributes())
            geom = first.geometry()
            if geom and not geom.isNull() and not geom.isEmpty():
                details["first_feature_wkb"] = QgsWkbTypes.displayString(geom.wkbType())
                details["first_feature_geometry_valid"] = str(geom.isGeosValid())
        try:
            details["last_error"] = str(provider.lastError()) if provider else "provider unavailable"
        except Exception as exc:
            details["last_error"] = "lastError unavailable: %s" % exc
        try:
            details["provider_errors"] = [str(e) for e in provider.errors()]
        except Exception:
            pass
        # Isolate the first feature in a scratch layer with the same declared
        # geometry type and fields. This does not alter the production layer.
        if features:
            try:
                scratch = QgsVectorLayer(
                    layer.source(), "ccc_materialization_test", "memory")
                scratch_provider = scratch.dataProvider()
                scratch_provider.addAttributes(layer.fields())
                scratch.updateFields()
                test_result = scratch_provider.addFeatures([QgsFeature(features[0])])
                if isinstance(test_result, tuple):
                    test_ok = bool(test_result[0])
                    test_added = len(test_result[1]) if len(test_result) > 1 and test_result[1] is not None else -1
                else:
                    test_ok = bool(test_result)
                    test_added = -1
                details["single_feature_test"] = (
                    "success=%s; provider_added=%s; featureCount=%s; lastError=%s" %
                    (test_ok, test_added, scratch.featureCount(), scratch_provider.lastError()))
            except Exception as exc:
                details["single_feature_test"] = "exception: %s" % exc
        details["add_ok"] = result_ok
        details["provider_added"] = added_count
        return details

    # Add detailed provider-level diagnostics after the batch insertion.
    # The test layer is isolated and does not modify the actual output layers.
    slice_provider_details = provider_diagnostic(slices_layer, slice_features, slice_add_result)
    intersection_provider_details = provider_diagnostic(
        intersections_layer, intersection_features, intersection_add_result)

    def materialization_report(label, stats, details):
        provider_added = str(stats["provider_added"]) if stats["provider_added"] >= 0 else "not reported by provider"
        lines = [
            label,
            "Source records: %d" % stats["source"],
            "QgsFeatures prepared: %d" % stats["prepared"],
            "Feature construction errors: %d" % stats["build_errors"],
            "Provider addFeatures success: %s" % str(stats["add_ok"]),
            "Provider-reported added features: %s" % provider_added,
            "Memory layer featureCount: %d" % stats["stored"],
            "Layer valid: %s" % details["layer_valid"],
            "Provider valid: %s" % details["provider_valid"],
            "Layer WKB type: %s" % details["layer_wkb"],
            "Provider WKB type: %s" % details["provider_wkb"],
            "First feature WKB type: %s" % details["first_feature_wkb"],
            "First feature geometry valid: %s" % details["first_feature_geometry_valid"],
            "Layer field count: %s" % details["field_count"],
            "First feature attribute count: %s" % details["first_feature_attribute_count"],
            "Single-feature scratch test: %s" % details["single_feature_test"],
            "Provider lastError: %s" % details["last_error"]
        ]
        if details["provider_errors"]:
            lines.append("Provider errors: " + " | ".join(details["provider_errors"][:3]))
        if stats["messages"]:
            lines.append("First construction error: " + stats["messages"][0])
        return "\n".join(lines)

    materialization_text = (
        "Diagnostic Layer Materialization\n\n" +
        materialization_report("SLICE LINES", materialization_stats["slices"], slice_provider_details) +
        "\n\n" +
        materialization_report("INTERSECTION POINTS", materialization_stats["intersections"], intersection_provider_details)
    )
    log(materialization_text.replace("\n", " | "))


    check_cancelled()
    report_progress(50, "Writing output",
                    "Writing output dataset…")
    # -------------------------------------------------------------------------
    # Unified output: marker and estimated missing lines are both polylines.
    # One Shapefile can therefore store both classes in one feature class.
    # -------------------------------------------------------------------------
    unified_fields = QgsFields()
    unified_fields.append(QgsField("LINE_TYPE", QVariant.String, len=32))
    unified_fields.append(QgsField("ERR_ID", QVariant.String, len=12))
    unified_fields.append(QgsField("ERR_IDS", QVariant.String, len=120))
    unified_fields.append(QgsField("ELEVATION", QVariant.Double, len=20, prec=6))
    unified_fields.append(QgsField("MISS_ELEV", QVariant.String, len=80))
    unified_fields.append(QgsField("SRC_ID", QVariant.LongLong))
    unified_fields.append(QgsField("PAIR_ID", QVariant.String, len=30))
    unified_layer = create_memory_layer("MultiLineString", output_crs, unified_fields)

    unified_features = []
    # Keep each marker linked to the specific error event, rather than merging
    # all marker pieces by source feature ID (which would lose ERR_ID lineage).
    for rec in error_records:
        geom = rec.get("clip_geom")
        if geom is None or geom.isEmpty():
            continue
        geom = QgsGeometry(geom)
        if output_transform is not None:
            geom.transform(output_transform)
        attrs = ["Missing_Contour_Marker", rec["err_id"], rec["err_id"], rec["elev"],
                 rec["miss_elev"], rec["src_id"], rec["pair_id"]]
        unified_features.append(make_line_feature(unified_layer, geom, attrs))

    # Estimated lines: use the existing script's reconstructed line geometries.
    for feat in missing_layer.getFeatures():
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        geom = QgsGeometry(geom)
        if output_transform is not None:
            geom.transform(output_transform)
        pair_id = feat["PAIR_ID"]
        miss_elev = feat["MISS_ELEV"]
        # The existing estimator groups several slice events. Link to all
        # associated event IDs in a deterministic compact string.
        related = []
        for rec in missing_points:
            if rec["pair_id"] != pair_id:
                continue
            for elev_text in rec["miss_elev"].split(","):
                try:
                    same_elevation = abs(float(elev_text) - float(miss_elev)) <= ELEVATION_TOLERANCE
                except Exception:
                    same_elevation = False
                if same_elevation and rec["err_id"] not in related:
                    related.append(rec["err_id"])
        # ERR_ID is the first contributing event; ERR_IDS preserves the full
        # list where a reconstructed line combines multiple slice events.
        err_id = related[0] if related else ""
        err_ids = ",".join(related)[:120]
        attrs = ["Estimated_Missing_Contour_Lines", err_id, err_ids,
                 float(miss_elev), str(miss_elev), None, pair_id]
        unified_features.append(make_line_feature(unified_layer, geom, attrs))
    unified_layer.dataProvider().addFeatures(unified_features)
    unified_layer.updateExtents()

    # -------------------------------------------------------------------------
    # Pre-export diagnostic: report the exact features prepared for the final
    # output dataset, before invoking the file writer. Diagnostic layers are
    # listed separately because they are currently temporary memory layers,
    # not part of the unified Shapefile output.
    # -------------------------------------------------------------------------
    output_format = PLUGIN_CONFIG.get("output_format", "shp")
    output_format_label = {
        "shp": "ESRI Shapefile",
        "gpkg": "GeoPackage",
        "geojson": "GeoJSON"
    }.get(output_format, str(output_format).upper())

    output_type_counts = defaultdict(int)
    for output_feature in unified_layer.getFeatures():
        try:
            line_type = str(output_feature["LINE_TYPE"] or "(empty)")
        except Exception:
            line_type = "(unreadable LINE_TYPE)"
        output_type_counts[line_type] += 1

    output_count = unified_layer.featureCount()
    pre_export_text = (
        "Pre-export Data Summary\n\n"
        "Output format: %s\n"
        "Total features prepared for output: %d\n\n"
        "Breakdown by LINE_TYPE:\n" % (output_format_label, output_count)
    )
    if output_type_counts:
        for line_type in sorted(output_type_counts.keys()):
            pre_export_text += "  %s: %d\n" % (line_type, output_type_counts[line_type])
    else:
        pre_export_text += "  (no output features)\n"

    pre_export_text += (
        "\nDiagnostic memory layers (not included in the output file):\n"
        "  Global slice lines: %d\n"
        "  Contour intersection points: %d\n"
        "  Contour error features: %d\n"
        "  Candidate marker features: %d\n"
        "  Estimated missing-contour lines: %d\n"
        % (slices_layer.featureCount(), intersections_layer.featureCount(),
           errors_layer.featureCount(), marker_layer.featureCount(),
           missing_layer.featureCount())
    )

    output_base = os.path.join(OUTPUT_FOLDER, "Contour_Completeness_Check")
    if output_format == "shp":
        output_path = output_base + ".shp"
        safe_delete_shapefile(output_path)
        write_shapefile(unified_layer, output_path)
        out_layer = QgsVectorLayer(output_path, "Contour Completeness Check", "ogr")
    elif output_format == "gpkg":
        output_path = output_base + ".gpkg"
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = "Contour_Completeness_Check"
        options.fileEncoding = "UTF-8"
        options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            unified_layer, output_path, QgsCoordinateTransformContext(), options)
        if result[0] != QgsVectorFileWriter.NoError:
            raise Exception("Failed to write GeoPackage: " + str(result[1]))
        out_layer = QgsVectorLayer(output_path + "|layername=Contour_Completeness_Check",
                                  "Contour Completeness Check", "ogr")
    else:
        output_path = output_base + ".geojson"
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GeoJSON"
        options.fileEncoding = "UTF-8"
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            unified_layer, output_path, QgsCoordinateTransformContext(), options)
        if result[0] != QgsVectorFileWriter.NoError:
            raise Exception("Failed to write GeoJSON: " + str(result[1]))
        out_layer = QgsVectorLayer(output_path, "Contour Completeness Check", "ogr")

    check_cancelled()
    if not out_layer.isValid():
        raise Exception("Output file was written but could not be loaded: " + output_path)
    QgsProject.instance().addMapLayer(out_layer)
    report_progress(100, "Writing Results (1/1)",
                    "Output file successfully written.", "WRITING", 1, 1)
    # Diagnostic memory layers are retained internally for analysis/debug
    # accounting, but are deliberately not added to the QGIS project.
    # Only the final Contour Completeness Check output is plotted.
    report_progress(100, "Output saved", output_path)
    log("SELESAI")
    for phase_name, phase_label in (("GLOBAL", "Slice reguler"),
                                    ("ENDPOINT", "Slice tambahan endpoint"),
                                    ("SMALL", "Slice tambahan kontur kecil")):
        st = phase_stats[phase_name]
        avg = st["seconds"] / float(st["count"]) if st["count"] else 0.0
        log("%s: %d slice | waktu %.2f detik | rata-rata %.3f detik/slice" %
            (phase_label, st["count"], st["seconds"], avg))
    log("Kontur checked: %d | unchecked tersisa: %d" %
        (len(checked_fids), max(0, layer.featureCount() - len(checked_fids))))
    log("Fitur sumber: %d" % layer.featureCount())
    log("Slice horizontal: %d" % slice_count_h)
    log("Slice vertikal reguler: %d" % slice_count_v)
    log("Perpotongan yang diproses: %d" % intersection_count)
    log("Perpotongan dengan elevasi tidak valid: %d" % invalid_elevation_count_local[0])
    log("Titik perpotongan kandidat: %d" % sum(1 for ev in all_intersections if ev["is_candidate"]))
    log("Pasangan kandidat: %d" % candidate_count)
    log("Fitur Contour_Errors (hanya segmen hasil buffer): %d" % errors_layer.featureCount())
    log("Fitur Candidate Markers: %d" % marker_layer.featureCount())
    log("Garis perkiraan kontur hilang: %d" % missing_layer.featureCount())
    log("Output folder: " + OUTPUT_FOLDER)
    print("\nOutput:")
    print(output_path)

