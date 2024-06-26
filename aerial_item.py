#  ***************************************************************************
#  *                                                                         *
#  *   This program is free software; you can redistribute it and/or modify  *
#  *   it under the terms of the GNU General Public License as published by  *
#  *   the Free Software Foundation; either version 2 of the License, or     *
#  *   (at your option) any later version.                                   *
#  *                                                                         *
#  ***************************************************************************

"""
/***************************************************************************
 SelORecon
                                 A QGIS plugin
 Guided selection and orientation of aerial reconnaissance images.
                              -------------------
        copyright            : (C) 2021 by Photogrammetry @ GEO, TU Wien, Austria
        email                : wilfried.karel@geo.tuwien.ac.at
 ***************************************************************************/

Display aerials either as points, or as images.

Images shall scale with zoom, but points not.
For points to always be drawn with the same viewport size, set QGraphicsItem.ItemIgnoresTransformations.
It would be natural to combine AerialPoint and AerialImage in a common graphics item,
so they automatically stay at the same relative position, even when moved within the scene.
ItemIgnoresTransformations propagates to children and cannot be unset for a child.
Hence, AerialImage cannot be a child of AerialPoint.
AerialPoint could be a child of AerialImage: the image would scale with zoom, while the point would not.
However, it seems that the bounding rectangle that Qt computes for an item with ItemIgnoresTransformations set
is generally wrong unless it is a top-level item:
if the point is shown (and the image is hidden) and one has zoomed far out,
then the point would still be shown with the same size, but it's bounding rectangle would be a single pixel,
making it practically impossible to click onto it.
Hence, AerialPoint cannot be a child of AerialImage, either,
and it does not make a difference if both are children
of another (invisible; ItemHasNoContents) item or members of a QGraphicsItemGroup.
Hence, make them 2 separate, top-level items that reference each other.
Whenever one of them
- gets hidden, it tells the other one to show and vice versa.
- is moved, it moves the other one.

Possibly, it would work to integrate both in a common QGraphicsItem
by constantly updating AerialPoint's bounding rectangle using QGraphicsItem.deviceTransform.
However, this sounds slow.
"""
from __future__ import annotations

from qgis.PyQt.QtCore import pyqtSlot, QEvent, QObject, QPointF, QSize, QRect, Qt
from qgis.PyQt.QtGui import QBitmap, QBrush, QColor, QCursor, QFocusEvent, QHelpEvent, QIcon, QImage, QKeyEvent, QPen, QPainter, QPixmap, QTransform
from qgis.PyQt.QtWidgets import (QDialog, QGraphicsEffect, QGraphicsEllipseItem, QGraphicsItem, QGraphicsLineItem, QGraphicsPixmapItem,
                                 QGraphicsSceneContextMenuEvent, QGraphicsSceneMouseEvent,
                                 QGraphicsSceneWheelEvent, QMenu, QMessageBox, QStyle, QStyleOptionGraphicsItem, QWhatsThis, QWidget)

import numpy as np
from osgeo import gdal

from concurrent import futures
import datetime
import enum
import json
import logging
from pathlib import Path
import sqlite3
import threading
from typing import cast, Final
import weakref

from . import GdalPushLogHandler
from .preview_window import claheAvailable, ContrastEnhancement, enhanceContrast, PreviewWindow
from . import map_scene
from .georef import georef

logger: Final = logging.getLogger(__name__)


class Availability(enum.IntEnum):
    def __new__(cls, color):
        value = len(cls.__members__)
        obj = int.__new__(cls, value)
        obj._value_ = value
        obj.color = color
        return obj

    color: Qt.GlobalColor | QColor

    missing = Qt.gray
    findPreview = QColor(126, 177, 229)  # Qt.blue
    preview = QColor(110, 195, 144)  # Qt.green
    image = QColor(238, 195, 59)  # Qt.yellow


class Usage(enum.IntEnum):
    discarded = 0
    unset = 1
    selected = 2


class TransformState(enum.IntEnum):
    def __new__(cls, penStyle: Qt.PenStyle):
        value = len(cls.__members__)
        obj = int.__new__(cls, value)
        obj._value_ = value
        obj.penStyle = penStyle
        return obj

    penStyle: Qt.PenStyle

    original = Qt.DotLine
    changed = Qt.SolidLine
    locked = Qt.SolidLine


class Visualization(enum.Enum):
    none = enum.auto()
    asPoint = enum.auto()
    asImage = enum.auto()


class InversionEffect(QGraphicsEffect):
    def draw(self, painter):
        pixmap, offset = self.sourcePixmap(Qt.DeviceCoordinates)
        img = pixmap.toImage()
        img.invertPixels()
        painter.setWorldTransform(QTransform())
        painter.drawPixmap(offset, QPixmap.fromImage(img))


