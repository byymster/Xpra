#!/usr/bin/env python
# This file is part of Xpra.
# Copyright (C) 2011-2014 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

#tricky: use xpra.scripts.config to get to the python "platform" module
from xpra.scripts.config import python_platform as pp
import sys
import os
from xpra import __version__ as local_version
from xpra.log import Logger
log = Logger("util")

def version_as_numbers(version):
    return [int(x) for x in version.split(".")]

def version_compat_check(remote_version):
    if remote_version is None:
        msg = "remote version not available!"
        log(msg)
        return msg
    rv = version_as_numbers(remote_version)
    lv = version_as_numbers(local_version)
    if rv==lv:
        log("identical remote version: %s", remote_version)
        return None
    if rv[0:3]<[0, 12, 6]:
        #this is the oldest version we support
        msg = "remote version %s is too old, sorry" % str(rv[:2])
        log(msg)
        return  msg
    if rv[0]>0:
        log("newer remote version %s may work, we'll see..", remote_version)
        return  None
    log("local version %s should be compatible with remote version: %s", local_version, remote_version)
    return None


def get_host_info():
    #this function is for non UI thread info
    info = {}
    try:
        import socket
        info.update({
                    "pid"                   : os.getpid(),
                    "byteorder"             : sys.byteorder,
                    "hostname"              : socket.gethostname(),
                    "python.full_version"   : sys.version,
                    "python.version"        : sys.version_info[:3],
                    })
    except:
        pass
    for x in ("uid", "gid"):
        if hasattr(os, "get%s" % x):
            try:
                info[x] = getattr(os, "get%s" % x)()
            except:
                pass
    return info

def get_version_info():
    props = {
             "version"  : local_version
             }
    try:
        from xpra.src_info import LOCAL_MODIFICATIONS, REVISION
        props.update({
                    "local_modifications"  : LOCAL_MODIFICATIONS,
                    "revision"             : REVISION,
                  })
    except Exception as e:
        log.warn("missing some source information: %s", e)
    try:
        from xpra.build_info import BUILD_DATE, BUILT_BY, BUILT_ON, BUILD_BIT, BUILD_CPU, \
                                    COMPILER_VERSION, LINKER_VERSION, BUILD_TIME, PYTHON_VERSION, CYTHON_VERSION
        props.update({
                    "date"                 : BUILD_DATE,
                    "time"                 : BUILD_TIME,
                    "by"                   : BUILT_BY,
                    "on"                   : BUILT_ON,
                    "bit"                  : BUILD_BIT,
                    "cpu"                  : BUILD_CPU,
                    "compiler"             : COMPILER_VERSION,
                    "linker"               : LINKER_VERSION,
                    "python"               : PYTHON_VERSION,
                    "cython"               : CYTHON_VERSION,
                  })
    except Exception as e:
        log.warn("missing some build information: %s", e)
    return props

def do_get_platform_info():
    from xpra.os_util import platform_name
    def get_processor_name():
        if pp.system() == "Windows":
            return pp.processor()
        elif pp.system() == "Darwin":
            os.environ['PATH'] = os.environ['PATH'] + os.pathsep + '/usr/sbin'
            command ="sysctl -n machdep.cpu.brand_string"
            import subprocess
            return subprocess.check_output(command).strip()
        elif pp.system() == "Linux":
            with open("/proc/cpuinfo") as f:
                data = f.read()
            import re
            for line in data.split("\n"):
                if "model name" in line:
                    return re.sub(".*model name.*:", "", line,1).strip()
        assert False
    info = {
            ""          : sys.platform,
            "name"      : platform_name(sys.platform, pp.release()),
            "release"   : pp.release(),
            "platform"  : pp.platform(),
            "machine"   : pp.machine(),
            "processor" : pp.processor(),
            "architecture" : pp.architecture(),
            }
    try:
        info["processor"] = get_processor_name()
    except:
        info["processor"] = pp.processor()
    if sys.platform.startswith("linux") and hasattr(pp, "linux_distribution"):
        info["linux_distribution"] = pp.linux_distribution()
    return info
#cache the output:
platform_info_cache = None
def get_platform_info():
    global platform_info_cache
    if platform_info_cache is None:
        platform_info_cache = do_get_platform_info()
    return platform_info_cache
