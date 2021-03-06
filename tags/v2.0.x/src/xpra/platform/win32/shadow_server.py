# coding=utf8
# This file is part of Xpra.
# Copyright (C) 2012-2017 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
import time
import ctypes

from ctypes.wintypes import RECT

from xpra.log import Logger
from xpra.util import AdHocStruct, envbool, prettify_plug_name, roundup
log = Logger("shadow", "win32")
traylog = Logger("tray")
shapelog = Logger("shape")
netlog = Logger("network")

from xpra.util import XPRA_APP_ID
from xpra.os_util import StringIOClass
from xpra.scripts.config import InitException
from xpra.server.gtk_server_base import GTKServerBase
from xpra.server.shadow.gtk_shadow_server_base import GTKShadowServerBase
from xpra.server.shadow.root_window_model import RootWindowModel
from xpra.platform.win32 import constants as win32con
from xpra.platform.win32.keyboard_config import KeyboardConfig, fake_key
from xpra.platform.win32.gui import get_virtualscreenmetrics
from xpra.platform.win32.namedpipes.connection import NamedPipeConnection
from xpra.platform.win32.win32_events import get_win32_event_listener
from xpra.codecs.image_wrapper import ImageWrapper

#user32:
from xpra.platform.win32.common import (EnumWindows, EnumWindowsProc, FindWindowA, IsWindowVisible,
                                        GetWindowTextLengthW, GetWindowTextW,
                                        GetWindowRect,
                                        GetWindowThreadProcessId,
                                        GetDesktopWindow, GetSystemMetrics,
                                        GetWindowDC, ReleaseDC, SetPhysicalCursorPos,
                                        mouse_event)
#gdi32:
from xpra.platform.win32.common import (CreateCompatibleDC, DeleteDC,
                                        CreateCompatibleBitmap,
                                        GetBitmapBits, SelectObject,
                                        BitBlt, GetDeviceCaps,
                                        GetSystemPaletteEntries)


NOEVENT = object()
BUTTON_EVENTS = {
                 #(button,up-or-down)  : win-event-name
                 (1, True)  : (win32con.MOUSEEVENTF_LEFTDOWN,   0),
                 (1, False) : (win32con.MOUSEEVENTF_LEFTUP,     0),
                 (2, True)  : (win32con.MOUSEEVENTF_MIDDLEDOWN, 0),
                 (2, False) : (win32con.MOUSEEVENTF_MIDDLEUP,   0),
                 (3, True)  : (win32con.MOUSEEVENTF_RIGHTDOWN,  0),
                 (3, False) : (win32con.MOUSEEVENTF_RIGHTUP,    0),
                 (4, True)  : (win32con.MOUSEEVENTF_WHEEL,      win32con.WHEEL_DELTA),
                 (4, False) : NOEVENT,
                 (5, True)  : (win32con.MOUSEEVENTF_WHEEL,      -win32con.WHEEL_DELTA),
                 (5, False) : NOEVENT,
                 }

NULLREGION = 1      #The region is empty.
SIMPLEREGION = 2    #The region is a single rectangle.
COMPLEXREGION = 3   #The region is more than a single rectangle.
REGION_CONSTS = {
                NULLREGION      : "the region is empty",
                SIMPLEREGION    : "the region is a single rectangle",
                COMPLEXREGION   : "the region is more than a single rectangle",
                }
SEAMLESS = envbool("XPRA_WIN32_SEAMLESS", False)
DISABLE_DWM_COMPOSITION = True
#no composition on XP, don't bother trying:
try:
    from sys import getwindowsversion       #@UnresolvedImport
    if getwindowsversion().major<6:
        DISABLE_DWM_COMPOSITION = False
except:
    pass
DISABLE_DWM_COMPOSITION = envbool("XPRA_DISABLE_DWM_COMPOSITION", DISABLE_DWM_COMPOSITION)