class AerialObject(QObject):

    __timerId: int | None = None

    def __init__(self, scene: map_scene.MapScene, posScene: QPointF, imgId: str, meta, db: sqlite3.Connection):
        super().__init__()
        point = AerialPoint()
        image = AerialImage(imgId, posScene, meta, point, db, self)
        self.__point: Final = weakref.ref(point)
        self.image: Final = weakref.ref(image)
        point.setImage(image)
        image.setVisible(False)
        scene.contrastEnhancementChanged.connect(image.setContrastEnhancement)
        scene.visualizationChanged.connect(self.__setVisualization)
        scene.highlightAerials.connect(self.__highlight)
        scene.showAsImage.connect(self.__showAsImage)
        toolTip = [f'<tr><td>{name}</td><td>{value}</td></tr>' for name, value in meta._asdict().items()]
        toolTip = ''.join(['<table>'] + toolTip + ['</table>'])
        for el in point, image:
            el.setToolTip(toolTip)
            effect = InversionEffect()
            effect.setEnabled(False)
            el.setGraphicsEffect(effect)
            # Add the items to the scene only now, such that they have not emitted scene signals during their setup.
            scene.addItem(el)

    def timerEvent(self, event) -> None:
        for item in (self.image(), self.__point()):
            if item:
                if effect := item.graphicsEffect():
                    effect.setEnabled(not effect.isEnabled())

    # end of overrides

    def isAnimated(self) -> bool:
        return self.__timerId is not None

    @pyqtSlot(dict, dict, set)
    def __setVisualization(self, usages: dict[Usage, bool], visualizations: dict[Availability, Visualization], filteredImageIds: set[str]):
        if image := self.image():
            usageIsOn = usages.get(image.usage())
            visualization = visualizations.get(image.availability())
            if usageIsOn is None or visualization is None:
                return
            isFiltered = not filteredImageIds or image.id() in filteredImageIds
            image.setVisible(visualization == Visualization.asImage and usageIsOn and isFiltered)
            if point := self.__point():
                point.setVisible(visualization == Visualization.asPoint and usageIsOn and isFiltered)

    @pyqtSlot(set)
    def __highlight(self, imgIds) -> None:
        image = self.image()
        if image and image.id() in imgIds:
            for item in (image, self.__point()):
                if item and item.isVisible():
                    item.setFocus()
            # animate
            if self.__timerId is None:
                self.__timerId = self.startTimer(500)
                self.__updateZValues()
        else:
            # stop animation
            if self.__timerId is not None:
                self.killTimer(self.__timerId)
                self.__timerId = None
                for item in (image, self.__point()):
                    if item:
                        if effect := item.graphicsEffect():
                            effect.setEnabled(False)
                self.__updateZValues()

    @pyqtSlot(str, bool)
    def __showAsImage(self, imgId, show) -> None:
        image = self.image()
        point = self.__point()
        if image and image.id() == imgId and point:
            image.setVisible(show)
            point.setVisible(not show)
            focusItem = image if show else point
            focusItem.setFocus(Qt.OtherFocusReason)

    def __updateZValues(self) -> None:
        if image := self.image():
            updateZValue(image)
        if point := self.__point():
            updateZValue(point)


class AerialPoint(QGraphicsEllipseItem):

    def __init__(self, radius: float = 7):
        super().__init__(-radius, -radius, radius * 2, radius * 2)
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        self.setFlag(QGraphicsItem.ItemIsFocusable)
        self.setFlag(QGraphicsItem.ItemClipsChildrenToShape)
        self.setCursor(Qt.PointingHandCursor)
        self.setCacheMode(QGraphicsItem.DeviceCoordinateCache)
        self.__transformState = TransformState.original
        self.__cross: Final = _makeOverlay('cross', self)
        self.__tick: Final = _makeOverlay('tick', self)
        self.__image: weakref.ref | None = None

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, v):
        if change == QGraphicsItem.ItemVisibleHasChanged:
            if scene := cast(map_scene.MapScene, self.scene()):
                scene.addAerialsVisible.emit(1 if v else -1)
        return super().itemChange(change, v)

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.setVisible(False)
            if image := self.image():
                image.setVisible(True)
                image.setFocus(Qt.OtherFocusReason)
        else:
            super().mouseDoubleClickEvent(event)

    def sceneEvent(self, event: QEvent) -> bool:
        if event.type() != QEvent.WhatsThis:
            return super().sceneEvent(event)
        whatsThis = 'An aerial image shown as point. Double-click to open.'
        QWhatsThis.showText(cast(QHelpEvent, event).globalPos(), whatsThis)
        return True

    def focusInEvent(self, event: QFocusEvent) -> None:
        self.__setPen()
        updateZValue(self)
        super().focusInEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self.__setPen()
        updateZValue(self)
        super().focusOutEvent(event)

    # end of overrides

    def setImage(self, image: AerialImage) -> None:
        self.__image = weakref.ref(image)
        self.setAvailability(image.availability())
        self.setUsage(image.usage())
        self.setTransformState(image.transformState())
        updateZValue(self)

    def image(self) -> AerialImage | None:
        if self.__image is not None:
            return self.__image()

    def setAvailability(self, availability: Availability) -> None:
        self.setBrush(QBrush(availability.color))

    def setUsage(self, usage: Usage) -> None:
        self.__cross.setVisible(usage == Usage.discarded)
        self.__tick.setVisible(usage == Usage.selected)

    def setTransformState(self, transformState: TransformState) -> None:
        self.__transformState = transformState
        self.__setPen()

    def __setPen(self) -> None:
        self.setPen(QPen(QColor(162, 17, 17) if self.__transformState == TransformState.locked else Qt.black,
                         3 if self.hasFocus() else 2,
                         self.__transformState.penStyle))


