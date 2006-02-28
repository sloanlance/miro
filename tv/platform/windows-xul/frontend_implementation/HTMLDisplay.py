import app
import threading
import socket
import re
import resource
import xhtmltools
import time
import errno
import os
import config
import util
from util import quoteJS

def execChromeJS(js):
    """Execute some Javascript in the context of the privileged top-level
    chrome window. Queued and delivered via a HTTP-based event
    mechanism; no return value is recovered."""
    httpServer.classLock.acquire()
    try:
        if httpServer.chromeJavascriptStream:
            print "XULJS: exec %s" % js
            httpServer.chromeJavascriptStream.queueChunk("text/plain", js)
        else:
            print "XULJS: queue: %s" % js
            httpServer.chromeJavascriptQueue.append(js)
    finally:
        httpServer.classLock.release()

from frontend_implementation import UIBackendDelegate

###############################################################################
#### HTTP server to deliver pages/events to browsers via XMLHttpRequest    ####
###############################################################################

# document cookie -> (content type, body)
pendingDocuments = {}

# The port we're listening on
serverPort = None
lock = threading.RLock() # and a lock protecting it

def getDTVPlatformName():
    return "xul"

def getServerPort():
    lock.acquire()
    try:
        if serverPort is None:
            # Bring up the server.
            httpListener()

        assert serverPort, "httpListener didn't set the port"
        result = serverPort
    finally:
        lock.release()

    return result

class httpListener:
    def __init__(self):
        global serverPort

        # Create and bind socket; start listening
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(None)
        self.socket.bind( ('127.0.0.1', 0) )
        (myAddr, myPort) = self.socket.getsockname()
        print "httpListener: Listening on %s %s" % (myAddr, myPort)
        assert not serverPort, "Only one httpListener allowed, please"
        serverPort = myPort
        self.socket.listen(63)

        # Kick off the accept loop in a new thread
        thread = threading.Thread(target = self.acceptThread, \
                                  name = "httpListener accept thread")
        thread.setDaemon(True)
        thread.start()

    def acceptThread(self):
        while True:
            (conn, address) = self.socket.accept()
            conn.settimeout(None)
            httpServer(conn)

