#!/usr/bin/env python
# This file is part of Xpra.
# Copyright (C) 2013 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import time
from xpra.gtk_common.gobject_compat import import_gobject
gobject = import_gobject()
gobject.threads_init()

import unittest
from collections import deque
try:
    from xpra.server import video_subregion, region
except ImportError:
    video_subregion = None
    region = None


class TestVersionUtilModule(unittest.TestCase):

    def test_eq(self):
        log = video_subregion.sslog

        def refresh_cb(window, regions):
            log("refresh_cb(%s, %s)", window, regions)
        r = video_subregion.VideoSubregion(gobject.timeout_add, gobject.source_remove, refresh_cb, 150)

        ww = 1024
        wh = 768

        log("* checking that we need some events")
        last_damage_events = []
        for x in range(video_subregion.MIN_EVENTS):
            last_damage_events.append((0, 0, 0, 1, 1))
        r.identify_video_subregion(ww, wh, video_subregion.MIN_EVENTS, last_damage_events)
        assert r.rectangle is None

        vr = (time.time(), 100, 100, 320, 240)
        log("* easiest case: all updates in one region")
        last_damage_events = []
        for _ in range(50):
            last_damage_events.append(vr)
        r.identify_video_subregion(ww, wh, 50, last_damage_events)
        assert r.rectangle
        assert r.rectangle==region.rectangle(*vr[1:])

        log("* checking that empty damage events does not cause errors")
        r.reset()
        r.identify_video_subregion(ww, wh, 0, [])
        assert r.rectangle is None

        log("* checking that full window is not a region")
        vr = (time.time(), 0, 0, ww, wh)
        last_damage_events = []
        for _ in range(50):
            last_damage_events.append(vr)
        r.identify_video_subregion(ww, wh, 50, last_damage_events)
        assert r.rectangle is None

        log("* checking that regions covering the whole window give the same result")
        last_damage_events = deque(maxlen=150)
        for x in range(4):
            for y in range(4):
                vr = (time.time(), ww*x/4, wh*y/4, ww/4, wh/4)
                last_damage_events.append(vr)
                last_damage_events.append(vr)
                last_damage_events.append(vr)
        r.identify_video_subregion(ww, wh, 150, last_damage_events)
        assert r.rectangle is None

        vr = (time.time(), ww/4, wh/4, ww/2, wh/2)
        log("* mixed with region using 1/5 of window and 1/3 of updates: %s", vr)
        for _ in range(30):
            last_damage_events.append(vr)
        r.identify_video_subregion(ww, wh, 200, last_damage_events)
        assert r.rectangle is not None

        log("* info=%s", r.get_info())

        log("* checking that two video regions with the same characteristics get merged")
        last_damage_events = deque(maxlen=150)
        r.reset()
        v1 = (time.time(), 100, 100, 320, 240)
        v2 = (time.time(), 500, 500, 320, 240)
        for _ in range(50):
            last_damage_events.append(v1)
            last_damage_events.append(v2)
        r.identify_video_subregion(ww, wh, 100, last_damage_events)
        m = region.merge_all([region.rectangle(*v1[1:]), region.rectangle(*v2[1:])])
        assert r.rectangle==m, "expected %s but got %s" % (m, r.rectangle)

        log("* but not if they are too far apart")
        last_damage_events = deque(maxlen=150)
        r.reset()
        v1 = (time.time(), 20, 20, 320, 240)
        v2 = (time.time(), ww-20-320, wh-240-20, 320, 240)
        for _ in range(50):
            last_damage_events.append(v1)
            last_damage_events.append(v2)
        r.identify_video_subregion(ww, wh, 100, last_damage_events)
        assert r.rectangle is None


def main():
    if video_subregion and region:
        unittest.main()

if __name__ == '__main__':
    main()