class AerialImage(QGraphicsPixmapItem):

    __pixMapWidth: Final = 3000  # Approx. width of a microfilm scan, it seems.

    __rotateCursor: Final = QCursor(QPixmap(':/plugins/selorecon/rotate'))

    __transparencyCursor: Final = QCursor(QPixmap(':/plugins/selorecon/eye'))

    __threadPool: futures.ThreadPoolExecutor | None = None

    # To be set beforehand by the scene:

    imageRootDir: Path

    previewRootDir: Path

    scaleCartesian2map: float

    @staticmethod
    def createTables(db: sqlite3.Connection) -> None:
        db.execute('''
            CREATE TABLE IF NOT EXISTS usages
            (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL
            ) ''')
        db.executemany(
            'INSERT OR IGNORE INTO usages(id, name) VALUES( ?, ? )',
            ((el, el.name) for el in Usage))
        db.execute('''
            CREATE TABLE IF NOT EXISTS aerials
            (
                id TEXT PRIMARY KEY NOT NULL,  -- <sortie>/<bildnr>.ecw
                usage INT NOT NULL REFERENCES usages(id),
                scenePos TEXT NOT NULL,
                trafo TEXT NOT NULL,
                trafoLocked INT NOT NULL DEFAULT 0,
                path TEXT,                     -- Relative to imageRootDir if previewRect is NULL else to previewRootDir.
                previewRect TEXT CHECK(previewRect ISNULL OR path NOTNULL),
                meta TEXT NOT NULL
            ) ''')

    @staticmethod
    def unload():
        if __class__.__threadPool is not None:
            __class__.__threadPool.shutdown(wait=False, cancel_futures=True)

    def __init__(self, imgId: str, pos: QPointF, meta, point: AerialPoint, db: sqlite3.Connection, obj: AerialObject):
        super().__init__()
        self.setFlag(QGraphicsItem.ItemIsMovable)
        self.setFlag(QGraphicsItem.ItemIsFocusable)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges)
        self.setShapeMode(QGraphicsPixmapItem.BoundingRectShape)
        self.setTransformationMode(Qt.SmoothTransformation)
        self.__origPos: Final = pos
        self.__radiusBild: Final[float] = meta.Radius_Bild
        self.__point: Final = point
        self.__opacity: float = 1.
        self.__requestedPixMapParams: tuple[str, QRect, int, ContrastEnhancement] | None  = None
        self.__currentContrast: ContrastEnhancement = ContrastEnhancement.clahe if claheAvailable else ContrastEnhancement.histogram
        self.__futurePixmap: futures.Future | None = None
        self.__futurePixmapLock: Final = threading.Lock()
        self.__lastRequestedFuture: futures.Future | None = None
        self.__lastRequestedFutureLock: Final = threading.Lock()
        self.__db: Final = db
        self.object: Final = obj
        self.__id: Final = imgId
        self.__availability: Availability | None = None
        self.__transformState: TransformState = TransformState.original
        self.__lock: Final = _makeOverlay('lock', self, QGraphicsItem.ItemIgnoresTransformations)
        self.__cross: Final = _makeOverlay('cross', self, QGraphicsItem.ItemIgnoresTransformations)
        self.__tick: Final = _makeOverlay('tick', self, QGraphicsItem.ItemIgnoresTransformations)

        if row := db.execute('SELECT usage, scenePos, trafo, trafoLocked FROM aerials WHERE id == ?', [imgId]).fetchone():
            usage = Usage(row[0])
            self.setPos(QPointF(*json.loads(row[1])))
            self.setTransform(QTransform(*json.loads(row[2])))
            if row[3]:
                trafoState = TransformState.locked
            elif self.transform() == self.__originalTransform() and self.pos() == self.__origPos:
                trafoState = TransformState.original
            else:
                trafoState = TransformState.changed
            self.__setTransformState(trafoState)
        else:
            def toJson(value):
                if isinstance(value, datetime.date):
                    return str(value)
                raise TypeError(f'Unable to encode type {value.__class__}')

            db.execute(
                'INSERT INTO aerials (id, usage, scenePos, trafo, path, meta) VALUES(?, ?, ?, ?, ?, ?)',
                [imgId,
                 Usage.unset,
                 json.dumps([pos.x(), pos.y()]),
                 json.dumps(np.eye(3).ravel().tolist()),
                 imgId if (__class__.imageRootDir / imgId).exists() else None,
                 json.dumps(meta._asdict(), default=toJson)])
            usage = Usage.unset
            self.__resetTransform()
        self.__deriveAvailability()
        self.__setUsage(usage)
        self.__setPixMap()

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, v):
        if change == QGraphicsItem.ItemVisibleHasChanged:
            if v:
                self.__requestPixMap()
            if scene := self.scene():
                scene.addAerialsVisible.emit(1 if v else -1)
        elif change == QGraphicsItem.ItemPositionHasChanged:
            self.__point.setPos(v)
            self.__db.execute(
                'UPDATE aerials SET scenePos = ? WHERE id == ?',
                [json.dumps([v.x(), v.y()]), self.__id])
            if scene := self.scene():
                scene.aerialFootPrintChanged.emit(self.__id, self.footprint())
            self.__setTransformState(TransformState.changed)
        elif change == QGraphicsItem.ItemTransformHasChanged:
            self.__db.execute(
                'UPDATE aerials SET trafo = ? WHERE id == ?',
                [json.dumps([
                    v.m11(), v.m12(), v.m13(),
                    v.m21(), v.m22(), v.m23(),
                    v.m31(), v.m32(), v.m33()]), self.__id])
            if scene := self.scene():
                scene.aerialFootPrintChanged.emit(self.__id, self.footprint())
            self.__setTransformState(TransformState.changed)
        return super().itemChange(change, v)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if not self.hasFocus():
            # Prevent this from being moved just because another item on top has ignored the event.
            return event.ignore()
        isMovable = self.flags() & QGraphicsItem.ItemIsMovable
        if event.button() == Qt.LeftButton:
            if event.modifiers() & Qt.AltModifier:
                self.__opacity = self.opacity()
                self.setOpacity(0)
            elif isMovable:
                self.setCursor(Qt.ClosedHandCursor)
            else:
                event.ignore()
        if isMovable:
            # Otherwise, super ignores the event, and so I would not receive a corresp. release event.
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.setOpacity(self.__opacity)
        self.__chooseCursor(event)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.setVisible(False)
            self.__point.setVisible(True)
            self.__point.setFocus(Qt.OtherFocusReason)
        else:
            super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QGraphicsSceneWheelEvent) -> None:
        if self.availability() < Availability.preview or not self.hasFocus():
            # Prevent self from being zoomed only because an item on top has ignored the event.
            return event.ignore()
        numSteps = event.delta() / 8 / 15
        if event.modifiers() & Qt.ShiftModifier:
            numSteps /= 10
        if event.modifiers() & Qt.AltModifier:
            self.__opacity = min(max(self.opacity() - numSteps * .1, .3), 1.)
            self.setOpacity(self.__opacity)
            return
        if not self.flags() & QGraphicsItem.ItemIsMovable:
            return event.ignore()
        pos = event.pos()  # in units of image pixels; ignores self.offset() i.e. (0, 0) is the image center.
        x, y = pos.x(), pos.y()
        if not event.modifiers() & Qt.ControlModifier:
            # self.mapToScene(pt) seems to return:
            # self.transform().map(pt) + self.scenePos()
            # , where self.scenePos() == self.pos() for top-level items.
            scale = 1.1 ** numSteps
            # trafo = QTransform.fromTranslate(x, y).scale(scale, scale).translate(-x, -y)
            trafo = QTransform.fromTranslate(x * (1-scale), y * (1-scale)).scale(scale, scale)
            # or equivalently, using standard matrix multiplications:
            # trafo = QTransform.fromTranslate(-x, -y) * QTransform.fromScale(scale, scale) * QTransform.fromTranslate(x, y)
        else:
            angle = numSteps * 10
            trafo = QTransform.fromTranslate(x, y).rotate(angle).translate(-x, -y)
        # Note: since Qt multiplies points on their right, self.transform() gets applied last.
        combined = trafo * self.transform()
        # self.pos() is my position in parent's (scene) coordinates, which is added to the result of self.transform().map
        # Let's make self.transform() map the origin of item coordinates to (0, 0) in parent (scene) coordinates,
        # and move self.pos() accordingly, so the position of my AerialPoint will move to self.pos() as well.
        # Hence, do not simply do:
        # self.setTransform(combined)
        # or equivalently:
        # self.setTransform(trafo, combine=True)

        # Multiplies QPointF on the left of the 3x3 transformation matrix (in homogeneous coordinates).
        origin = combined.map(QPointF(0., 0.))
        combined *= QTransform.fromTranslate(-origin.x(), -origin.y())
        self.setTransform(combined)
        self.moveBy(origin.x(), origin.y())
        # or equivalently:
        # self.setPos(self.pos() + origin)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        self.__chooseCursor(event)
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        # Note: if QMainWindow has a QMenuBar, then only every other release of the Alt key lands here.
        # Qt Designer may set a QMenuBar in .ui
        self.__chooseCursor(event)
        super().keyReleaseEvent(event)

    def sceneEvent(self, event: QEvent) -> bool:
        if event.type() != QEvent.WhatsThis:
            return super().sceneEvent(event)
        whatsThis = '''
<h4>An aerial image shown as such.</h4>

Pan using the left mouse button.<br/>

Use mouse wheel to scale the image under the mouse cursor.<br/>

Use mouse wheel + Ctrl to rotate it under the mouse cursor.<br/>

Hold Shift to slow down zoom and rotation.<br/>

Use mouse wheel + Alt to control transparency.<br/>

Hold left mouse button + Alt to temporally hide the image.<br/>

Double-click to close.<br/>
'''
        QWhatsThis.showText(cast(QHelpEvent, event).globalPos(), whatsThis)
        return True

    def focusInEvent(self, event: QFocusEvent) -> None:
        updateZValue(self)
        super().focusInEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        updateZValue(self)
        super().focusOutEvent(event)

    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent) -> None:
        menu = QMenu('menu')
        menu.setToolTipsVisible(True)
        if self.__availability in (Availability.findPreview, Availability.preview):
            menu.addAction(QIcon(':/plugins/selorecon/image-crop'), 'Find preview', lambda: self.__findPreview())
        menu.addSection('Usage')
        usage = self.usage()
        if usage != Usage.unset:
            menu.addAction(QIcon(':/plugins/selorecon/selection'),
                           'Unset', lambda: self.__setUsage(Usage.unset))
        if usage != Usage.selected:
            menu.addAction(QIcon(':/plugins/selorecon/tick'),
                           'Select', lambda: self.__setUsage(Usage.selected))
        if usage != Usage.discarded:
            menu.addAction(QIcon(':/plugins/selorecon/cross'),
                           'Discard', lambda: self.__setUsage(Usage.discarded))
        menu.addSeparator()
        if self.__transformState == TransformState.locked:
            menu.addAction(QIcon(':/plugins/selorecon/unlock'), 'Unlock transform',
                           lambda: self.__setTransformState(TransformState.original if (self.transform() == self.__originalTransform() and self.pos() == self.__origPos) else TransformState.changed))
        elif self.flags() & QGraphicsItem.ItemIsMovable:
            if self.__availability in (Availability.image, ):  # TODO Availability.preview
                menu.addAction(QIcon(':/plugins/selorecon/magnet'), 'Auto-georeference', self.__georeference)
            menu.addAction(QIcon(':/plugins/selorecon/lock'), 'Lock transform', lambda: self.__setTransformState(TransformState.locked))
        if self.__transformState == TransformState.changed:
            menu.addAction(QIcon(':/plugins/selorecon/home'), 'Reset transform', self.__resetTransform)
        menu.exec(event.screenPos())

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget) -> None:
        pm = None
        with self.__futurePixmapLock:
            if self.__futurePixmap is not None:
                pm = self.__futurePixmap.result()  # result() might raise here, in the wanted thread.
                self.__futurePixmap = None
        if pm is not None:
            self.__setPixMap(pm)
        super().paint(painter, option, widget)
        painter.save()
        # Qt 5.15 docs for QGraphicsItem::paint say:
        #   "QGraphicsItem does not support use of cosmetic pens with a non-zero width."
        # But obviously, it does support them, at least on Windows.
        width = 2 if option.state & QStyle.State_HasFocus else 1
        assert self.__availability is not None
        pen = QPen(self.__availability.color, width, self.__transformState.penStyle)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawRect(self.boundingRect())
        painter.restore()

    def scene(self) -> map_scene.MapScene:
        return cast(map_scene.MapScene, super().scene())

    # end of overrides

    def __setPixMap(self, pm: QPixmap | None = None):
        if pm is None:
            pixMapWidth = __class__.__pixMapWidth
            path, previewRect = self.__db.execute('SELECT path, previewRect FROM aerials WHERE id == ?',
                                                  [self.__id]).fetchone()
            if previewRect:
                width, height, rotation = json.loads(previewRect)[2:]
                if rotation % 2:
                    width, height = height, width
            elif path:
                with GdalPushLogHandler():
                    ds = gdal.Open(str(__class__.imageRootDir / path))
                    width, height = ds.RasterXSize, ds.RasterYSize
            else:
                width, height = [pixMapWidth] * 2
            pm = QBitmap(pixMapWidth, _pixMapHeightFor(pixMapWidth, QSize(width, height)))
            pm.fill(Qt.color1)
        origPm = self.pixmap()
        self.setPixmap(pm)
        self.setOffset(-pm.width() / 2, -pm.height() / 2)
        if origPm.size() != pm.size():
            if scene := self.scene():
                scene.aerialFootPrintChanged.emit(self.__id, self.footprint())

    def __requestPixMap(self):
        path, previewRect = self.__db.execute('SELECT path, previewRect FROM aerials WHERE id == ?',
                                              [self.__id]).fetchone()
        if previewRect is None:
            rotationCcw = 0
            previewRect = QRect()
        else:
            *rect, rotationCcw = json.loads(previewRect)
            previewRect = QRect(*rect)
        if self.__availability in (Availability.preview, Availability.image):
            if not self.__requestedPixMapParams or self.__requestedPixMapParams != (path, previewRect, rotationCcw, self.__currentContrast):
                if __class__.__threadPool is None:
                    __class__.__threadPool = futures.ThreadPoolExecutor(thread_name_prefix='AerialReader')
                absPath = __class__.imageRootDir / path if previewRect.isNull() else __class__.previewRootDir / path
                future = __class__.__threadPool.submit(_getPixMap, absPath, __class__.__pixMapWidth,
                                                       previewRect, rotationCcw, self.__currentContrast)
                future.add_done_callback(self.__pixMapReady)
                with self.__lastRequestedFutureLock:
                    if self.__lastRequestedFuture:
                        self.__lastRequestedFuture.cancel()
                    self.__lastRequestedFuture = future
        self.__requestedPixMapParams = path, previewRect, rotationCcw, self.__currentContrast

    def __pixMapReady(self, future: futures.Future) -> None:
         # This is called from a worker thread.
        with self.__lastRequestedFutureLock:
            if self.__lastRequestedFuture is not future:
                # Another pixmap has been requested after this one.
                # Still, this one has been received after the other one.
                # This is possible only if they were computed in different worker threads of the pool,
                # and it is more probable if the computation of this pixmap has been more elaborate.
                return
        with self.__futurePixmapLock:
            self.__futurePixmap = future
        self.update()

    def setContrastEnhancement(self, contrast: ContrastEnhancement):
        self.__currentContrast = contrast
        if self.isVisible():
            self.__requestPixMap()

    def availability(self) -> Availability:
        assert self.__availability is not None
        return self.__availability

    def __deriveAvailability(self) -> None:
        path, rect = self.__db.execute('SELECT path, previewRect FROM aerials WHERE id == ?', [self.__id]).fetchone()
        if path is None:
            filmDir = self.previewRootDir / Path(self.__id).parent
            availability = Availability.findPreview if filmDir.exists() else Availability.missing
        else:
            availability = Availability.image if rect is None else Availability.preview
        if self.__availability != availability:
            if scene := self.scene():
                absPath = ''
                if path is not None:
                    absPath = str(__class__.previewRootDir / path if rect else __class__.imageRootDir / path)
                scene.aerialAvailabilityChanged.emit(self.__id, int(availability), absPath)
        self.__availability = availability
        self.__point.setAvailability(availability)
        self.__setMovability()

    def __setMovability(self) -> None:
        # __init__: self.__availability is None; __setMovability will be called again right after, via __deriveAvailability.
        availability = self.__availability or Availability.missing
        self.setFlag(QGraphicsItem.ItemIsMovable, availability >= Availability.preview and self.__transformState != TransformState.locked)

    def usage(self) -> Usage:
        value, = self.__db.execute(
            'SELECT usage FROM aerials WHERE id == ?',
            [self.__id]).fetchone()
        return Usage(value)

    def __setUsage(self, usage: Usage) -> None:
        self.__cross.setVisible(usage == Usage.discarded)
        self.__tick.setVisible(usage == Usage.selected)
        self.__point.setUsage(usage)
        if scene := self.scene():
            scene.aerialUsageChanged.emit(self.__id, int(usage))
        self.__db.execute(
            'UPDATE aerials SET usage = ? WHERE id == ?',
            [usage, self.__id])

    def transformState(self) -> TransformState:
        return self.__transformState

    def __setTransformState(self, transformState: TransformState) -> None:
        self.__transformState = transformState
        isLocked = transformState == TransformState.locked
        self.__db.execute(
            'UPDATE aerials SET trafoLocked = ? WHERE id == ?',
            [isLocked, self.__id])
        self.__lock.setVisible(isLocked)
        self.__setMovability()
        updateZValue(self)
        self.__point.setTransformState(transformState)

    def __originalTransform(self) -> QTransform:
        scale = self.__radiusBild * __class__.scaleCartesian2map / (__class__.__pixMapWidth / 2)
        # Actually, 2 times this scale seems a bit closer to the true scale.
        return QTransform.fromScale(scale, scale)

    def __resetTransform(self):
        self.setTransform(self.__originalTransform())
        self.setPos(self.__origPos)
        self.__setTransformState(TransformState.original)

    def __chooseCursor(self, event: QKeyEvent | QGraphicsSceneMouseEvent):
        if event.modifiers() & Qt.AltModifier:
            self.setCursor(self.__transparencyCursor)
        elif event.modifiers() & Qt.ControlModifier and self.flags() & QGraphicsItem.ItemIsMovable:
            self.setCursor(self.__rotateCursor)
        else:
            self.unsetCursor()

    def __findPreview(self):
        filmDir = self.previewRootDir / Path(self.__id).parent
        dialog = PreviewWindow(filmDir, Path(self.__id).stem)
        if dialog.exec() == QDialog.Accepted:
            path, rect, viewRotationCcw = dialog.selection()
            self.__db.execute(
                'UPDATE aerials SET path = ?, previewRect = ? WHERE id == ?',
                [str(path.relative_to(__class__.previewRootDir)),
                 json.dumps([rect.left(), rect.top(), rect.width(), rect.height(), viewRotationCcw]),
                 self.__id])
            self.__deriveAvailability()
            self.__requestPixMap()

    def __georeference(self):
        # Pass current aerial orientation as GDAL transform
        pos = self.pos()
        tr: QTransform = self.transform()
        transform = np.array([[tr.m11(), tr.m12(), tr.m13()],
                              [tr.m21(), tr.m22(), tr.m23()],
                              [tr.m31(), tr.m32(), tr.m33()]])
        assert abs(transform[2, :] - (0, 0, 1)).max() < 1.e-7
        assert abs(transform[:, 2] - (0, 0, 1)).max() < 1.e-7
        # Top/left image corner in scene CS.
        # Same as: transform[:2, :2].T @ self.offset() + self.pos()
        topLeft = self.mapToScene(self.offset())
        gdalTrafo = np.zeros((2, 3))
        gdalTrafo[:, 0] = topLeft.x(), topLeft.y()
        gdalTrafo[:, 1:] = transform[:2, :2].T  # Qt actually uses the transpose.
        path, previewRect = self.__db.execute('SELECT path, previewRect FROM aerials WHERE id == ?',
                                              [self.__id]).fetchone()
        assert previewRect is None
        with GdalPushLogHandler():
            ds = gdal.Open(str(__class__.imageRootDir / path))
            gdalTrafo[:, 1:] *= __class__.__pixMapWidth / ds.RasterXSize  # display -> native resolution.
            gdalTrafo[1, :] *= -1.  # Scene -> WCS
            try:
                gdalTrafo, aerialPts, orthoPts = georef(ds, gdalTrafo)
            except:
                return logger.exception('Automatic georeferencing failed.')
        scaleNative2display = __class__.__pixMapWidth / ds.RasterXSize
        aerialPts *= scaleNative2display
        orthoPts *= scaleNative2display
        off = np.array([self.offset().x(), self.offset().y()])
        aerialPts += off
        orthoPts += off
        ptRadius = 3
        ptPen = QPen(Qt.magenta, 1)
        ptBrush = QBrush(Qt.magenta)
        # Must not set QGraphicsItem.ItemIgnoresTransformations on the lines, or their rotations and lengths will be wrong.
        # To still result in lines with a width of 2px on screen, adapt it.
        # Since the view cannot be changed while the lines are displayed, this static width will always be displayed as wanted.
        linePen = QPen(Qt.cyan, 2. / self.deviceTransform(self.scene().views()[0].viewportTransform()).determinant() ** .5)
        items = []
        for aerialPt, orthoPt in zip(aerialPts, orthoPts, strict=True):
            pt = QGraphicsEllipseItem(-ptRadius, -ptRadius, 2 * ptRadius, 2 * ptRadius, self)
            pt.setFlag(QGraphicsItem.ItemIgnoresTransformations)
            pt.setPos(*aerialPt)
            pt.setPen(ptPen)
            pt.setBrush(ptBrush)
            line = QGraphicsLineItem(*aerialPt, *orthoPt, self)
            line.setPen(linePen)
            items.extend((pt, line))
        gdalTrafo[1, :] *= -1.  # WCS -> Scene
        gdalTrafo[:, 1:] *= 1. / scaleNative2display
        newPos = gdalTrafo[:, 0] + gdalTrafo[:, 1:] @ -off
        newTr = gdalTrafo[:, 1:].T
        newTr = QTransform(newTr[0, 0], newTr[0, 1], newTr[1, 0], newTr[1, 1], 0., 0.)
        # These will call self.itemChange, update point's position and store the new orientation in the DB.
        self.setPos(*newPos)
        self.setTransform(newTr)
        shift = (pos.x(), pos.y()) - newPos
        shift = np.sum(shift ** 2) ** .5
        scale = (np.linalg.det(gdalTrafo[:, 1:].T) / np.linalg.det(transform[:2, :2])) ** .5
        msgs = [f'{len(aerialPts)} homologous points', f'Shift: {shift:.2f}m', f'Scale: {scale:.2f}']
        logger.info(f'{Path(path).name} georeferenced: ' + '; '.join(msgs))
        button = QMessageBox.question(self.scene().views()[0], 'Automatic Georeferencing Results', '\n'.join(msgs) + '\nAccept?')
        if button == QMessageBox.No:
            self.setPos(pos)
            self.setTransform(tr)
        for item in items:
            item.setParentItem(None)
            self.scene().removeItem(item)

    def id(self):
        return self.__id

    def footprint(self):
        # CS QGraphicsScene -> WCS: invert y-coordinate
        return [{'x': pt.x(), 'y': -pt.y()} for pt in self.mapToScene(self.boundingRect())[:-1]]

    def radiusBild(self) -> float:
        return self.__radiusBild