class PALETTEENTRY(ctypes.Structure):
    _fields_ = [
        ('peRed',   ctypes.c_ubyte),
        ('peGreen', ctypes.c_ubyte),
        ('peBlue',  ctypes.c_ubyte),
        ('peFlags', ctypes.c_ubyte),
        ]

DWM_EC_DISABLECOMPOSITION = 0
DWM_EC_ENABLECOMPOSITION = 1
def set_dwm_composition(value=DWM_EC_DISABLECOMPOSITION):
    try:
        from ctypes import windll
        windll.dwmapi.DwmEnableComposition(value)
        log("DwmEnableComposition(%s) succeeded", value)
        return True
    except Exception as e:
        log.error("Error: cannot change dwm composition:")
        log.error(" %s", e)
        return False


class Win32RootWindowModel(RootWindowModel):

    def __init__(self, root):
        RootWindowModel.__init__(self, root)
        self.dc, self.memdc, self.bitmap = None, None, None
        self.bit_depth = 32
        self.bitblt_err_time = 0
        self.disabled_dwm_composition = DISABLE_DWM_COMPOSITION and set_dwm_composition(DWM_EC_DISABLECOMPOSITION)
        if SEAMLESS:
            self.property_names.append("shape")
            self.dynamic_property_names.append("shape")
            self.rectangles = self.get_shape_rectangles(logit=True)
            self.shape_notify = []

    def cleanup(self):
        if self.disabled_dwm_composition:
            set_dwm_composition(DWM_EC_ENABLECOMPOSITION)
        dc = self.dc
        if dc:
            self.dc = None
            ReleaseDC(dc)
        memdc = self.memdc
        if memdc:
            self.memdc = None
            DeleteDC(memdc)

    def refresh_shape(self):
        rectangles = self.get_shape_rectangles()
        if rectangles==self.rectangles:
            return  #unchanged
        self.rectangles = rectangles
        shapelog("refresh_shape() sending notify for updated rectangles: %s", rectangles)
        #notify listeners:
        pspec = AdHocStruct()
        pspec.name = "shape"
        for cb, args in self.shape_notify:
            shapelog("refresh_shape() notifying: %s", cb)
            try:
                cb(self, pspec, *args)
            except:
                shapelog.error("error in shape notify callback %s", cb, exc_info=True)

    def connect(self, signal, cb, *args):
        if signal=="notify::shape":
            self.shape_notify.append((cb, args))
        else:
            RootWindowModel.connect(self, signal, cb, *args)

    def get_shape_rectangles(self, logit=False):
        #get the list of windows
        l = log
        if logit or envbool("XPRA_SHAPE_DEBUG", False):
            l = shapelog
        taskbar = FindWindowA("Shell_TrayWnd", None)
        l("taskbar window=%#x", taskbar)
        ourpid = os.getpid()
        l("our pid=%i", ourpid)
        rectangles = []
        def enum_windows_cb(hwnd, lparam):
            if not IsWindowVisible(hwnd):
                l("skipped invisible window %#x", hwnd)
                return True
            pid = ctypes.c_int()
            thread_id = GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid==ourpid:
                l("skipped our own window %#x", hwnd)
                return True
            #skipping IsWindowEnabled check
            length = GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length+1)
            if GetWindowTextW(hwnd, buf, length+1)>0:
                window_title = buf.value
            else:
                window_title = ''
            l("get_shape_rectangles() found window '%s' with pid=%i and thread id=%i", window_title, pid, thread_id)
            rect = RECT()
            if GetWindowRect(hwnd, ctypes.byref(rect))==0:
                l("GetWindowRect failure")
                return True
            left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
            if right<0 or bottom<0:
                l("skipped offscreen window at %ix%i", right, bottom)
                return True
            if hwnd==taskbar:
                l("skipped taskbar")
                return True
            #dirty way:
            if window_title=='Program Manager':
                return True
            #this should be the proper way using GetTitleBarInfo (but does not seem to work)
            #import ctypes
            #from ctypes.windll.user32 import GetTitleBarInfo        #@UnresolvedImport
            #from ctypes.wintypes import (DWORD, RECT)
            #class TITLEBARINFO(ctypes.Structure):
            #    pass
            #TITLEBARINFO._fields_ = [
            #    ('cbSize', DWORD),
            #    ('rcTitleBar', RECT),
            #    ('rgstate', DWORD * 6),
            #]
            #ti = TITLEBARINFO()
            #ti.cbSize = ctypes.sizeof(ti)
            #GetTitleBarInfo(hwnd, ctypes.byref(ti))
            #if ti.rgstate[0] & win32con.STATE_SYSTEM_INVISIBLE:
            #    log("skipped system invisible window")
            #    return True
            w = right-left
            h = bottom-top
            l("shape(%s - %#x)=%s", window_title, hwnd, (left, top, w, h))
            if w<=0 and h<=0:
                l("skipped invalid window size: %ix%i", w, h)
                return True
            if left==-32000 and top==-32000:
                #there must be a better way of skipping those - I haven't found it
                l("skipped special window")
                return True
            #now clip rectangle:
            if left<0:
                left = 0
                w = right
            if top<0:
                top = 0
                h = bottom
            rectangles.append((left, top, w, h))
            return True
        EnumWindows(EnumWindowsProc(enum_windows_cb))
        l("get_shape_rectangles()=%s", rectangles)
        return sorted(rectangles)

    def get_property(self, prop):
        if prop=="shape":
            assert SEAMLESS
            shape = {"Bounding.rectangles" : self.rectangles}
            #provide clip rectangle? (based on workspace area?)
            return shape
        return RootWindowModel.get_property(self, prop)


    def get_root_window_size(self):
        w = GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
        h = GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
        return w, h

    def get_image(self, x, y, width, height, logger=None):
        start = time.time()
        desktop_wnd = GetDesktopWindow()
        metrics = get_virtualscreenmetrics()
        if self.metrics is None or self.metrics!=metrics:
            #new metrics, start from scratch:
            self.metrics = metrics
            self.dc, self.memdc, self.bitmap = None, None, None
        dx, dy, dw, dh = metrics
        #clamp rectangle requested to the virtual desktop size:
        if x<dx:
            width -= x-dx
            x = dx
        if y<dy:
            height -= y-dy
            y = dy
        if width>dw:
            width = dw
        if height>dh:
            height = dh
        if not self.dc:
            self.dc = GetWindowDC(desktop_wnd)
            assert self.dc, "failed to get a drawing context from the desktop window %s" % desktop_wnd
            self.bit_depth =  GetDeviceCaps(self.dc, win32con.BITSPIXEL)
            self.memdc = CreateCompatibleDC(self.dc)
            assert self.memdc, "failed to get a compatible drawing context from %s" % self.dc
            self.bitmap = CreateCompatibleBitmap(self.dc, width, height)
            assert self.bitmap, "failed to get a compatible bitmap from %s" % self.dc
        r = SelectObject(self.memdc, self.bitmap)
        if r==0:
            log.error("Error: cannot select bitmap object")
            return None
        select_time = time.time()
        log("get_image up to SelectObject (%s) took %ims", REGION_CONSTS.get(r, r), (select_time-start)*1000)
        try:
            if BitBlt(self.memdc, 0, 0, width, height, self.dc, x, y, win32con.SRCCOPY)==0:
                e = ctypes.get_last_error()
                #rate limit the error message:
                now = time.time()
                if now-self.bitblt_err_time>10:
                    log.error("Error: failed to blit the screen, error %i", e)
                    self.bitblt_err_time = now
                return None
        except Exception as e:
            log("BitBlt error", exc_info=True)
            log.error("Error: cannot capture screen")
            log.error(" %s", e)
            return None
        bitblt_time = time.time()
        log("get_image BitBlt took %ims", (bitblt_time-select_time)*1000)
        rowstride = roundup(width*self.bit_depth//8, 2)
        buf_size = rowstride*height
        pixels = ctypes.create_string_buffer("", buf_size)
        log("GetBitmapBits(%#x, %#x, %#x)", self.bitmap, buf_size, ctypes.addressof(pixels))
        r = GetBitmapBits(self.bitmap, buf_size, ctypes.byref(pixels))
        if r==0:
            log.error("Error: failed to copy screen bitmap data")
            return None
        if r!=buf_size:
            log.warn("Warning: truncating pixel buffer, got %i bytes but expected %i", r, buf_size)
            pixels = pixels[:r]
        log("get_image GetBitmapBits took %ims", (time.time()-bitblt_time)*1000)
        assert pixels, "no pixels returned from GetBitmapBits"
        if self.bit_depth==32:
            rgb_format = "BGRX"
        elif self.bit_depth==30:
            rgb_format = "r210"
        elif self.bit_depth==24:
            rgb_format = "BGR"
        elif self.bit_depth==16:
            rgb_format = "BGR565"
        elif self.bit_depth==8:
            rgb_format = "RLE8"
        else:
            raise Exception("unsupported bit depth: %s" % self.bit_depth)
        bpp = self.bit_depth//8
        v = ImageWrapper(x, y, width, height, pixels, rgb_format, self.bit_depth, rowstride, bpp, planes=ImageWrapper.PACKED, thread_safe=True)
        if self.bit_depth==8:
            count = GetSystemPaletteEntries(self.dc, 0, 0, None)
            log("palette size: %s", count)
            palette = []
            if count>0:
                buf = (PALETTEENTRY*count)()
                r = GetSystemPaletteEntries(self.dc, 0, count, ctypes.byref(buf))
                #we expect 16-bit values, so bit-shift them:
                for p in buf:
                    palette.append((p.peRed<<8, p.peGreen<<8, p.peBlue<<8))
            v.set_palette(palette)
        if logger==None:
            logger = log
        log("get_image%s=%s took %ims", (x, y, width, height), v, (time.time()-start)*1000)
        return v

    def take_screenshot(self):
        from PIL import Image               #@UnresolvedImport
        x, y, w, h = get_virtualscreenmetrics()
        image = self.get_image(x, y, w, h)
        if not image:
            return None
        assert image.get_width()==w and image.get_height()==h
        assert image.get_pixel_format()=="BGRX"
        img = Image.frombuffer("RGB", (w, h), image.get_pixels(), "raw", "BGRX", 0, 1)
        out = StringIOClass()
        img.save(out, format="PNG")
        screenshot = (img.width, img.height, "png", img.width*3, out.getvalue())
        out.close()
        return screenshot


class ShadowServer(GTKShadowServerBase):

    def __init__(self):
        GTKShadowServerBase.__init__(self)
        self.keycodes = {}
        if GetSystemMetrics(win32con.SM_SAMEDISPLAYFORMAT)==0:
            raise InitException("all the monitors must use the same display format")
        el = get_win32_event_listener()
        from xpra.net.bytestreams import set_continue_wait
        #on win32, we want to wait just a little while,
        #to prevent servers spinning wildly on non-blocking sockets:
        set_continue_wait(5)
        #TODO: deal with those messages?
        #el.add_event_callback(win32con.WM_POWERBROADCAST,   self.power_broadcast_event)
        #el.add_event_callback(WM_WTSSESSION_CHANGE,         self.session_change_event)
        #these are bound to callbacks in the client,
        #but on the server we just ignore them:
        el.ignore_events.update({
                                 win32con.WM_ACTIVATEAPP        : "WM_ACTIVATEAPP",
                                 win32con.WM_MOVE               : "WM_MOVE",
                                 win32con.WM_INPUTLANGCHANGE    : "WM_INPUTLANGCHANGE",
                                 win32con.WM_WININICHANGE       : "WM_WININICHANGE",
                                 })


    def print_screen_info(self):
        w, h = self.root.get_size()
        try:
            display = prettify_plug_name(self.root.get_screen().get_display().get_name())
        except:
            display = ""
        self.do_print_screen_info(display, w, h)


    def add_listen_socket(self, socktype, sock):
        netlog("add_listen_socket(%s, %s)", socktype, sock)
        if socktype=="named-pipe":
            #named pipe listener uses a thread:
            sock.new_connection_cb = self._new_connection
            self.socket_types[sock] = socktype
            sock.start()
        else:
            GTKServerBase.add_listen_socket(self, socktype, sock)

    def _new_connection(self, listener, *args):
        socktype = self.socket_types.get(listener)
        netlog("_new_connection(%s) socktype=%s", listener, socktype)
        if socktype!="named-pipe":
            return GTKServerBase._new_connection(self, listener)
        pipe_handle = args[0]
        conn = NamedPipeConnection(listener.pipe_name, pipe_handle)
        netlog.info("New %s connection received on %s", socktype, conn.target)
        return self.make_protocol(socktype, conn, frominfo=conn.target)


    def make_tray_widget(self):
        from xpra.platform.win32.win32_tray import Win32Tray
        return Win32Tray(self, XPRA_APP_ID, self.tray_menu, "Xpra Shadow Server", "server-notconnected", None, self.tray_click_callback, None, self.tray_exit_callback)


    def makeRootWindowModel(self):
        return Win32RootWindowModel(self.root)


    def refresh(self):
        v = GTKShadowServerBase.refresh(self)
        if v and SEAMLESS:
            self.root_window_model.refresh_shape()
        log("refresh()=%s", v)
        return v

    def do_process_mouse_common(self, proto, wid, pointer):
        #adjust pointer position for offset in client:
        try:
            x, y = pointer
            SetPhysicalCursorPos(x, y)
        except Exception as e:
            log("SetPhysicalCursorPos%s failed", pointer, exc_info=True)
            log.error("Error: failed to move the cursor:")
            log.error(" %s", e)

    def get_keyboard_config(self, props):
        return KeyboardConfig()

    def fake_key(self, keycode, press):
        fake_key(keycode, press)

    def do_process_button_action(self, proto, wid, button, pressed, pointer, modifiers, *args):
        self._update_modifiers(proto, wid, modifiers)
        x, y = self._process_mouse_common(proto, wid, pointer)
        self._server_sources.get(proto).user_event()
        event = BUTTON_EVENTS.get((button, pressed))
        if event is None:
            log.warn("no matching event found for button=%s, pressed=%s", button, pressed)
            return
        elif event is NOEVENT:
            return
        dwFlags, dwData = event
        mouse_event(dwFlags, x, y, dwData, 0)

    def make_hello(self, source):
        capabilities = GTKServerBase.make_hello(self, source)
        capabilities["shadow"] = True
        capabilities["server_type"] = "Python/gtk2/win32-shadow"
        return capabilities

    def get_info(self, proto):
        info = GTKServerBase.get_info(self, proto)
        info.setdefault("features", {})["shadow"] = True
        info.setdefault("server", {
                                   "type"       : "Python/gtk2/win32-shadow",
                                   "tray"       : self.tray,
                                   "tray-icon"  : self.tray_icon or ""
                                   })
        return info


def main():
    from xpra.platform import program_context
    with program_context("Shadow-Test", "Shadow Server Screen Capture Test"):
        rwm = Win32RootWindowModel(None)
        pngdata = rwm.take_screenshot()
        FILENAME = "screenshot.png"
        with open(FILENAME , "wb") as f:
            f.write(pngdata[4])
        print("saved screenshot as %s" % FILENAME)


if __name__ == "__main__":
    main()
