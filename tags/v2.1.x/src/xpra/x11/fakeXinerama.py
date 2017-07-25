#!/usr/bin/env python
# coding=utf8
# This file is part of Xpra.
# Copyright (C) 2014-2017 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os

from xpra.log import Logger
log = Logger("x11", "server", "x11")

from xpra.util import prettify_plug_name
from xpra.os_util import find_lib, find_lib_ldconfig, LINUX
from xpra.version_util import XPRA_VERSION


fakeXinerama_config_files = [
            #the new fakexinerama file:
            os.path.expanduser("~/.%s-fakexinerama" % os.environ.get("DISPLAY")),
            #compat file for "old" version found on github:
            os.path.expanduser("~/.fakexinerama"),
           ]

def find_libfakeXinerama():
    if LINUX:
        try:
            libpath = find_lib_ldconfig("fakeXinerama")
            if libpath:
                return libpath
        except Exception as e:
            log.error("Error: cannot launch ldconfig -p to locate libfakeXinerama:", e)
            log.error(" %s", e)
    return find_lib("libfakeXinerama.so.1")

current_xinerama_config = None

def save_fakeXinerama_config(supported=True, source="", ss=[]):
    """ returns True if the fakexinerama config was modified """
    global current_xinerama_config
    def delfile(msg):
        global current_xinerama_config
        if msg:
            log.warn(msg)
        cleanup_fakeXinerama()
        oldconf = current_xinerama_config
        current_xinerama_config = None
        return oldconf is not None
    if not supported:
        return delfile(None)
    if len(ss)==0:
        return delfile("cannot save fake xinerama settings: no display found")
    if len(ss)>1:
        return delfile("cannot save fake xinerama settings: more than one display found")
    if len(ss)==2 and type(ss[0])==int and type(ss[1])==int:
        #just WxH, not enough display information
        return delfile("cannot save fake xinerama settings: missing display data from client %s" % source)
    display_info = ss[0]
    if len(display_info)<10:
        return delfile("cannot save fake xinerama settings: incomplete display data from client %s" % source)
    #display_name, width, height, width_mm, height_mm, \
    #monitors, work_x, work_y, work_width, work_height = s[:11]
    monitors = display_info[5]
    if len(monitors)>=10:
        return delfile("cannot save fake xinerama settings: too many monitors! (%s)" % len(monitors))
    #generate the file data:
    data = ["# file generated by xpra %s for display %s" % (XPRA_VERSION, os.environ.get("DISPLAY")),
            "# %s monitors:" % len(monitors),
            "%s" % len(monitors)]
    #the new config (numeric values only)
    config = [len(monitors)]
    for i, m in enumerate(monitors):
        if len(m)<7:
            return delfile("cannot save fake xinerama settings: incomplete monitor data for monitor: %s" % m)
        plug_name, x, y, width, height, wmm, hmm = m[:7]
        data.append("# %s (%smm x %smm)" % (prettify_plug_name(plug_name, "monitor %s" % i), wmm, hmm))
        data.append("%s %s %s %s" % (x, y, width, height))
        config.append((x, y, width, height))
    if current_xinerama_config==config:
        #we assume that no other process is going to overwrite the deprecated .fakexinerama
        log("fake xinerama config unchanged")
        return False
    current_xinerama_config = config
    data.append("")
    contents = "\n".join(data)
    for filename in fakeXinerama_config_files:
        try:
            with open(filename, 'wb') as f:
                f.write(contents)
        except Exception as e:
            log.warn("Error writing fake xinerama file '%s':", filename)
            log.warn(" %s", e)
    log("saved %s monitors to fake xinerama files: %s", len(monitors), fakeXinerama_config_files)
    return True

def cleanup_fakeXinerama():
    for f in fakeXinerama_config_files:
        try:
            if os.path.exists(f):
                log("cleanup_fakexinerama() deleting fake xinerama file '%s'", f)
                os.unlink(f)
        except Exception as e:
            log.error("Error: failed to delete fakexinerama config file")
            log.error(" '%s': %s", f, e)


def main():
    print("libfakeXinerama=%s" % find_libfakeXinerama())

if __name__ == "__main__":
    main()