def _pixMapHeightFor(width: int, size: QSize) -> int:
    return round(size.height() / size.width() * width)

def _getPixMap(path: Path, width: int, rect: QRect, rotationCcw: int, contrast: ContrastEnhancement):
    with GdalPushLogHandler():
        ds = gdal.Open(str(path))
        if rect.isNull():
            rect = QRect(0, 0, ds.RasterXSize, ds.RasterYSize)
        height = _pixMapHeightFor(width, rect.size())
        img = QImage(width, height, QImage.Format_RGBA8888)
        img.fill(Qt.white)
        ptr = img.bits()
        ptr.setsize(img.sizeInBytes())
        assert ds.RasterCount in (1, 3)
        iBands = [1] * 3 if ds.RasterCount == 1 else [1, 2, 3]
        ds.ReadRaster1(rect.left(), rect.top(), rect.width(), rect.height(),
                       width, height, gdal.GDT_Byte, iBands,
                       buf_pixel_space=4, buf_line_space=width * 4, buf_band_space=1,
                       resample_alg=gdal.GRIORA_Gauss,
                       inputOutputBuf=ptr)
    if rotationCcw != 0:
        #arr = np.ndarray(shape=(img.height(), img.width(), 4), dtype=np.uint8, buffer=ptr)
        #rotated = np.rot90(arr, k=rotationCcw)
        #linear = arr.reshape(-1)
        #linear[:] = rotated.reshape(-1)
        # Cannot reshape a (rectangular) QImage ...
        # So use QImage directly:
        # "Rotates the coordinate system counterclockwise by the given angle. The angle is specified in degrees."
        img = img.transformed(QTransform().rotate(-90 * rotationCcw))
    enhanceContrast(img, contrast)
    return QPixmap.fromImage(img)

