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
"""
from __future__ import annotations

from qgis.PyQt.QtCore import pyqtSignal, pyqtSlot, QObject, Qt, QUrl
from qgis.PyQt.QtGui import QKeyEvent
from qgis.PyQt.QtWidgets import QDialog, QGridLayout
from qgis.PyQt.QtWebKit import QWebSettings
from qgis.PyQt.QtWebKitWidgets import QWebInspector, QWebPage, QWebView

import functools
import http
import http.server
# must not import logging before PyQt, or logging will fail within pydevd!
import logging
from pathlib import Path
import threading
import urllib.parse
import urllib.request

showWeb = True
webInspectorSupport = False

logger = logging.getLogger(__name__)
httpdLogger = logging.getLogger(__name__ + '.httpd')


class WebView(QWebView):

    # inbound
    aerialsLoaded = pyqtSignal(list)
    attackDataLoaded = pyqtSignal(list)
    areaOfInterestLoaded = pyqtSignal(list)
    aerialFootPrintChanged = pyqtSignal(str, list)
    aerialAvailabilityChanged = pyqtSignal(str, int, str)
    aerialUsageChanged = pyqtSignal(str, int)

    # outbound
    filterAerials = pyqtSignal(set)
    highlightAerials = pyqtSignal(set)
    showAsImage = pyqtSignal(str, bool)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setWhatsThis('Hit F5 to re-load.' + (' Hit F4 to open Web Inspector.' if webInspectorSupport else ''))
        self.__httpd: http.server.HTTPServer | None = None
        self.__webInspectorDialog = None

        # Expose a QObject to JavaScript, to receive signals from there (Qt WebKit Bridge).
        self.__exposedToWebJavaScript = ExposedToWebJavaScript()

        if not showWeb:
            return

        if webInspectorSupport:
            self.settings().setAttribute(QWebSettings.WebAttribute.DeveloperExtrasEnabled, True)

        self.__createHttpd()

        # Redirecting the JavaScript console needs QWebPage to be sub-classed.
        # But with a sub-classed web page, the web inspector no longer works.
        if webInspectorSupport:
            page = self.page()
            # This seems to create the action and make it available in the context menu.
            _ = page.action(QWebPage.InspectElement).isEnabled()
        else:
            page = WebPage(self)
            self.setPage(page)

        frame = page.mainFrame()
        for ori in Qt.Orientation.Horizontal, Qt.Orientation.Vertical:
            frame.setScrollBarPolicy(ori, Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        frame.javaScriptWindowObjectCleared.connect(self.__onWebJavaScriptWindowObjectCleared)

        self.aerialsLoaded.connect(self.__exposedToWebJavaScript.aerialsLoaded)
        self.attackDataLoaded.connect(self.__exposedToWebJavaScript.attackDataLoaded)
        self.areaOfInterestLoaded.connect(self.__exposedToWebJavaScript.areaOfInterestLoaded)
        self.aerialFootPrintChanged.connect(self.__exposedToWebJavaScript.aerialFootPrintChanged)
        self.aerialAvailabilityChanged.connect(self.__exposedToWebJavaScript.aerialAvailabilityChanged)
        self.aerialUsageChanged.connect(self.__exposedToWebJavaScript.aerialUsageChanged)

        self.__exposedToWebJavaScript.filterAerials.connect(self.__filterAerials)
        self.__exposedToWebJavaScript.highlightAerials.connect(self.__highlightAerials)
        self.__exposedToWebJavaScript.showAsImage.connect(self.showAsImage)

        assert self.__httpd is not None
        #self.setUrl(QUrl.fromLocalFile(str(Path(__file__).parent / 'VisAnPrototype/index.html')))
        self.setUrl(QUrl(f'http://localhost:{self.__httpd.server_port}/'))
        # self.setUrl(QUrl('https://webkit.org/blog-files/webgl/SpiritBox.html'))
        # self.setUrl(QUrl('https://p5js.org/examples/hello-p5-animation.html'))

        # Provide the option to inspect the web page with QWebInspector.
        # If the visualization did not suppress the context menu, then this could simply be:
        # set QWebSettings.WebAttribute.DeveloperExtrasEnabled above.
        # The context menu would then show an entry to reload the page, and another one to open the web inspector.
        # However, p5's orbitControl suppresses the context menu, so the right mouse button can be used for panning.
        # Without the context menu, providing QWebInspector is more complicated.
        # For speed, create it on demand in __onWebInspect.

        # These would be dialog-wide.
        # # F4 - show web inspector
        # shortcut = QShortcut(self)
        # shortcut.setContext(Qt.ApplicationShortcut)
        # shortcut.setKey(Qt.Key_F4)
        # shortcut.activated.connect(self.__onWebInspect)

        # #F5 - reload page
        # shortcut = QShortcut(self)
        # shortcut.setKey(Qt.Key_F5)
        # #shortcut.activated.connect(self.reload)
        # shortcut.activated.connect(lambda: self.page().triggerAction(QWebPage.WebAction.ReloadAndBypassCache))

    def unload(self) -> None:
        if self.__httpd is not None:
            logger.debug('Shutting down httpd ...')
            self.__httpd.shutdown()
            logger.debug('httpd shut down.')

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.__httpd is not None:
            if event.key() == Qt.Key_F4:
                # show web inspector
                return self.__onWebInspect()
            if event.key() == Qt.Key_F5:
                # reload page
                return self.page().triggerAction(QWebPage.WebAction.ReloadAndBypassCache)
        super().keyPressEvent(event)

    def __createHttpd(self) -> None:
        # https://gist.github.com/kwk/5387c0e8d629d09f93665169879ccb86
        directory = (Path(__file__).parent / 'VisAnPrototype').resolve(strict=True)
        Handler = functools.partial(RequestHandler, directory=directory)
        # Pass port=0 to let the OS choose an unused port. This fails sometimes with high port numbers.
        # So pass a specific port that works and is hopefully unused.
        port = 8010
        self.__httpd = http.server.HTTPServer(('localhost', port), Handler)

        def serve_forever():
            assert self.__httpd is not None
            try:
                with self.__httpd:
                    self.__httpd.serve_forever()
                self.__httpd = None
            except Exception:
                logger.exception('httpd failed: ')
                raise

        thread = threading.Thread(target=serve_forever, daemon=True, name='httpd')
        thread.start()
        url = f'http://localhost:{self.__httpd.server_port}'
        with urllib.request.urlopen(url + '/index.html') as response:
            assert response.status == http.HTTPStatus.OK

        logger.debug(f'{thread.name} serves {directory} at {url}')

    @pyqtSlot()
    def __onWebInspect(self) -> None:
        if self.__webInspectorDialog is None:
            webInspector = QWebInspector(self)
            webInspector.setPage(self.page())
            webInspector.setVisible(True)
            self.__webInspectorDialog = QDialog(self)
            self.__webInspectorDialog.setWindowTitle("Web Inspector")
            self.__webInspectorDialog.resize(950, 400)
            layout = QGridLayout()
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(webInspector)
            self.__webInspectorDialog.setLayout(layout)

        self.__webInspectorDialog.setVisible(not self.__webInspectorDialog.isVisible())

    @pyqtSlot()
    def __onWebJavaScriptWindowObjectCleared(self) -> None:
        self.page().mainFrame().addToJavaScriptWindowObject('qgisplugin', self.__exposedToWebJavaScript)

    @pyqtSlot(list)
    def __filterAerials(self, imgIds) -> None:
        logger.debug(f'filterAerials({imgIds})')
        self.filterAerials.emit(set(imgIds))

    @pyqtSlot(list)
    def __highlightAerials(self, imgIds) -> None:
        logger.debug(f'highlightAerials({imgIds})')
        self.highlightAerials.emit(set(imgIds))


class RequestHandler(http.server.SimpleHTTPRequestHandler):

    def log_message(self, format, *args):
        httpdLogger.debug(format % args)


class WebPage(QWebPage):

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.__logger = logging.getLogger(__name__ + '.javascript')

    def javaScriptConsoleMessage(self, message: str, lineNumber: int, sourceId: str) -> None:
        comps = urllib.parse.urlparse(sourceId)
        sourceId = urllib.parse.urlunparse(('', '', *comps[2:]))
        self.__logger.info(f'{sourceId}:{lineNumber}: {message}')


class ExposedToWebJavaScript(QObject):

    # PlugIn -> Browser
    aerialsLoaded = pyqtSignal(list)
    attackDataLoaded = pyqtSignal(list)
    areaOfInterestLoaded = pyqtSignal(list)
    aerialFootPrintChanged = pyqtSignal(str, list)
    aerialAvailabilityChanged = pyqtSignal(str, int, str)
    aerialUsageChanged = pyqtSignal(str, int)

    # Browser -> PlugIn
    filterAerials = pyqtSignal(list)
    highlightAerials = pyqtSignal(list)
    showAsImage = pyqtSignal(str, bool)
