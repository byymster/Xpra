# This file is part of Xpra.
# Copyright (C) 2012-2014 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import gobject
import glib

from xpra.log import Logger
log = Logger("clipboard", "osx")

from xpra.clipboard.translated_clipboard import TranslatedClipboardProtocolHelper
from xpra.clipboard.clipboard_base import ClipboardProxy, TEXT_TARGETS

update_clipboard_change_count = None

change_callbacks = []
change_count = 0
_initialised = False


def get_ctypes_Pasteboard_changeCount():
    """ a ctypes implementation to access the Pasteboard's changeCount """
    import ctypes.util
    appkit = ctypes.cdll.LoadLibrary(ctypes.util.find_library('AppKit'))
    assert appkit
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library('objc'))
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.objc_msgSend.restype = ctypes.c_void_p
    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    # Without this, it will still work, but it'll leak memory
    NSAutoreleasePool = objc.objc_getClass('NSAutoreleasePool')
    NSPasteboard = objc.objc_getClass('NSPasteboard')
    def get_change_count():
        pool = objc.objc_msgSend(NSAutoreleasePool, objc.sel_registerName('alloc'))
        pool = objc.objc_msgSend(pool, objc.sel_registerName('init'))
        # get the pasteboard class id then an instance:
        pasteboard = objc.objc_msgSend(NSPasteboard, objc.sel_registerName('generalPasteboard'))
        if pasteboard is None:
            log.warn("cannot load Pasteboard, maybe not running from a GUI session?")
            return None
        changeCount = objc.objc_msgSend(pasteboard, objc.sel_registerName('changeCount'))
        objc.objc_msgSend(pool, objc.sel_registerName('release'))
        return changeCount
    return get_change_count


def get_AppKit_Pasteboard_changeCount():
    """ PyObjC AppKit implementation to access Pasteboard's changeCount """
    from AppKit import NSPasteboard      #@UnresolvedImport
    pasteboard = NSPasteboard.generalPasteboard()
    if pasteboard is None:
        log.warn("cannot load Pasteboard, maybe not running from a GUI session?")
        return None

    def get_change_count():
        return pasteboard.changeCount()
    return get_change_count


def init_pasteboard():
    global _initialised
    if _initialised:
        return
    _initialised = True
    #try both implementations, ctypes first
    for info, init_fn in (("ctypes", get_ctypes_Pasteboard_changeCount),
                          ("AppKit", get_AppKit_Pasteboard_changeCount)):
        try:
            log("init_pasteboard: trying %s using %s", info, init_fn)
            get_change_count = init_fn()
            if get_change_count is None:
                continue
            #test it (may throw an exception?):
            v = get_change_count()
            if v is None:
                continue
            log("%s pasteboard access success, current change count=%s, setting up timer to watch for changes", info, v)
            #good, use it:
            setup_watcher(get_change_count)
            return True
        except:
            log.error("error initializing %s pasteboard", info, exc_info=True)
    return False

def setup_watcher(get_change_count):
    global change_callbacks, update_clipboard_change_count

    #this function will update the globalcount:
    def update_change_count():
        global change_count
        change_count = get_change_count()
        return change_count
    update_clipboard_change_count = update_change_count

    #register this function to check periodically:
    def timer_clipboard_check():
        global change_count
        c = change_count
        change_count = update_change_count()
        log("timer_clipboard_check() was %s, now %s", c, change_count)
        if c!=change_count:
            for x in change_callbacks:
                try:
                    x()
                except Exception as e:
                    log("error in change callback %s: %s", x, e)

    from xpra.platform.ui_thread_watcher import get_UI_watcher
    w = get_UI_watcher()
    if w is None:
        log.warn("no UI watcher available, cannot watch for clipboard events")
        return False
    log("UI watcher=%s", w)
    w.add_alive_callback(timer_clipboard_check)
    return True


class OSXClipboardProtocolHelper(TranslatedClipboardProtocolHelper):
    """
        Full of OSX quirks!
        darwin/features.py should be set
        * CLIPBOARD_GREEDY: request the other end to send tokens for all owner change events
        * CLIPBOARD_WANT_TARGETS: include targets with the tokens
    """

    def __init__(self, *args, **kwargs):
        init_pasteboard()
        kwargs["clipboard.local"] = "CLIPBOARD"
        kwargs["clipboards.local"] = ["CLIPBOARD"]
        TranslatedClipboardProtocolHelper.__init__(self, *args, **kwargs)

    def make_proxy(self, clipboard):
        return OSXClipboardProxy(clipboard)

    def _do_munge_raw_selection_to_wire(self, target, dtype, dformat, data):
        #override so we can catch weird OSX data:
        if dformat == 0 and dtype=="NONE":
            log("got 'NONE' data from clipboard")
            return None, None
        return TranslatedClipboardProtocolHelper._do_munge_raw_selection_to_wire(self, target, dtype, dformat, data)

    def _get_clipboard_from_remote_handler(self, proxy, selection, target):
        #cannot work on osx, the nested mainloop doesn't run :(
        #so we don't even try and rely on the "wants_targets" flag
        #to get the server to send us the data with the token
        #see "got_token" below
        return None

    def __repr__(self):
        return "OSXClipboardProtocolHelper"


class OSXClipboardProxy(ClipboardProxy):

    def __init__(self, selection):
        ClipboardProxy.__init__(self, selection)
        global change_callbacks
        change_callbacks.append(self.local_clipboard_changed)

    def got_token(self, targets, target_data, claim):
        ClipboardProxy.got_token(self, targets, target_data, claim)
        #prevent any change from firing another clipboard update:
        if update_clipboard_change_count:
            c = update_clipboard_change_count()
            log("change count now at %s", c)

    def local_clipboard_changed(self):
        log("local_clipboard_changed()")
        self.do_owner_changed()

    def cleanup(self):
        ClipboardProxy.cleanup(self)
        global change_callbacks
        try:
            change_callbacks.remove(self.local_clipboard_changed)
        except:
            pass


gobject.type_register(OSXClipboardProxy)





def main():
    global change_count
    import time
    from xpra.platform import program_context
    with program_context("OSX Clipboard Change Test"):
        log.enable_debug()

        #init UI watcher with gobject (required by pasteboard monitoring code)
        from xpra.platform.ui_thread_watcher import get_UI_watcher
        gobject.threads_init()
        import gtk.gdk
        gtk.gdk.threads_init()
        get_UI_watcher(glib.timeout_add)

        log.info("testing pasteboard")
        if not init_pasteboard():
            log.warn("failed to initialize a pasteboard!")
            return
        assert update_clipboard_change_count is not None, "cannot access clipboard change count"
        cc = update_clipboard_change_count()
        log.info("current change count=%s", cc)
        clipboard = gtk.Clipboard(selection="CLIPBOARD")
        log.info("changing clipboard %s contents", clipboard)
        clipboard.set_text("HELLO WORLD %s" % time.time())
        cc = update_clipboard_change_count()
        log.info("new change count=%s", cc)
        log.info("any update to your clipboard should get logged (^C to exit)")
        while True:
            v = update_clipboard_change_count()
            if v!=cc:
                log.info("success! the clipboard change has been detected, new change count=%s", v)
            else:
                log.info(".")
            time.sleep(1)
        if v==cc:
            log.info("no clipboard change detected")


if __name__ == "__main__":
    main()