class httpServer:

    classLock = threading.RLock()
    chromeJavascriptStream = None
    chromeJavascriptQueue = []
    prefJavascriptStream = None
    prefJavascriptQueue = []
    reqNum = 0

    def __init__(self, socket):
        self.socket = socket
        self.file = socket.makefile("rb")
        self.isChunked = False
        self.chunkQueue = []
        self.reqNum = None
        self.cond = threading.Condition()

        # NEEDS: more convincing random ID
        self.boundary = "DTVDTVDTVDTVDTVDTV%s" % (str(id(self)))

        # Kick off a thread that can block waiting for a request to be
        # received
        self.thread = threading.Thread(target = self.requestThread, \
                                       name = "httpServer -- reading request")
        self.thread.setDaemon(True)
        self.thread.start()

    def incReqNum(self):
        ret = -1
        httpServer.classLock.acquire()
        try:
            httpServer.reqNum += 1
            ret = httpServer.reqNum
        finally:
            httpServer.classLock.release()
        return ret

    def requestThread(self):
        request = None
        
        try:
            try:
                request = self.file.readline()

                match = re.match(r"^([^ ]+) +([^ ]+)", request)
                assert match, "Malformed HTTP request"
                method = match.group(1)
                path = match.group(2)
                self.reqNum = self.incReqNum()
                self.thread.setName("httpServer [%d] -- %s" % \
                                    (self.reqNum, path))

                self.handleRequest(method, path)

            # In handling exceptions, remember that reqNum can be None if
            # the initial readline failed -- so use %s, never %d, when
            # printing it.
            except socket.error, (code, description):
                if code == errno.ECONNABORTED or \
                        code == errno.ECONNRESET:
                    # Normal: Mozilla was just being abrupt
                    print "[%s] Ignoring remote or network error '%s'" % \
                        (self.reqNum, description)
                    return
                else:
                    details = "Closing socket; request was [%s] %s" % \
                        (self.reqNum, request)
                    util.failedExn("when answering a request",
                                   details = details)
            except:
                details = "Closing socket; request was [%s] %s" % \
                    (self.reqNum, request)
                util.failedExn("when answering a request", details = details)

        finally:
            self.socket.close()

        # Thread exits at this point

    def handleRequest(self, method, path):
        assert method == 'GET', "Only GET is supported"

        ## Mutator stream ##
        match = re.match("^/dtv/mutators/(.*)", path)
        if match:
            cookie = match.group(1)
            print "[%s @%s] Events" % (self.reqNum, cookie)

            self.beginSendingChunks()
            HTMLDisplay.setMutationOutput(cookie, self)
            self.runChunkPump()
            return

        ## Chrome-context Javascript stream ##
        match = re.match("^/dtv/xuljs", path)
        if match:
            print "[%s] XULJS" % (self.reqNum)

            httpServer.classLock.acquire()
            try:
                assert not httpServer.chromeJavascriptStream, \
                    "There can't be two xuljs's (%d)" % self.reqNum

                self.beginSendingChunks()
                for a in httpServer.chromeJavascriptQueue:
                    print "XULJS: flush %s" % a
                    self.queueChunk("text/plain", a)
                httpServer.chromeJavascriptQueue = []
                httpServer.chromeJavascriptStream = self
            finally:
                httpServer.classLock.release()

            self.runChunkPump()
            return

        ## Chrome-context Preferences Javascript stream ##
        match = re.match("^/dtv/prefjs", path)
        if match:
            print "[%s] PREFJS" % (self.reqNum)

            httpServer.classLock.acquire()
            try:
                assert not httpServer.prefJavascriptStream, \
                    "There can't be two prefjs's (%d)" % self.reqNum

                self.beginSendingChunks()
                if (config.get(config.RUN_AT_STARTUP)):
                    self.queueChunk("text/plain", "setRunAtStartup(true);")
                else:
                    self.queueChunk("text/plain", "setRunAtStartup(false);")
                checkEvery = config.get(config.CHECK_CHANNELS_EVERY_X_MN)
                self.queueChunk("text/plain", "setCheckEvery('%s');" % checkEvery)
                speed = config.get(config.UPSTREAM_LIMIT_IN_KBS)
                self.queueChunk("text/plain", "setMaxUpstream(%s);" % speed)

                if (config.get(config.LIMIT_UPSTREAM)):
                    self.queueChunk("text/plain", "setLimitUpstream(true);")
                else:
                    self.queueChunk("text/plain", "setLimitUpstream(false);")

                min = config.get(config.PRESERVE_X_GB_FREE)
                self.queueChunk("text/plain", "setMinDiskSpace(%s);" % min)
                if (config.get(config.PRESERVE_DISK_SPACE)):
                    self.queueChunk("text/plain", "setHasMinDiskSpace(true);")
                else:
                    self.queueChunk("text/plain", "setHasMinDiskSpace(false);")

                expire = config.get(config.EXPIRE_AFTER_X_DAYS)
                self.queueChunk("text/plain", "setExpire('%s');" % expire)

                httpServer.prefJavascriptQueue = []
                httpServer.prefJavascriptStream = self
            finally:
                httpServer.classLock.release()

            self.runChunkPump()
            return

        ## Initial HTML ##
        match = re.match("^/dtv/document/(.*)", path)
        if match:
            cookie = match.group(1)
            print "[%s @%s] Initial HTML" % (self.reqNum, cookie)

            assert cookie in pendingDocuments, \
                "bad document request %s to HTMLDisplay server %d" % \
                (cookie, self.reqNum)
            (contentType, body) = pendingDocuments[cookie]
            del pendingDocuments[cookie]

            self.sendDocumentAndClose(contentType, body)
            return

        ## Action ## 
        match = re.match(r"^/dtv/action/([^?]*)\?(.*)", path)
        if match:
            cookie = match.group(1)
            url = match.group(2)
            print "[%s @%s] Action: %s" % (self.reqNum, cookie, url)

	    HTMLDisplay.dispatchEventByCookie(cookie, url)
            self.sendDocumentAndClose("text/plain", "")
            return

        ## Returns result from UI Backend Delegate call ##

        # If we find that in the future the web server is being
        # littered with lots of URLs specific to the frontend, we may
        # want to make a more general system for registering python functions
        # that can be called by XUL

        match = re.match(r"^/dtv/delegateresult/([^?]*)\?(.*)", path)
        if match:
            cookie = match.group(1)
            url = match.group(2)
            print "[%s @%s] UIBackendDelegate: %s" % (self.reqNum, cookie, url)

	    UIBackendDelegate.dispatchResultByCookie(cookie, url)
            self.sendDocumentAndClose("text/plain", "")
            return

        ## Channel guide API ##
        match = re.match(r"^/dtv/dtvapi/([^?]+)\?(.*)", path)
        if match:
            # NEEDS: it may be necessary to encode the url parameter
            # in JS, and decode it here. I'm not super-clear on the
            # circumstances (if any) under which Mozilla would treat
            # the query string as other than opaque bytes.
            action = match.group(1)
            parameter = match.group(2)
            print "[%s] DTVAPI: action %s, parameter %s" % (self.reqNum, \
                                                            action, parameter)

            if action == 'addChannel':
                app.Controller.instance.addFeed(parameter)
            elif action == 'goToChannel':
                app.Controller.instance.selectFeed(parameter)
            else:
                print "WARNING: ignored bad DTVAPI request '%s'" % request

            self.sendDocumentAndClose("text/plain", "")
            return

        ## Resource file ##
        match = re.match("^/dtv/resource/(.*)", path)
        if match:
            relativePath = match.group(1)
            print "[%s] Resource: %s" % (self.reqNum, relativePath)
            fullPath = resource.path(relativePath)
            data = open(fullPath,'rb').read()

            # Guess the content-type.
            contentType = None
            if re.search(".png$", fullPath):
                contentType = "image/png"
            elif re.search(".jpg$", fullPath):
                contentType = "image/jpeg"
            elif re.search(".jpeg$", fullPath):
                contentType = "image/jpeg"
            elif re.search(".gif$", fullPath):
                contentType = "image/gif"
            elif re.search(".css$", fullPath):
                contentType = "text/css"
            elif re.search(".js$", fullPath):
                contentType = "application/x-javascript"

            self.sendDocumentAndClose(contentType, data, cache=True)
            return

        ## Fell through - bad URL ##
        assert False, "Unrecognized request"

    def sendDocumentAndClose(self, contentType, data, cache=False):
        self.socket.send("HTTP/1.0 200 OK\r\n")
        self.socket.send("Content-Length: %s\r\n" % len(data))
        if contentType:
            self.socket.send("Content-Type: %s\r\n" % contentType)
        if cache and not 'DTV_DISABLE_CACHE' in os.environ:
            cacheTime = 60*60 # keep it an hour
            thenGMT = time.gmtime(time.time()+cacheTime)
            thenString = time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                                       thenGMT)
            self.socket.send("Expires: %s\r\n" % thenString)
        self.socket.send("\r\n")
        self.socket.send(data)

    def queueChunk(self, mimeType, body):
        self.cond.acquire()
        try:
            assert self.isChunked, \
                "queueChunk only works on event-based HTTP sessions"
            self.chunkQueue.append((mimeType, body))
            self.cond.notify()
        finally:
            self.cond.release()

    def beginSendingChunks(self):
        self.socket.send("""HTTP/1.0 200 OK
Content-Type: multipart/x-mixed-replace;boundary="%s"

--%s""" % (self.boundary, self.boundary))
        self.isChunked = True

    def runChunkPump(self):
        self.cond.acquire()
        try:
            while True:
                while len(self.chunkQueue) == 0:
                    self.cond.wait()
            
                (mimeType, body) = self.chunkQueue[0]
                self.chunkQueue = self.chunkQueue[1:]

                self.cond.release()
                try:
                    try:
                        self.socket.send("Content-type: %s\r\n\r\n%s\r\n--%s" \
                                         % (mimeType, body, self.boundary))
                    except socket.error, (code, description):
                        if code == errno.ECONNABORTED or \
                                code == errno.ECONNRESET:
                            print "[%d] Events end with remote error '%s'" % \
                                (self.reqNum, description)
                            return
                        else:
                            raise
                finally:
                    self.cond.acquire()

        finally:
            self.cond.release()
            self.socket.close()

