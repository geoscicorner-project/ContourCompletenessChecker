# -*- coding: utf-8 -*-
"""Contour Completeness Checker - QGIS plugin."""
import os
import time
import traceback

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAction, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QDoubleSpinBox, QLineEdit, QFileDialog,
    QMessageBox, QGroupBox, QFormLayout, QCheckBox, QApplication, QWidget,
    QProgressBar, QDialogButtonBox
)
from qgis.gui import QgsProjectionSelectionDialog
from qgis.core import (
    QgsProject, QgsWkbTypes, QgsVectorLayer, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsUnitTypes
)


class ProcessingMonitorDialog(QDialog):
    """Single-window monitor for analysis, result writing and memory usage."""
    STAGES = (
        ("GLOBAL", "Global Check"),
        ("ENDPOINT", "Local Endpoint Check"),
        ("SMALL", "Local Small-Feature Check"),
        ("WRITING", "Writing Results"),
    )

    def __init__(self, parent=None):
        super(ProcessingMonitorDialog, self).__init__(parent)
        self.setWindowTitle("Contour Completeness Checker — Processing Monitor")
        self.setMinimumWidth(480)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.cancelled = False
        self.finished = False
        self.started = time.time()
        self.stage_widgets = {}
        root = QVBoxLayout(self)

        for key, title in self.STAGES:
            group = QGroupBox(title)
            layout = QVBoxLayout(group)
            count = QLabel("Waiting")
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            eta = QLabel("Estimated time remaining: —")
            layout.addWidget(count)
            layout.addWidget(bar)
            layout.addWidget(eta)
            root.addWidget(group)
            self.stage_widgets[key] = {"count": count, "bar": bar, "eta": eta}

        mem_group = QGroupBox("System Resource Monitor")
        mem_layout = QVBoxLayout(mem_group)
        self.system_memory_label = QLabel("System RAM: collecting…")
        self.process_memory_label = QLabel("QGIS process RAM: collecting…")
        mem_layout.addWidget(self.system_memory_label)
        mem_layout.addWidget(self.process_memory_label)
        root.addWidget(mem_group)

        self.elapsed_label = QLabel("Elapsed: 00:00:00")
        root.addWidget(self.elapsed_label)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel)
        root.addWidget(self.cancel_button)

        self.memory_timer = QTimer(self)
        self.memory_timer.timeout.connect(self.update_memory)
        self.memory_timer.start(1000)
        self.update_memory()

    def _cancel(self):
        self.cancelled = True
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancelling…")

    def update_stage(self, key, value, count_text, eta_text):
        if key not in self.stage_widgets:
            return
        w = self.stage_widgets[key]
        if int(value) < 0:
            # Indeterminate mode: the file writer does not expose byte/feature
            # callbacks while its synchronous write operation is running.
            w["bar"].setRange(0, 0)
        else:
            if w["bar"].minimum() == 0 and w["bar"].maximum() == 0:
                w["bar"].setRange(0, 100)
            w["bar"].setValue(max(0, min(100, int(value))))
        w["count"].setText(count_text)
        w["eta"].setText(eta_text)

    def update_elapsed(self):
        elapsed = max(0, int(time.time() - self.started))
        self.elapsed_label.setText("Elapsed: %s" % time.strftime("%H:%M:%S", time.gmtime(elapsed)))

    def update_memory(self):
        self.update_elapsed()
        used = total = None
        process_used = None
        try:
            import psutil
            vm = psutil.virtual_memory()
            used, total = vm.used, vm.total
            process_used = psutil.Process(os.getpid()).memory_info().rss
        except Exception:
            # Windows fallback for total/used system RAM; process RAM remains
            # unavailable when psutil is not installed.
            try:
                import ctypes
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                    total = stat.ullTotalPhys
                    used = total - stat.ullAvailPhys
            except Exception:
                pass

        def fmt(value):
            return "%.2f GB" % (float(value) / (1024.0 ** 3)) if value is not None else "Unavailable"

        if total is not None:
            pct = (100.0 * used / total) if total else 0
            self.system_memory_label.setText(
                "System RAM: %s / %s (%.1f%%)" % (fmt(used), fmt(total), pct))
        else:
            self.system_memory_label.setText("System RAM: unavailable")
        self.process_memory_label.setText("QGIS process RAM: %s" % fmt(process_used))

    def closeEvent(self, event):
        # Prevent accidental closure while the engine still references this
        # dialog through its progress/cancellation callbacks.
        if self.finished:
            event.accept()
        else:
            event.ignore()