def _makeOverlay(name: str, parent: QGraphicsItem, flag: QGraphicsItem.GraphicsItemFlag | None = None):
    pm = QPixmap(':/plugins/selorecon/' + name)
    item = QGraphicsPixmapItem(pm, parent)
    item.setOffset(-pm.width() / 2, -pm.height() / 2)
    item.setTransformationMode(Qt.SmoothTransformation)
    if flag is not None:
        item.setFlag(flag)
    return item


"""
Rules for z-stacking, from top to bottom:
- the focus item (controlled by map view; there can be at most one focus item at a time) and animated items (controlled by web view / JavaScript);
- images with original or changed transformation;
- points;
- images with locked transformation
Display aerials with small footprints above those with large ones if they belong to the same group above.
These rules shall make it easy to orient additional, large scale images using already oriented small scale images as background.
"""
def updateZValue(item: AerialImage | AerialPoint) -> None:
    isImage = isinstance(item, AerialImage)
    image = item if isImage else item.image()
    isLockedImage = isImage and image and image.transformState() == TransformState.locked
    object = image.object if image else None
    if item.hasFocus() or object and object.isAnimated():
        level = 3
    elif isImage and not isLockedImage:
        level = 2
    elif not isImage:
        level = 1
    else:
        level = 0
    scale = 1.  # 0 <= scale <= 1
    if image:
        scale = 1 / image.radiusBild()  # do not hassle around with the adjusted scale and image resolution.
    nextScale = 2
    item.setZValue(nextScale * level + scale)
