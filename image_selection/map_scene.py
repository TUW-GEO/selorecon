#  ***************************************************************************
#  *                                                                         *
#  *   This program is free software; you can redistribute it and/or modify  *
#  *   it under the terms of the GNU General Public License as published by  *
#  *   the Free Software Foundation; either version 2 of the License, or     *
#  *   (at your option) any later version.                                   *
#  *                                                                         *
#  ***************************************************************************

from qgis.PyQt.QtCore import pyqtSignal, pyqtSlot, Qt, QPointF, QSettings
from qgis.PyQt.QtGui import QPen, QPolygonF
from qgis.PyQt.QtWidgets import QFileDialog, QGraphicsPolygonItem, QGraphicsScene, QMessageBox

import pandas as pd
from osgeo import ogr, osr
import sqlite3

import configparser
import glob
import json
import logging
from pathlib import Path

from .aerial_item import ContrastEnhancement, AerialObject, AerialImage, Availability, Usage, Visualization

logger = logging.getLogger(__name__)


def _truncateMsg(msg: str, maxLen = 500):
    if len(msg) > maxLen:
        return msg[:maxLen] + ' ...'
    return msg


class MapScene(QGraphicsScene):

    aerialsLoaded = pyqtSignal(list)

    aerialFootPrintChanged = pyqtSignal(str, str)

    aerialAvailabilityChanged = pyqtSignal(str, int, str)

    aerialUsageChanged = pyqtSignal(str, int)
    
    contrastEnhancement = pyqtSignal(ContrastEnhancement)

    visualizationByAvailability = pyqtSignal(Availability, Visualization, dict)

    visualizationByUsage = pyqtSignal(Usage, bool, dict)


    def __init__(self, *args, epsg: int, config: configparser.ConfigParser, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.__wcs = osr.SpatialReference()
        self.__wcs.ImportFromEPSG(epsg)
        self.__db = None
        self.__aoi = None
        self.__config = config


    @pyqtSlot()
    def selectAoiFile(self):
        fileName = QFileDialog.getOpenFileName(None, "Open the area of interest as a polygon", self.__lastDir, "Polygon formats (*.kml;*.shp);;Any type (*.*)")[0]
        if fileName:
            self.__lastDir = str(Path(fileName).parent)
            self.loadAoiFile(Path(fileName))


    @pyqtSlot()
    def selectAerialsFile(self):
        fileName = QFileDialog.getOpenFileName(None, "Open DB query result", self.__lastDir, "Excel sheets (*.xls);;Any type (*.*)")[0]
        if fileName:
            self.__lastDir = str(Path(fileName).parent)
            self.loadAerialsFile(Path(fileName))


    @pyqtSlot(ContrastEnhancement)
    def setContrastEnhancement(self, contrastEnhancement):
        self.contrastEnhancement.emit(contrastEnhancement)


    @pyqtSlot(Availability, Visualization, dict)
    def setVisualizationByAvailability(self, availability, visualization, usages) -> None:
        self.visualizationByAvailability.emit(availability, visualization, usages)


    @pyqtSlot(Usage, bool, dict)
    def setVisualizationByUsage(self, usage, checked, visualizations) -> None:
        self.visualizationByUsage.emit(usage, checked, visualizations)


    def unload(self):
        AerialImage.unload()
        if self.__db is not None:
            self.__db.close()


    def loadAoiFile(self, fileName: Path) -> None:
        logger.info(f'File with the area of interest to load: {fileName}')
        ds = ogr.Open(str(fileName))
        if ds.GetLayerCount() > 1:
            logger.warning('Data source has multiple layers. Will use the first one.')
        layer = ds.GetLayer(0)
        if layer.GetFeatureCount() != 1:
            return logger.error('First layer does not have a single feature. Choose another file.')
        # For both KML and Shape, layer.GetFeatureCount() reports 1.
        # For KML, GetFeature(1) returns the only feature, while for Shape it must be GetFeature(0).
        # Hence, do not rely on GetFeature(idx), but on iteration, which works for both.
        feature, = layer
        geom = feature.GetGeometryRef()
        geom.FlattenTo2D()
        if geom.GetGeometryType() != ogr.wkbPolygon:
            return logger.error(f"First layer's first feature is not a polygon, but a {geom.GetGeometryName()}. Choose another file.")
        if not geom.IsSimple():
            return logger.error("First layer's first feature is not a simple polygon. Choose another file.")
        geom.TransformTo(self.__wcs)
        assert geom.GetGeometryCount() >= 1
        outerRing = geom.GetGeometryRef(0)
        pts = outerRing.GetPoints()
        scenePos = QPointF(pts[0][0], -pts[0][1])
        qPts = []
        for pt in pts:
            qPts.append(QPointF(pt[0], -pt[1]) - scenePos)
        polyg = QGraphicsPolygonItem(QPolygonF(qPts))
        polyg.setPos(scenePos)
        polyg.setZValue(100)
        pen = QPen(Qt.magenta, 3)
        pen.setCosmetic(True)
        polyg.setPen(pen)
        if self.__aoi is not None:
            self.removeItem(self.__aoi)
        self.addItem(polyg)
        self.__aoi = polyg
        for view in self.views():
            view.fitInView(self.itemsBoundingRect(), Qt.KeepAspectRatio)


    def loadAerialsFile(self, fileName: Path) -> None:
        logger.info(f'Spreadsheet with image meta data to load: {fileName}')
        if self.__db is not None:
            self.__db.close()
        dbPath = fileName.with_suffix('.sqlite')
        if dbPath.exists():
            button = QMessageBox.question(
                None, 'Data base exists', f'Data base {dbPath} already exists.<br/>Open and load orientations? Otherwise, it will be overwritten.',
                QMessageBox.Open | QMessageBox.Discard | QMessageBox.Abort)
            if button == QMessageBox.Abort:
                return
            if button == QMessageBox.Discard:
                dbPath.unlink()
      
        if self.__aoi is not None:
            self.removeItem(self.__aoi)
        self.clear()
        if self.__aoi is not None:
            self.addItem(self.__aoi)

        AerialImage.previewRootDir = Path(self.__config['PREVIEWS']['rootDir'])
        if not AerialImage.previewRootDir.is_absolute():
            AerialImage.previewRootDir = fileName.parent / AerialImage.previewRootDir

        imageRootDir = Path(self.__config['IMAGES']['rootDir'])
        if not imageRootDir.is_absolute():
            imageRootDir = fileName.parent / imageRootDir
        AerialImage.imageRootDir = imageRootDir
        imgExt = '.ecw'

        fsImgFiles = set(Path(el) for el in glob.iglob(str(imageRootDir / ('**/*' + imgExt)), recursive=True))
        sheet_name='Geo_Abfrage_SQL'
        df = pd.read_excel(fileName, sheet_name=sheet_name, true_values=['Ja', 'ja'], false_values=['Nein', 'nein'])
        if not self.__cleanData(df, sheet_name):
            return

        self.__db = sqlite3.connect(dbPath, isolation_level=None)
        self.__db.execute('PRAGMA foreign_keys = ON')
        AerialImage.createTables(self.__db)

        xlsImgFiles = []
        shouldBeMissing = []
        shouldBeThere = []
        aerialObjects = []
        # Speed up the creating of a new DB, especially if it is located on a network drive.
        # Also, errors during setup will leave an existing DB in its original state.
        self.__db.execute('BEGIN TRANSACTION')
        for row in df.itertuples(index=False):
            #fn = f'{row.Datum.year}-{row.Datum.month:02}-{row.Datum.day:02}_{row.Sortie}_{row.Bildnr}' + imgExt
            #imgFilePath = imageRootDir / fn
            imgId = Path(row.Sortie) / f'{row.Bildnr}{imgExt}'
            imgFilePath = imageRootDir / imgId
            if not row.LBDB and imgFilePath in fsImgFiles:
                shouldBeMissing.append(imgFilePath.name)
            elif row.LBDB and imgFilePath not in fsImgFiles:
                shouldBeThere.append(imgFilePath.name)
            xlsImgFiles.append(imgFilePath)
            csDb = osr.SpatialReference()
            csDb.ImportFromEPSG(row.EPSG_Code)
            assert csDb.IsProjected() or csDb.IsGeographic()
            db2wcs = osr.CoordinateTransformation(csDb, self.__wcs)
            x, y = row.x, row.y
            if csDb.EPSGTreatsAsNorthingEasting() or csDb.EPSGTreatsAsLatLong():
                x, y = y, x
            wcsCtr = db2wcs.TransformPoint(x, y)
            aerialObjects.append(AerialObject(self, QPointF(wcsCtr[0], -wcsCtr[1]), str(imgId), row, self.__db))

        self.__db.execute('COMMIT TRANSACTION')

        for view in self.views():
            view.fitInView(self.itemsBoundingRect(), Qt.KeepAspectRatio)

        if any((shouldBeMissing, shouldBeThere)):
            msgs = []
            if shouldBeMissing:
                msgs.append('{} out of {} files should be missing according to {}, but they are present: {}'.format(
                    len(shouldBeMissing), len(xlsImgFiles), sheet_name, ', '.join(shouldBeMissing)))
            if shouldBeThere:
                msgs.append('{} out of {} files should be present according to {}, but they are missing: {}'.format(
                    len(shouldBeThere), len(xlsImgFiles), sheet_name, ', '.join(shouldBeThere)))
            for msg in msgs:
                logger.warning(msg)
            QMessageBox.warning(None, "Inconsistency", _truncateMsg('\n'.join(msgs)))

        xlsImgFiles = set(xlsImgFiles)
        spare = fsImgFiles - xlsImgFiles
        if spare:
            msg = '{} files are present, but not in {}: {}'.format(len(spare), sheet_name, ', '.join(el.name for el in spare))
            logger.warning(msg)
            QMessageBox.warning(None, "Inconsistency", _truncateMsg(msg))

        logger.info('{} of {} images available.'.format(
            sum(el.image.availability() == Availability.image for el in aerialObjects), len(aerialObjects)))

        aerials = {}
        cursor = self.__db.execute('SELECT * FROM aerials')
        iId = [el[0] for el in cursor.description].index('id')
        for row in cursor:
            aerial = {name: val for (name, *_), val in zip(cursor.description, row)
                      if name not in ('trafo', 'scenePos', 'previewRect')}
            aerial['meta'] = json.loads(aerial['meta'])
            aerials[row[iId]] = aerial

        for aerialObject in aerialObjects:
            imgId, footprint = aerialObject.image.idAndFootprint(asJson=False)
            aerials[imgId].update([('footprint', footprint),
                                   ('availability', int(aerialObject.image.availability()))])

        self.aerialsLoaded.emit(list(aerials.values()))


    def __cleanData(self, df: pd.DataFrame, sheet_name: str) -> bool:
        def error(msg):
            logger.error(msg)
            QMessageBox.critical(None, "Erroneous Coordinate Reference System", msg)
            return False

        df['Datum'] = df['Datum'].dt.date  # strip time of day

        # EPSG codes' column seems to be named either 'EPSG-Code', or 'EPSGCode'.
        # Standardize the name into a Python identifier.
        iEpsgs = [idx for idx, el in enumerate(df.columns) if 'epsg' in el.lower()]
        iXWgs84s = [idx for idx, el in enumerate(df.columns) if 'xwgs84' in el.lower()]
        iYWgs84s = [idx for idx, el in enumerate(df.columns) if 'ywgs84' in el.lower()]
        for idxs, name in [(iEpsgs, 'EPSG code'), (iXWgs84s, 'WGS84 longitude'), (iYWgs84s, 'WGS84 latitude')]:
            if len(idxs) > 1:
                return error('Multiple columns in {} seem to provide {}: {}.'.format(sheet_name, name, ', '.join(df.columns[idx] for idx in idxs)))
        if iEpsgs and (iXWgs84s or iYWgs84s):
            return error(f'{sheet_name} defines columns both for EPSG code and WGS84 coordinates.')
        if iEpsgs:
            df.rename(columns={df.columns[iEpsgs[0]]: "EPSG_Code"}, inplace=True)
        elif iXWgs84s or iYWgs84s:
            if not (iXWgs84s and iYWgs84s):
                return error(f'{sheet_name} defines only one WGS84 coordinate.')
            #series = pd.Series([4326] * len(df))
            df['EPSG_Code'] = [4326] * len(df)
            df.rename(columns={df.columns[iXWgs84s[0]]: "x", df.columns[iYWgs84s[0]]: "y"}, inplace=True)
        else:
            return error(f"{sheet_name} seems to provide no information on coordinate system. Columns are: {', '.join(df.columns)}")
        return True

    @property
    def __lastDir(self):
        settings = QSettings("TU WIEN", "Image Selection", self)
        return settings.value("lastDir", ".")

    @__lastDir.setter
    def __lastDir(self, value: str):
        settings = QSettings("TU WIEN", "Image Selection", self)
        settings.setValue("lastDir", value)