###############################################################################
#### Channel guide support                                                 ####
###############################################################################

# These are used by the channel guide. See ChannelGuideToDtvApi in the
# Trac wiki for the full writeup.

def getDTVAPICookie():
    return str(getServerPort())

def getDTVAPIURL():
    return "http://127.0.0.1:%s/dtv/resource/dtvapi.js" % getServerPort()

###############################################################################
#### HTML display                                                          ####
###############################################################################

def _genMutator(name):
    """Internal: Generates a method that causes the javascript function with
the given name to be called with the arguments passed to the method. Each
argument will be turned into a string and quoted according to Javascript's
requirements. When the method is called, it returns immediately, and the
request goes in a queue."""
    def mutatorFunc(self, *args):
        self.lock.acquire()
        try:
            args = ','.join(['"%s"' % quoteJS(a) for a in args])
            command = "%s(%s);" % (name, args)
            
            command = xhtmltools.toUTF8Bytes(command)         

            if self.mutationOutput:
                self.mutationOutput.queueChunk("text/plain", command)
            else:
                self.queue.append(command)
        finally:
            self.lock.release()
    return mutatorFunc

class HTMLDisplay (app.Display):
    "Selectable Display that shows a HTML document."

    def __init__(self, html, existingView=None, frameHint=None, areaHint=None,
                 baseURL=None):
        """'html' is the initial contents of the display, as a string.
        Remaining arguments are ignored."""

        html=xhtmltools.toUTF8Bytes(html)

        if baseURL is not None:
            # This is something the Mac port uses. Complain about that.
            print "WARNING: HTMLDisplay ignoring baseURL '%s'" % baseURL

        app.Display.__init__(self)

        # Save the HTML so the server can find it
        pendingDocuments[self.getEventCookie()] = ("text/html", html)

	self.lock = threading.RLock()
        self.mutationOutput = None
        self.queue = []

    def getURL(self):
        """Return the URL to load to see this document."""
        return "http://127.0.0.1:%s/dtv/document/%s" % \
            (self.getServerPort(), self.getEventCookie())

    # The mutation functions.
    addItemAtEnd = _genMutator('addItemAtEnd')
    addItemBefore = _genMutator('addItemBefore')
    removeItem = _genMutator('removeItem')
    changeItem = _genMutator('changeItem')
    hideItem = _genMutator('hideItem')
    showItem = _genMutator('showItem')

    ### Concerning dispatching events via context cookies ###

    cookieToInstanceMap = {}

    # NEEDS: security audit: do we need to make cookies difficult to
    # predict?
    def getEventCookie(self):
	# Can't do this initialization in constructor, because of
	# circular dependency between HTMLDisplay constructor and
	# derived TemplateDisplay constructor. (You need the initial
	# HTML to create the HTMLDisplay, but you need the eventCookie
	# to make the initial HTML.) NEEDS: wish there was a way to
	# put a mutex around this. Is safe in the current
	# implementation, though, because getEventCookie is always
	# called first from the TemplateDisplay constructor.
	if hasattr(self, 'eventCookie'):
	    return self.eventCookie

	# Create cookie and add this instance to the instance cookie
	# lookup table
	self.eventCookie = str(id(self))
	HTMLDisplay.cookieToInstanceMap[self.eventCookie] = self

	return self.eventCookie

    def getDTVPlatformName(self):
        return getDTVPlatformName()

    def getServerPort(self):
        port = getServerPort()
        return port

    @classmethod
    def dispatchEventByCookie(klass, eventCookie, eventURL):
        thread = threading.Thread(target=lambda : klass.cookieToInstanceMap[eventCookie].onURLLoad(eventURL))
        thread.setName("dispatchEvent -- %s" % eventURL)
        thread.setDaemon(False)
        thread.start()

    def onURLLoad(self, url):
        """Called when this HTML browser attempts to load a URL (either
        through user action or Javascript.) The URL is provided as a
        string. Return true to allow the URL to load, or false to cancel
        the load (for example, because it was a magic URL that marks
        an item to be downloaded.) Implementation in HTMLDisplay always
        returns true; override in a subclass to implement special
        behavior."""
        # For overriding
        return True

    @classmethod
    def setMutationOutput(klass, eventCookie, htmlServer):
	self = klass.cookieToInstanceMap[eventCookie]
        assert not self.mutationOutput, "HTMLDisplay already has its htmlServer"

        self.lock.acquire()
        try:
            self.mutationOutput = htmlServer
            for q in self.queue:
                self.mutationOutput.queueChunk('text/plain', q)
            self.queue = []
        finally:
            self.lock.release()

    ### Concerning destruction ###

    def unlink(self):
	self.lock.acquire()
	try:
	    if self.eventCookie in HTMLDisplay.cookieToInstanceMap:
		del HTMLDisplay.cookieToInstanceMap[self.eventCookie]
            if self.eventCookie in pendingDocuments:
                del pendingDocuments[self.eventCookie]
	finally:
	    self.lock.release()

    def __del__(self):
        self.unlink()

###############################################################################
###############################################################################