class CheckerDialog(QDialog):
    def __init__(self, parent=None):
        super(CheckerDialog, self).__init__(parent)
        self.setWindowTitle("Contour Completeness Checker")
        self.setMinimumWidth(560)
        self._external_layer = None
        self._gdb_sublayers = []
        layout = QVBoxLayout(self)

        input_box = QGroupBox("Input")
        form = QFormLayout(input_box)
        self.source_mode = QComboBox()
        self.source_mode.addItem("Loaded Layer", "loaded")
        self.source_mode.addItem("Shapefile", "shp")
        self.source_mode.addItem("File Geodatabase", "gdb")
        form.addRow("Input source:", self.source_mode)

        self.layer_combo = QComboBox()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_layers)
        loaded_row = QHBoxLayout()
        loaded_row.addWidget(self.layer_combo, 1)
        loaded_row.addWidget(self.refresh_button)
        self.loaded_row_widget = QWidget()
        self.loaded_row_widget.setLayout(loaded_row)
        form.addRow("Contour polyline layer:", self.loaded_row_widget)

        self.path_edit = QLineEdit()
        self.path_browse = QPushButton("Browse…")
        self.path_browse.clicked.connect(self.browse_source)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.path_browse)
        self.path_row_widget = QWidget()
        self.path_row_widget.setLayout(path_row)
        form.addRow("Source path:", self.path_row_widget)

        self.gdb_layer_combo = QComboBox()
        form.addRow("Feature class:", self.gdb_layer_combo)
        self.elev_combo = QComboBox()
        self.elev_combo.currentIndexChanged.connect(self.estimate_contour_interval)
        form.addRow("Elevation field:", self.elev_combo)

        self.crs_info_label = QLabel("CRS: Select an input layer")
        self.crs_info_label.setWordWrap(True)
        self.crs_info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.crs_select_button = QPushButton("Select CRS…")
        self.crs_select_button.clicked.connect(self.select_input_crs)
        crs_row = QHBoxLayout()
        crs_row.addWidget(self.crs_info_label, 1)
        crs_row.addWidget(self.crs_select_button)
        crs_widget = QWidget()
        crs_widget.setLayout(crs_row)
        form.addRow("Coordinate reference system:", crs_widget)
        self._input_crs_override = None
        self._crs_override_layer_key = None

        self.source_mode.currentIndexChanged.connect(self.update_source_controls)
        self.layer_combo.currentIndexChanged.connect(self.refresh_fields)
        self.layer_combo.currentIndexChanged.connect(self.update_crs_info)
        self.gdb_layer_combo.currentIndexChanged.connect(self.refresh_fields)
        self.gdb_layer_combo.currentIndexChanged.connect(self.update_crs_info)
        self.refresh_layers()
        self.update_source_controls()
        layout.addWidget(input_box)

        params = QGroupBox("Analysis parameters")
        pform = QFormLayout(params)
        self.contour_interval = QDoubleSpinBox()
        self.contour_interval.setDecimals(4)
        self.contour_interval.setRange(0.0001, 1000000)
        self.contour_interval.setValue(2.0)
        self.slice_interval = QDoubleSpinBox()
        self.slice_interval.setDecimals(3)
        self.slice_interval.setRange(0.01, 10000000)
        self.slice_interval.setValue(100.0)
        pform.addRow("Contour interval (map units):", self.contour_interval)
        pform.addRow("Checking interval (metres):", self.slice_interval)

        self.regular_count_label = QLabel("Estimated global checks: —")
        self.regular_count_label.setWordWrap(True)
        self.regular_note_label = QLabel(
            "Global checks are the first stage only. Processing time per slice "
            "depends on the number of contour features. The subsequent endpoint "
            "and local small-feature checks depend strongly on data quality."
        )
        self.regular_note_label.setWordWrap(True)
        self.regular_note_label.setStyleSheet("color: #666666;")
        pform.addRow(self.regular_count_label)
        pform.addRow(self.regular_note_label)
        self.slice_interval.valueChanged.connect(self.update_regular_estimate)
        layout.addWidget(params)

        output = QGroupBox("Output")
        oform = QFormLayout(output)
        self.format_combo = QComboBox()
        self.format_combo.addItem("ESRI Shapefile — one polyline file", "shp")
        self.format_combo.addItem("GeoPackage — one file, one layer", "gpkg")
        self.format_combo.addItem("GeoJSON — one file, mixed features", "geojson")
        oform.addRow("Format:", self.format_combo)
        self.output_edit = QLineEdit(os.path.join(os.path.expanduser("~"),
                                                   "Downloads", "Contour_Completeness_Check"))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse_output)
        outrow = QHBoxLayout()
        outrow.addWidget(self.output_edit, 1)
        outrow.addWidget(browse)
        oform.addRow("Output folder:", outrow)

        self.aoi_check = QCheckBox("Gunakan AOI boundary untuk meningkatkan akurasi hasil")
        self.aoi_source_combo = QComboBox()
        self.aoi_source_combo.addItem("Loaded layer", "loaded")
        self.aoi_source_combo.addItem("From file", "file")
        self.aoi_combo = QComboBox()
        self.aoi_path_edit = QLineEdit()
        self.aoi_path_edit.setPlaceholderText("Pilih file AOI (SHP, GPKG, GeoJSON, dan lainnya)")
        self.aoi_browse_button = QPushButton("Browse…")
        self.aoi_browse_button.clicked.connect(self.browse_aoi_file)
        self.aoi_loaded_row = QWidget()
        aoi_loaded_layout = QHBoxLayout(self.aoi_loaded_row)
        aoi_loaded_layout.setContentsMargins(0, 0, 0, 0)
        aoi_loaded_layout.addWidget(self.aoi_combo, 1)
        self.aoi_file_row = QWidget()
        aoi_file_layout = QHBoxLayout(self.aoi_file_row)
        aoi_file_layout.setContentsMargins(0, 0, 0, 0)
        aoi_file_layout.addWidget(self.aoi_path_edit, 1)
        aoi_file_layout.addWidget(self.aoi_browse_button)
        self.refresh_aoi_layers()
        self.aoi_check.toggled.connect(self.update_aoi_controls)
        self.aoi_source_combo.currentIndexChanged.connect(self.update_aoi_controls)
        oform.addRow(self.aoi_check)
        oform.addRow("AOI source:", self.aoi_source_combo)
        oform.addRow("Loaded AOI layer:", self.aoi_loaded_row)
        oform.addRow("AOI file:", self.aoi_file_row)
        self.update_aoi_controls()
        layout.addWidget(output)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        run = QPushButton("Run check")
        cancel.clicked.connect(self.reject)
        run.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(run)
        layout.addLayout(buttons)
        self.update_source_controls()

    def update_source_controls(self):
        mode = self.source_mode.currentData()
        is_loaded = mode == "loaded"
        is_gdb = mode == "gdb"
        self.loaded_row_widget.setVisible(is_loaded)
        self.path_row_widget.setVisible(not is_loaded)
        self.gdb_layer_combo.setVisible(is_gdb)
        self.refresh_button.setEnabled(is_loaded)
        self.path_edit.setPlaceholderText("Select a .shp file" if mode == "shp" else "Select a .gdb folder")
        self.refresh_fields()
        self.update_crs_info()

    def refresh_layers(self):
        current = self.layer_combo.currentData() if self.layer_combo.count() else None
        self.layer_combo.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and layer.isValid() and \
                    layer.geometryType() == QgsWkbTypes.LineGeometry:
                self.layer_combo.addItem(layer.name(), layer.id())
        if current:
            idx = self.layer_combo.findData(current)
            if idx >= 0:
                self.layer_combo.setCurrentIndex(idx)
        self.refresh_aoi_layers()
        self.refresh_fields()
        self.update_crs_info()

    def browse_source(self):
        mode = self.source_mode.currentData()
        if mode == "shp":
            path, _ = QFileDialog.getOpenFileName(
                self, "Select contour Shapefile", self.path_edit.text(),
                "Shapefile (*.shp);;All files (*.*)")
            if path:
                self.path_edit.setText(path)
                self._external_layer = QgsVectorLayer(path, os.path.splitext(os.path.basename(path))[0], "ogr")
                self.refresh_fields()
                self.update_crs_info()
        elif mode == "gdb":
            path = QFileDialog.getExistingDirectory(self, "Select File Geodatabase (.gdb)",
                                                     self.path_edit.text())
            if path:
                self.path_edit.setText(path)
                self.load_gdb_sublayers(path)
                self.update_crs_info()

    def load_gdb_sublayers(self, path):
        self.gdb_layer_combo.clear()
        self._gdb_sublayers = []
        try:
            from qgis.core import QgsProviderRegistry
            details = QgsProviderRegistry.instance().querySublayers(path)
            for detail in details:
                if detail.providerKey() != "ogr":
                    continue
                uri = detail.uri()
                name = detail.name()
                candidate = QgsVectorLayer(uri, name, "ogr")
                if candidate.isValid() and candidate.geometryType() == QgsWkbTypes.LineGeometry:
                    self._gdb_sublayers.append((name, uri))
                    self.gdb_layer_combo.addItem(name, uri)
        except Exception as ex:
            QMessageBox.warning(self, "File Geodatabase", "Could not read geodatabase layers:\n%s" % ex)
        if not self._gdb_sublayers:
            QMessageBox.warning(self, "File Geodatabase",
                                "No valid polyline feature classes were found in this geodatabase.")
        self.refresh_fields()
        self.update_crs_info()

    def selected_source_layer(self):
        mode = self.source_mode.currentData()
        if mode == "loaded":
            layer_id = self.layer_combo.currentData()
            return QgsProject.instance().mapLayer(layer_id) if layer_id else None
        if mode == "shp":
            path = self.path_edit.text().strip()
            if not path:
                return None
            if self._external_layer is None or self._external_layer.source() != path:
                self._external_layer = QgsVectorLayer(path, os.path.splitext(os.path.basename(path))[0], "ogr")
            return self._external_layer if self._external_layer.isValid() else None
        uri = self.gdb_layer_combo.currentData()
        if not uri:
            return None
        self._external_layer = QgsVectorLayer(uri, self.gdb_layer_combo.currentText(), "ogr")
        return self._external_layer if self._external_layer.isValid() else None

    def _layer_key(self, layer):
        return (layer.id(), layer.source()) if layer is not None else None

    def update_crs_info(self):
        layer = self.selected_source_layer()
        if not layer or not layer.isValid():
            self.crs_info_label.setText("CRS: Select a valid contour layer.")
            self._input_crs_override = None
            self._crs_override_layer_key = None
            return
        key = self._layer_key(layer)
        if self._crs_override_layer_key != key:
            self._input_crs_override = None
            self._crs_override_layer_key = key
        crs = self._input_crs_override or layer.crs()
        if crs.isValid() and crs.authid():
            self.crs_info_label.setText(
                "Detected: %s | Authority: %s | Ellipsoid: %s | Units: %s" %
                (crs.description() or crs.authid(), crs.authid(),
                 crs.ellipsoidAcronym() or "Not specified", str(crs.mapUnits()))
            )
            self.crs_info_label.setStyleSheet("color: #237a3b;")
        elif crs.isValid():
            self.crs_info_label.setText(
                "CRS definition exists but has no recognized authority ID. "
                "Please confirm or select the input CRS."
            )
            self.crs_info_label.setStyleSheet("color: #a66a00;")
        else:
            self.crs_info_label.setText(
                "CRS could not be identified. Select the CRS and datum used by the input data."
            )
            self.crs_info_label.setStyleSheet("color: #b00020;")

    def select_input_crs(self):
        layer = self.selected_source_layer()
        if not layer or not layer.isValid():
            QMessageBox.warning(self, "Input CRS", "Select a valid contour layer first.")
            return
        current = self._input_crs_override or layer.crs()
        dialog = QgsProjectionSelectionDialog(self)
        if current.isValid():
            dialog.setCrs(current)
        dialog.setWindowTitle("Select Input Coordinate Reference System")
        if dialog.exec_():
            selected = dialog.crs()
            if selected.isValid():
                self._input_crs_override = selected
                self._crs_override_layer_key = self._layer_key(layer)
                self.update_crs_info()

    def resolved_input_crs(self):
        layer = self.selected_source_layer()
        if not layer or not layer.isValid():
            return None
        crs = self._input_crs_override or layer.crs()
        return crs if crs.isValid() and crs.authid() else None

    def refresh_fields(self):
        if not hasattr(self, "elev_combo"):
            return
        self.elev_combo.clear()
        layer = self.selected_source_layer()
        if layer and layer.isValid():
            for field in layer.fields():
                self.elev_combo.addItem(field.name(), field.name())
            preferred = self.elev_combo.findText("VALKNT")
            if preferred >= 0:
                self.elev_combo.setCurrentIndex(preferred)
            self.estimate_contour_interval()
        self.update_regular_estimate()

    def estimate_contour_interval(self):
        """Estimate the most common positive elevation step from the selected field."""
        if not hasattr(self, "contour_interval"):
            return
        layer = self.selected_source_layer()
        field_name = self.elev_combo.currentData()
        if not layer or not field_name:
            return
        field_index = layer.fields().indexFromName(field_name)
        if field_index < 0:
            return
        try:
            values = set()
            # Provider uniqueValues avoids transferring full feature geometries.
            for value in layer.uniqueValues(field_index):
                if value is None:
                    continue
                try:
                    number = float(value)
                    if number == number and abs(number) != float("inf"):
                        values.add(round(number, 6))
                except (TypeError, ValueError):
                    continue
            ordered = sorted(values)
            differences = []
            for i in range(1, len(ordered)):
                delta = round(ordered[i] - ordered[i - 1], 3)
                if delta > 0:
                    differences.append(delta)
            if differences:
                # Most frequent positive adjacent elevation difference.
                counts = {}
                for delta in differences:
                    counts[delta] = counts.get(delta, 0) + 1
                estimate = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
                self.contour_interval.setValue(float(estimate))
        except Exception:
            # Keep the current editable value if the provider cannot enumerate unique values.
            pass

    def update_regular_estimate(self):
        """Estimate global horizontal + vertical slices in the same metric CRS used by the engine."""
        if not hasattr(self, "slice_interval") or not hasattr(self, "regular_count_label"):
            return
        layer = self.selected_source_layer()
        if not layer or not layer.isValid() or layer.featureCount() == 0:
            self.regular_count_label.setText("Estimated global checks: —")
            return
        try:
            source_crs = layer.crs()
            extent = layer.extent()
            if source_crs.isGeographic() or source_crs.mapUnits() != QgsUnitTypes.DistanceMeters:
                ext = extent
                center = ext.center()
                wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
                if source_crs != wgs84:
                    to_wgs = QgsCoordinateTransform(source_crs, wgs84, QgsProject.instance())
                    center = to_wgs.transform(center)
                lon, lat = center.x(), center.y()
                zone = max(1, min(60, int((lon + 180.0) // 6) + 1))
                epsg = (32600 + zone) if lat >= 0 else (32700 + zone)
                target_crs = QgsCoordinateReferenceSystem("EPSG:%d" % epsg)
                transform = QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance())
                extent = transform.transformBoundingBox(extent)
            width = max(0.0, extent.width())
            height = max(0.0, extent.height())
            interval = max(0.01, float(self.slice_interval.value()))
            horizontal = max(0, int((height - interval) // interval) + 1) if height >= interval else 0
            vertical = max(0, int((width - interval) // interval) + 1) if width >= interval else 0
            total = horizontal + vertical
            self.regular_count_label.setText(
                "Estimated global checks: %s" % format(total, ",")
            )
        except Exception:
            self.regular_count_label.setText("Estimated global checks: unable to estimate for this layer.")

    def refresh_aoi_layers(self):
        if not hasattr(self, "aoi_combo"):
            return
        current = self.aoi_combo.currentData() if self.aoi_combo.count() else None
        self.aoi_combo.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and layer.isValid() and \
                    layer.geometryType() in (QgsWkbTypes.LineGeometry, QgsWkbTypes.PolygonGeometry):
                self.aoi_combo.addItem(layer.name(), layer.id())
        if current:
            idx = self.aoi_combo.findData(current)
            if idx >= 0:
                self.aoi_combo.setCurrentIndex(idx)

    def update_aoi_controls(self, *args):
        enabled = self.aoi_check.isChecked()
        use_loaded = self.aoi_source_combo.currentData() == "loaded"
        self.aoi_source_combo.setEnabled(enabled)
        self.aoi_loaded_row.setVisible(enabled and use_loaded)
        self.aoi_file_row.setVisible(enabled and not use_loaded)
        self.aoi_combo.setEnabled(enabled and use_loaded)
        self.aoi_path_edit.setEnabled(enabled and not use_loaded)
        self.aoi_browse_button.setEnabled(enabled and not use_loaded)

    def browse_aoi_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select AOI boundary file", self.aoi_path_edit.text(),
            "Vector files (*.shp *.gpkg *.geojson *.json *.gml *.kml);;All files (*)"
        )
        if path:
            self.aoi_path_edit.setText(path)

    def browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "Select output folder",
                                                 self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def get_config(self):
        layer = self.selected_source_layer()
        aoi = None
        aoi_enabled = self.aoi_check.isChecked()
        aoi_error = None
        if aoi_enabled:
            if self.aoi_source_combo.currentData() == "loaded":
                aoi_id = self.aoi_combo.currentData()
                aoi = QgsProject.instance().mapLayer(aoi_id) if aoi_id else None
            else:
                aoi_path = self.aoi_path_edit.text().strip()
                if aoi_path:
                    aoi_name = os.path.splitext(os.path.basename(aoi_path))[0]
                    aoi = QgsVectorLayer(aoi_path, aoi_name, "ogr")
                    if not aoi.isValid():
                        aoi_error = "File AOI tidak dapat dibuka: %s" % aoi_path
                else:
                    aoi_error = "Pilih file AOI terlebih dahulu."
        return {
            "source_layer": layer,
            "input_crs": self.resolved_input_crs(),
            "elevation_field": self.elev_combo.currentData(),
            "contour_interval": self.contour_interval.value(),
            "slice_interval": self.slice_interval.value(),
            "output_folder": self.output_edit.text().strip(),
            "output_format": self.format_combo.currentData(),
            "aoi_layer": aoi,
            "aoi_enabled": aoi_enabled,
            "aoi_error": aoi_error
        }


class ContourCompletenessChecker:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.plugin_dir = os.path.dirname(__file__)

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        self.action = QAction(QIcon(icon_path), "Contour Completeness Checker", self.iface.mainWindow())
        self.action.setToolTip("Contour Completeness Checker")
        self.action.setStatusTip("Run Contour Completeness Checker")
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu("&Contour Completeness Checker", self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginMenu("&Contour Completeness Checker", self.action)
            self.iface.removeToolBarIcon(self.action)

    def run(self):
        dlg = CheckerDialog(self.iface.mainWindow())
        if dlg.exec_() != QDialog.Accepted:
            return
        config = dlg.get_config()
        if config["source_layer"] is None:
            QMessageBox.warning(self.iface.mainWindow(), "Input required",
                                "Please select a valid contour polyline source (loaded layer, Shapefile, or File Geodatabase feature class).")
            return
        if not config["elevation_field"]:
            QMessageBox.warning(self.iface.mainWindow(), "Elevation field required",
                                "Please select the contour elevation field.")
            return
        if not config["output_folder"]:
            QMessageBox.warning(self.iface.mainWindow(), "Output folder required",
                                "Please select an output folder.")
            return
        if config.get("aoi_enabled"):
            aoi_layer = config.get("aoi_layer")
            if config.get("aoi_error"):
                QMessageBox.warning(self.iface.mainWindow(), "AOI input", config["aoi_error"])
                return
            if aoi_layer is None or not aoi_layer.isValid():
                QMessageBox.warning(self.iface.mainWindow(), "AOI input",
                                    "Pilih layer AOI yang valid atau file AOI terlebih dahulu.")
                return
            if aoi_layer.geometryType() not in (QgsWkbTypes.LineGeometry, QgsWkbTypes.PolygonGeometry):
                QMessageBox.warning(self.iface.mainWindow(), "AOI geometry",
                                    "AOI harus berupa layer garis atau poligon.")
                return
            if not aoi_layer.crs().isValid():
                QMessageBox.warning(self.iface.mainWindow(), "AOI CRS",
                                    "CRS layer AOI tidak valid. Tetapkan CRS terlebih dahulu.")
                return
        if not os.path.isdir(config["output_folder"]):
            try:
                os.makedirs(config["output_folder"])
            except Exception as ex:
                QMessageBox.critical(self.iface.mainWindow(), "Output folder error", str(ex))
                return

        progress = ProcessingMonitorDialog(self.iface.mainWindow())
        progress.setWindowModality(Qt.WindowModal)
        progress.show()
        QApplication.processEvents()

        def update_progress(value, message, eta_text="", stage=None, count=None, total=None):
            if stage:
                if int(value) < 0:
                    count_text = message
                else:
                    count_text = ("%s / %s" % (count, total)
                                  if count is not None and total is not None
                                  else message)
                    if int(value) >= 100:
                        count_text += " — Completed"
                progress.update_stage(stage, value, count_text, eta_text)
            QApplication.processEvents()

        try:
            from . import contour_engine
            contour_engine.set_plugin_config(
                config, update_progress, lambda: progress.cancelled)
            contour_engine.run_checker()
            progress.finished = True
            QMessageBox.information(
                self.iface.mainWindow(), "Contour check completed",
                "The contour completeness check has finished.\n\nOutput folder:\n%s" %
                config["output_folder"])
        except Exception as ex:
            progress.finished = True
            progress.close()
            traceback.print_exc()
            QMessageBox.critical(self.iface.mainWindow(), "Contour check failed",
                                 "The analysis could not be completed:\n%s" % str(ex))
        finally:
            progress.finished = True
            progress.close()
