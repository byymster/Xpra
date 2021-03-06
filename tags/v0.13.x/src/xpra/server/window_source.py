# coding=utf8
# This file is part of Xpra.
# Copyright (C) 2011 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
# Copyright (C) 2010-2014 Antoine Martin <antoine@devloop.org.uk>
# Copyright (C) 2008 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import time
import os

from xpra.log import Logger
log = Logger("window", "encoding")
refreshlog = Logger("window", "refresh")
compresslog = Logger("window", "compress")


AUTO_REFRESH_ENCODING = os.environ.get("XPRA_AUTO_REFRESH_ENCODING", "")
AUTO_REFRESH_THRESHOLD = int(os.environ.get("XPRA_AUTO_REFRESH_THRESHOLD", 95))
AUTO_REFRESH_QUALITY = int(os.environ.get("XPRA_AUTO_REFRESH_QUALITY", 99))
AUTO_REFRESH_SPEED = int(os.environ.get("XPRA_AUTO_REFRESH_SPEED", 0))

MAX_PIXELS_PREFER_RGB = 4096

DELTA = os.environ.get("XPRA_DELTA", "1")=="1"
MAX_DELTA_SIZE = int(os.environ.get("XPRA_MAX_DELTA_SIZE", "10000"))
HAS_ALPHA = os.environ.get("XPRA_ALPHA", "1")=="1"
FORCE_BATCH = os.environ.get("XPRA_FORCE_BATCH", "0")=="1"
STRICT_MODE = os.environ.get("XPRA_ENCODING_STRICT_MODE", "0")=="1"


from xpra.deque import maxdeque
from xpra.server.window_stats import WindowPerformanceStatistics
from xpra.simple_stats import add_list_stats
from xpra.server.batch_delay_calculator import calculate_batch_delay, get_target_speed, get_target_quality
from xpra.server.stats.maths import time_weighted_average
from xpra.gtk_common.region import rectangle, add_rectangle
try:
    from xpra.codecs.xor import xor_str        #@UnresolvedImport
except Exception, e:
    log("cannot load xor module: %s", e)
    xor_str = None
from xpra.server.picture_encode import webp_encode, rgb_encode, PIL_encode, mmap_encode, mmap_send
from xpra.codecs.loader import NEW_ENCODING_NAMES_TO_OLD, PREFERED_ENCODING_ORDER, get_codec
from xpra.codecs.codec_constants import LOSSY_PIXEL_FORMATS, get_PIL_encodings


class WindowSource(object):
    """
    We create a Window Source for each window we send pixels for.

    The UI thread calls 'damage' and we eventually
    call ServerSource.queue_damage to queue the damage compression,

    """

    _encoding_warnings = set()

    def __init__(self, idle_add, timeout_add, source_remove,
                    queue_damage, queue_packet, statistics,
                    wid, window, batch_config, auto_refresh_delay,
                    video_helper,
                    server_core_encodings, server_encodings,
                    encoding, encodings, core_encodings, encoding_options, rgb_formats,
                    default_encoding_options,
                    mmap, mmap_size):
        #scheduling stuff (gobject wrapped):
        self.idle_add = idle_add
        self.timeout_add = timeout_add
        self.source_remove = source_remove

        # mmap:
        self._mmap = mmap
        self._mmap_size = mmap_size

        self.init_vars()

        self.queue_damage = queue_damage                #callback to add damage data which is ready to compress to the damage processing queue
        self.queue_packet = queue_packet                #callback to add a network packet to the outgoing queue
        self.wid = wid
        self.global_statistics = statistics             #shared/global statistics from ServerSource
        self.statistics = WindowPerformanceStatistics()

        self.server_core_encodings = server_core_encodings
        self.server_encodings = server_encodings
        self.encoding = encoding                        #the current encoding
        self.encodings = encodings                      #all the encodings supported by the client
        self.core_encodings = core_encodings            #the core encodings supported by the client
        self.rgb_formats = rgb_formats                  #supported RGB formats (RGB, RGBA, ...) - used by mmap
        self.encoding_options = encoding_options        #extra options which may be specific to the encoder (ie: x264)
        self.encoding_client_options = encoding_options.boolget("client_options")
                                                        #does the client support encoding options?
        self.supports_rgb24zlib = encoding_options.boolget("rgb24zlib")
                                                        #supports rgb (both rgb24 and rgb32..) compression outside network layer (unwrapped)
        self.rgb_zlib = encoding_options.boolget("rgb_zlib", True)      #client supports zlib pixel compression (not to be confused with 'rgb24zlib'...)
        self.rgb_lz4 = encoding_options.boolget("rgb_lz4", False)       #client supports lz4 pixel compression
        self.webp_leaks = encoding_options.boolget("webp_leaks", True)  #all clients leaked memory until this flag got added
        self.generic_encodings = encoding_options.boolget("generic")
        self.supports_transparency = HAS_ALPHA and encoding_options.boolget("transparency")
        self.full_frames_only = encoding_options.boolget("full_frames_only")
        ropts = set(("png", "webp", "rgb", "jpeg"))     #default encodings for auto-refresh
        if self.webp_leaks:
            ropts.remove("webp")                        #don't use webp if the client is going to leak with it!
        ropts = ropts.intersection(set(self.server_core_encodings)) #ensure the server has support for it
        ropts = ropts.intersection(set(self.core_encodings))        #ensure the client has support for it
        self.client_refresh_encodings = encoding_options.strlistget("auto_refresh_encodings", list(ropts))
        self.supports_delta = []
        if xor_str is not None and not window.is_tray():
            self.supports_delta = [x for x in encoding_options.strlistget("supports_delta", []) if x in ("png", "rgb24", "rgb32")]
        self.batch_config = batch_config
        #auto-refresh:
        self.auto_refresh_delay = auto_refresh_delay
        self.video_helper = video_helper
        if window.is_shadow():
            self.max_delta_size = -1

        self.is_OR = window.is_OR()
        self.is_tray = window.is_tray()
        self.has_alpha = window.has_alpha()
        self.window_dimensions = 0, 0
        self.fullscreen = window.get_property("fullscreen")
        self.scaling = window.get_property("scaling")
        self.maximized = False          #set by the client!
        window.connect("notify::scaling", self._scaling_changed)
        window.connect("notify::fullscreen", self._fullscreen_changed)

        #for deciding between small regions and full screen updates:
        self.max_small_regions = 40
        self.max_bytes_percent = 60
        self.small_packet_cost = 1024
        if mmap and mmap_size>0:
            #with mmap, we can move lots of data around easily
            #so favour large screen updates over small packets
            self.max_small_regions = 10
            self.max_bytes_percent = 25
            self.small_packet_cost = 4096

        # general encoding tunables (mostly used by video encoders):
        self._encoding_quality = maxdeque(100)   #keep track of the target encoding_quality: (event time, info, encoding speed)
        self._encoding_speed = maxdeque(100)     #keep track of the target encoding_speed: (event time, info, encoding speed)
        # they may have fixed values:
        self._fixed_quality = default_encoding_options.get("quality", 0)
        self._fixed_min_quality = default_encoding_options.get("min-quality", 0)
        self._fixed_speed = default_encoding_options.get("speed", 0)
        self._fixed_min_speed = default_encoding_options.get("min-speed", 0)

        self.init_encoders()
        self.update_encoding_selection(encoding)
        log("initial encoding for %s: %s", self.wid, self.encoding)


    def update_encoding_selection(self, encoding=None):
        #now we have the real list of encodings we can use:
        #"rgb32" and "rgb24" encodings are both aliased to "rgb"
        common_encodings = [{"rgb32" : "rgb", "rgb24" : "rgb"}.get(x, x) for x in self._encoders.keys() if x in self.core_encodings]
        if self.webp_leaks and "webp" in common_encodings:
            common_encodings.remove("webp")
        self.common_encodings = [x for x in PREFERED_ENCODING_ORDER if x in common_encodings]
        #ensure the encoding chosen is supported by this source:
        self.encoding = self.pick_encoding([encoding]+self.common_encodings)
        self.auto_refresh_encodings = [x for x in self.client_refresh_encodings if x in self.common_encodings]
        log("update_encoding_selection(%s) encoding=%s, common encodings=%s, auto_refresh_encodings=%s", encoding, self.encoding, self.common_encodings, self.auto_refresh_encodings)
        assert self.encoding is not None

    def init_encoders(self):
        self._encoders["rgb24"] = self.rgb_encode
        self._encoders["rgb32"] = self.rgb_encode
        for x in get_PIL_encodings(get_codec("PIL")):
            if x in self.server_core_encodings:
                self._encoders[x] = self.PIL_encode
        #prefer this one over PIL supplied version:
        if "webp" in self.server_core_encodings:
            self._encoders["webp"] = self.webp_encode
        if self._mmap and self._mmap_size>0:
            self._encoders["mmap"] = self.mmap_encode

    def __repr__(self):
        return "WindowSource(%s : %s)" % (self.wid, self.window_dimensions)


    def init_vars(self):
        self.server_core_encodings = []
        self.server_encodings = []
        self.encoding = None
        self.encodings = []
        self.encoding_last_used = None
        self.auto_refresh_encodings = []
        self.core_encodings = []
        self.rgb_formats = []
        self.client_refresh_encodings = []
        self.encoding_options = {}
        self.encoding_client_options = {}
        self.supports_rgb24zlib = False
        self.rgb_zlib = False
        self.rgb_lz4 = False
        self.generic_encodings = []
        self.supports_transparency = False
        self.full_frames_only = False
        self.supports_delta = []
        self.last_pixmap_data = None
        self.suspended = False
        #
        self.auto_refresh_delay = 0
        self.video_helper = None
        self.refresh_timer = None
        self.timeout_timer = None
        self.expire_timer = None
        self.soft_timer = None
        self.soft_expired = 0
        self.max_soft_expired = 5
        self.min_delta_size = 512
        self.max_delta_size = MAX_DELTA_SIZE
        self.is_OR = False
        self.is_tray = False
        self.has_alpha = False
        self.window_dimensions = 0, 0
        self.fullscreen = False
        self.scaling = None
        self.maximized = False
        #
        self.max_small_regions = 0
        self.max_bytes_percent = 0
        self.small_packet_cost = 0
        #
        self._encoding_quality = []
        self._encoding_speed = []
        #
        self._fixed_quality = 0
        self._fixed_min_quality = 0
        self._fixed_speed = 0
        self._fixed_min_speed = 0
        #
        self._damage_delayed = None
        self._damage_delayed_expired = False
        self._sequence = 1
        self._last_sequence_queued = 0
        self._damage_cancelled = 0
        self._damage_packet_sequence = 1
        encoders = {}
        if self._mmap and self._mmap_size>0:
            #we must always be able to send mmap
            #so we can reclaim its space
            encoders["mmap"] = self.mmap_encode
        self._encoders = encoders

    def cleanup(self):
        self.cancel_damage()
        self.statistics.reset()
        log("encoding_totals for wid=%s with primary encoding=%s : %s", self.wid, self.encoding, self.statistics.encoding_totals)
        self.init_vars()
        self._damage_cancelled = float("inf")

    def suspend(self):
        self.cancel_damage()
        self.statistics.reset()
        self.suspended = True

    def resume(self, window):
        self.cancel_damage()
        self.statistics.reset()
        self.suspended = False
        self.refresh(window, {"quality" : 100})

    def refresh(self, window, options={}):
        w, h = window.get_dimensions()
        self.damage(window, 0, 0, w, h, options)


    def set_new_encoding(self, encoding):
        """ Changes the encoder for the given 'window_ids',
            or for all windows if 'window_ids' is None.
        """
        if self.encoding==encoding:
            return
        self.statistics.reset()
        self.last_pixmap_data = None
        self.update_encoding_selection(encoding)

    def _scaling_changed(self, window, *args):
        self.scaling = window.get_property("scaling")
        log("window recommended scaling changed: %s", self.scaling)
        self.reconfigure(False)

    def set_scaling(self, scaling):
        self.scaling = scaling
        self.reconfigure(True)

    def _fullscreen_changed(self, window, *args):
        self.fullscreen = window.get_property("fullscreen")
        log("window fullscreen state changed: %s", self.fullscreen)
        self.reconfigure(False)

    def set_client_properties(self, properties):
        self.maximized = properties.boolget("maximized", False)
        self.client_refresh_encodings = properties.strlistget("encoding.auto_refresh_encodings", self.client_refresh_encodings)
        self.full_frames_only = properties.boolget("encoding.full_frames_only", self.full_frames_only)
        self.supports_transparency = HAS_ALPHA and properties.boolget("encoding.transparency", self.supports_transparency)
        self.encodings = properties.strlistget("encodings", self.encodings)
        self.core_encodings = properties.strlistget("encodings.core", self.core_encodings)
        rgb_formats = properties.strlistget("encodings.rgb_formats", self.rgb_formats)
        if not self.supports_transparency:
            #remove rgb formats with alpha
            rgb_formats = [x for x in rgb_formats if x.find("A")<0]
        self.rgb_formats = rgb_formats
        self.update_encoding_selection(self.encoding)


    def unmap(self):
        self.cancel_damage()
        self.statistics.reset()


    def cancel_damage(self):
        """
        Use this method to cancel all currently pending and ongoing
        damage requests for a window.
        Damage methods will check this value via 'is_cancelled(sequence)'.
        """
        log("cancel_damage() wid=%s, dropping delayed region %s and all sequences up to %s", self.wid, self._damage_delayed, self._sequence)
        #for those in flight, being processed in separate threads, drop by sequence:
        self._damage_cancelled = self._sequence
        self.cancel_expire_timer()
        self.cancel_soft_timer()
        self.cancel_refresh_timer()
        self.cancel_timeout_timer()
        #if a region was delayed, we can just drop it now:
        self._damage_delayed = None
        self._damage_delayed_expired = False
        self.last_pixmap_data = None
        #make sure we don't account for those as they will get dropped
        #(generally before encoding - only one may still get encoded):
        for sequence in self.statistics.encoding_pending.keys():
            if self._damage_cancelled>=sequence:
                try:
                    del self.statistics.encoding_pending[sequence]
                except KeyError:
                    #may have been processed whilst we checked
                    pass

    def cancel_expire_timer(self):
        if self.expire_timer:
            self.source_remove(self.expire_timer)
            self.expire_timer = None

    def cancel_soft_timer(self):
        if self.soft_timer:
            self.source_remove(self.soft_timer)
            self.soft_timer = None

    def cancel_refresh_timer(self):
        if self.refresh_timer:
            self.source_remove(self.refresh_timer)
            self.refresh_timer = None

    def cancel_timeout_timer(self):
        if self.timeout_timer:
            self.source_remove(self.timeout_timer)
            self.timeout_timer = None


    def is_cancelled(self, sequence=None):
        """ See cancel_damage(wid) """
        return self._damage_cancelled>=(sequence or float("inf"))

    def add_stats(self, info, suffix=""):
        """
            Add window specific stats
        """
        prefix = "window[%s]." % self.wid
        #no suffix for metadata (as it is the same for all clients):
        info[prefix+"dimensions"] = self.window_dimensions
        info[prefix+"encoding"+suffix] = self.encoding
        info[prefix+"encoding.mmap"+suffix] = bool(self._mmap) and (self._mmap_size>0)
        if self.encoding_last_used:
            info[prefix+"encoding.last_used"+suffix] = self.encoding_last_used
        info[prefix+"suspended"+suffix] = self.suspended or False
        info[prefix+"property.scaling"+suffix] = self.scaling or (1, 1)
        info[prefix+"property.fullscreen"+suffix] = self.fullscreen or False
        self.statistics.add_stats(info, prefix, suffix)

        #speed / quality properties (not necessarily the same as the video encoder settings..):
        info[prefix+"property.min_speed"+suffix] = self._fixed_min_speed
        info[prefix+"property.speed"+suffix] = self._fixed_speed
        info[prefix+"property.min_quality"+suffix] = self._fixed_min_quality
        info[prefix+"property.quality"+suffix] = self._fixed_quality

        def add_last_rec_info(prefix, recs):
            #must make a list to work on (again!)
            l = list(recs)
            if len(l)>0:
                _, descr, _ = l[-1]
                for k,v in descr.items():
                    info[prefix+"."+k] = v
        quality_list = self._encoding_quality
        if quality_list:
            qp = prefix+"encoding.quality"+suffix
            add_list_stats(info, qp, [x for _, _, x in list(quality_list)])
            add_last_rec_info(qp, quality_list)
        speed_list = self._encoding_speed
        if speed_list:
            sp = prefix+"encoding.speed"+suffix
            add_list_stats(info, sp, [x for _, _, x in list(speed_list)])
            add_last_rec_info(sp, speed_list)
        self.batch_config.add_stats(info, prefix, suffix)

    def calculate_batch_delay(self, has_focus, other_is_fullscreen, other_is_maximized):
        if not self.batch_config.locked:
            calculate_batch_delay(self.wid, self.window_dimensions, has_focus, other_is_fullscreen, other_is_maximized, self.is_OR, self.soft_expired, self.batch_config, self.global_statistics, self.statistics)

    def update_speed(self):
        if self.suspended or self._mmap:
            return
        speed = self._fixed_speed
        if speed<=0:
            min_speed = self.get_min_speed()
            #make a copy to work on (and discard "info")
            speed_data = [(event_time, speed) for event_time, _, speed in list(self._encoding_speed)]
            info, target_speed = get_target_speed(self.wid, self.window_dimensions, self.batch_config, self.global_statistics, self.statistics, min_speed, speed_data)
            speed_data.append((time.time(), target_speed))
            speed = max(min_speed, time_weighted_average(speed_data, min_offset=0.1, rpow=1.2))
            speed = min(99, speed)
        else:
            info = {}
            speed = min(100, speed)
        speed = int(speed)
        log("update_speed() info=%s, speed=%s", info, speed)
        self._encoding_speed.append((time.time(), info, speed))

    def set_min_speed(self, min_speed):
        if self._fixed_min_speed!=min_speed:
            self._fixed_min_speed = min_speed
            self.reconfigure()

    def get_min_speed(self):
        return self._fixed_min_speed

    def set_speed(self, speed):
        if self._fixed_speed != speed:
            prev_speed = self.get_current_speed()
            self._fixed_speed = speed
            #force a reload when switching to/from 100% speed:
            self.reconfigure(force_reload=(speed>99 and prev_speed<=99) or (speed<=99 and prev_speed>99))

    def get_current_speed(self):
        ms = self.get_min_speed()
        s = min(100, self._fixed_speed)
        if s>0:
            return max(ms, s)
        es = self._encoding_speed
        if not es:
            return max(ms, 80)
        return max(ms, es[-1][-1])

    def update_quality(self):
        if self.suspended or self._mmap:
            return
        if self.encoding in ("rgb", "png", "png/P", "png/L"):
            #the user has selected an encoding which does not use quality
            #so skip the calculations!
            return
        quality = self._fixed_quality
        if quality<=0:
            min_quality = self.get_min_quality()
            info, quality = get_target_quality(self.wid, self.window_dimensions, self.batch_config, self.global_statistics, self.statistics, min_quality)
            #make a copy to work on (and discard "info")
            ves_copy = [(event_time, speed) for event_time, _, speed in list(self._encoding_quality)]
            ves_copy.append((time.time(), quality))
            quality = max(min_quality, time_weighted_average(ves_copy, min_offset=0.1, rpow=1.2))
            quality = min(99, quality)
        else:
            info = {}
            quality = min(100, quality)
        quality = int(quality)
        log("update_quality() info=%s, quality=%s", info, quality)
        self._encoding_quality.append((time.time(), info, quality))

    def set_min_quality(self, min_quality):
        self._fixed_min_quality = min_quality

    def get_min_quality(self):
        return self._fixed_min_quality

    def set_quality(self, quality):
        self._fixed_quality = quality

    def get_current_quality(self):
        mq = self.get_min_quality()
        q = min(100, self._fixed_quality)
        if q>0:
            return max(mq, q)
        eq = self._encoding_quality
        if not eq:
            return max(mq, 90)
        return max(mq, eq[-1][-1])

    def reconfigure(self, force_reload=False):
        self.update_quality()
        self.update_speed()


    def damage(self, window, x, y, w, h, options={}):
        """ decide what to do with the damage area:
            * send it now (if not congested)
            * add it to an existing delayed region
            * create a new delayed region if we find the client needs it
            Also takes care of updating the batch-delay in case of congestion.
            The options dict is currently used for carrying the
            "quality" and "override_options" values, and potentially others.
            When damage requests are delayed and bundled together,
            specify an option of "override_options"=True to
            force the current options to override the old ones,
            otherwise they are only merged.
        """
        if self.suspended:
            return
        if w==0 or h==0:
            #we may fire damage ourselves,
            #in which case the dimensions may be zero (if so configured by the client)
            return
        now = time.time()
        if "auto_refresh" not in options:
            log("damage%s", (window, x, y, w, h, options))
            self.statistics.last_damage_events.append((now, x,y,w,h))
        self.global_statistics.damage_events_count += 1
        self.statistics.damage_events_count += 1
        self.statistics.last_damage_event_time = now
        ww, wh = window.get_dimensions()
        if self.window_dimensions != (ww, wh):
            self.statistics.last_resized = time.time()
            self.window_dimensions = ww, wh
        if self.full_frames_only:
            x, y, w, h = 0, 0, ww, wh

        if self._damage_delayed:
            #use existing delayed region:
            if not self.full_frames_only:
                regions = self._damage_delayed[2]
                add_rectangle(regions, x, y, w, h)
            #merge/override options
            if options is not None:
                override = options.get("override_options", False)
                existing_options = self._damage_delayed[4]
                for k,v in options.items():
                    if override or k not in existing_options:
                        existing_options[k] = v
            log("damage(%s, %s, %s, %s, %s) wid=%s, using existing delayed %s regions created %.1fms ago",
                x, y, w, h, options, self.wid, self._damage_delayed[3], now-self._damage_delayed[0])
            return
        elif self.batch_config.delay < self.batch_config.min_delay and not self.batch_config.always:
            #work out if we have too many damage requests
            #or too many pixels in those requests
            #for the last time_unit, and if so we force batching on
            event_min_time = now-self.batch_config.time_unit
            all_pixels = [pixels for _,event_time,pixels in self.global_statistics.damage_last_events if event_time>event_min_time]
            eratio = float(len(all_pixels)) / self.batch_config.max_events
            pratio = float(sum(all_pixels)) / self.batch_config.max_pixels
            if eratio>1.0 or pratio>1.0:
                self.batch_config.delay = self.batch_config.min_delay * max(eratio, pratio)

        delay = options.get("delay", self.batch_config.delay)
        delay = max(delay, options.get("min_delay", 0))
        delay = min(delay, options.get("max_delay", self.batch_config.max_delay))
        packets_backlog = self.statistics.get_packets_backlog()
        pixels_encoding_backlog, enc_backlog_count = self.statistics.get_pixels_encoding_backlog()
        #only send without batching when things are going well:
        # - no packets backlog from the client
        # - the amount of pixels waiting to be encoded is less than one full frame refresh
        # - no more than 10 regions waiting to be encoded
        if not self.must_batch(delay) and (packets_backlog==0 and pixels_encoding_backlog<=ww*wh and enc_backlog_count<=10):
            #send without batching:
            log("damage(%s, %s, %s, %s, %s) wid=%s, sending now with sequence %s", x, y, w, h, options, self.wid, self._sequence)
            actual_encoding = options.get("encoding")
            if actual_encoding is None:
                q = options.get("quality") or self.get_current_quality()
                s = options.get("speed") or self.get_current_speed()
                actual_encoding = self.get_best_encoding(False, w*h, ww, wh, s, q, self.encoding)
            if self.must_encode_full_frame(window, actual_encoding):
                x, y = 0, 0
                w, h = ww, wh
            self.batch_config.last_delays.append((now, delay))
            self.batch_config.last_actual_delays.append((now, delay))
            def damage_now():
                if self.is_cancelled():
                    return
                window.acknowledge_changes()
                self.process_damage_region(now, window, x, y, w, h, actual_encoding, options)
            self.idle_add(damage_now)
            return

        #create a new delayed region:
        regions = [rectangle(x, y, w, h)]
        self._damage_delayed_expired = False
        actual_encoding = options.get("encoding", self.encoding)
        self._damage_delayed = now, window, regions, actual_encoding, options or {}
        log("damage(%s, %s, %s, %s, %s) wid=%s, scheduling batching expiry for sequence %s in %.1f ms", x, y, w, h, options, self.wid, self._sequence, delay)
        self.batch_config.last_delays.append((now, delay))
        self.expire_timer = self.timeout_add(int(delay), self.expire_delayed_region, delay)

    def must_batch(self, delay):
        return FORCE_BATCH or self.batch_config.always or delay>self.batch_config.min_delay


    def expire_delayed_region(self, delay):
        """ mark the region as expired so damage_packet_acked can send it later,
            and try to send it now.
        """
        self.expire_timer = None
        self._damage_delayed_expired = True
        self.may_send_delayed()
        if self._damage_delayed is None:
            #region has been sent
            return
        #the region has not been sent yet because we are waiting for damage ACKs from the client
        if self.soft_expired<self.max_soft_expired:
            #there aren't too many regions soft expired yet
            #so use the "soft timer":
            self.soft_expired += 1
            #we have already waited for "delay" to get here, wait more as we soft expire more regions:
            self.soft_timer = self.timeout_add(int(self.soft_expired*delay), self.delayed_region_soft_timeout)
        else:
            #NOTE: this should never happen...
            #the region should now get sent when we eventually receive the pending ACKs
            #but if somehow they go missing... clean it up from a timeout:
            delayed_region_time = self._damage_delayed[0]
            self.timeout_timer = self.timeout_add(self.batch_config.timeout_delay, self.delayed_region_timeout, delayed_region_time)

    def delayed_region_soft_timeout(self):
        self.soft_timer = None
        if self._damage_delayed is None:
            return
        damage_time = self._damage_delayed[0]
        now = time.time()
        actual_delay = 1000.0*(now-damage_time)
        self.batch_config.last_actual_delays.append((now, actual_delay))
        self.do_send_delayed()
        return False

    def delayed_region_timeout(self, delayed_region_time):
        if self._damage_delayed is None:
            #delayed region got sent
            return False
        region_time = self._damage_delayed[0]
        if region_time!=delayed_region_time:
            #this is a different region
            return False
        #ouch: same region!
        window      = self._damage_delayed[1]
        options     = self._damage_delayed[4]
        log.warn("delayed_region_timeout: something is wrong, is the connection dead?")
        #re-try:
        self._damage_delayed = None
        self.full_quality_refresh(window, options)
        return False

    def may_send_delayed(self):
        """ send the delayed region for processing if there is no client backlog """
        if not self._damage_delayed:
            log("window %s delayed region already sent", self.wid)
            return False
        damage_time = self._damage_delayed[0]
        packets_backlog = self.statistics.get_packets_backlog()
        now = time.time()
        actual_delay = 1000.0*(now-damage_time)
        if packets_backlog>0:
            if actual_delay<self.batch_config.max_delay:
                log("send_delayed for wid %s, delaying again because of backlog: %s packets, batch delay is %s, elapsed time is %.1f ms",
                        self.wid, packets_backlog, self.batch_config.delay, actual_delay)
                #this method will get fired again damage_packet_acked
                return False
            else:
                log.warn("send_delayed for wid %s, elapsed time %.1f is above limit of %.1f - sending now", self.wid, actual_delay, self.batch_config.max_delay)
        else:
            #if we're here, there is no packet backlog, and therefore
            #may_send_delayed() may not be called again by an ACK packet,
            #so we must either process the region now or set a timer to
            #check again later:
            def check_again(delay=actual_delay/10.0):
                delay = int(min(self.batch_config.max_delay, max(10, delay)))
                self.timeout_add(delay, self.may_send_delayed)
                return False
            if self.batch_config.locked and self.batch_config.delay>actual_delay:
                #ensure we honour the fixed delay
                #(as we may get called from a damage ack before we expire)
                return check_again(self.batch_config.delay-actual_delay)
            pixels_encoding_backlog, enc_backlog_count = self.statistics.get_pixels_encoding_backlog()
            ww, wh = self.window_dimensions
            if pixels_encoding_backlog>=(ww*wh):
                log("send_delayed for wid %s, delaying again because too many pixels are waiting to be encoded: %s", self.wid, ww*wh)
                return check_again()
            elif enc_backlog_count>10:
                log("send_delayed for wid %s, delaying again because too many damage regions are waiting to be encoded: %s", self.wid, enc_backlog_count)
                return check_again()
            #no backlog, so ok to send, clear soft-expired counter:
            self.soft_expired = 0
            log("send_delayed for wid %s, batch delay is %.1f, elapsed time is %.1f ms", self.wid, self.batch_config.delay, actual_delay)
        self.batch_config.last_actual_delays.append((now, actual_delay))
        self.do_send_delayed()
        return False

    def do_send_delayed(self):
        self.cancel_timeout_timer()
        self.cancel_soft_timer()
        delayed = self._damage_delayed
        if delayed:
            self._damage_delayed = None
            self.send_delayed_regions(*delayed)
        return False

    def send_delayed_regions(self, damage_time, window, regions, coding, options):
        """ Called by 'send_delayed' when we expire a delayed region,
            There may be many rectangles within this delayed region,
            so figure out if we want to send them all or if we
            just send one full window update instead.
        """
        if self.is_cancelled():
            return
        # It's important to acknowledge changes *before* we extract them,
        # to avoid a race condition.
        window.acknowledge_changes()
        self.do_send_delayed_regions(damage_time, window, regions, coding, options)

    def do_send_delayed_regions(self, damage_time, window, regions, coding, options, exclude_region=None, fallback=None):
        ww,wh = window.get_dimensions()
        speed = options.get("speed") or self.get_current_speed()
        quality = options.get("quality") or self.get_current_quality()
        def get_encoding(pixel_count):
            return self.get_best_encoding(True, pixel_count, ww, wh, speed, quality, coding, fallback)

        def send_full_window_update():
            actual_encoding = get_encoding(ww*wh)
            log("send_delayed_regions: using full window update %sx%s with %s", ww, wh, actual_encoding)
            assert actual_encoding is not None
            self.process_damage_region(damage_time, window, 0, 0, ww, wh, actual_encoding, options)

        if exclude_region is None:
            if self.is_tray or self.full_frames_only:
                send_full_window_update()
                return

            if len(regions)>self.max_small_regions:
                #too many regions!
                send_full_window_update()
                return

        regions = list(set(regions))
        bytes_threshold = ww*wh*self.max_bytes_percent/100
        pixel_count = sum([rect.width*rect.height for rect in regions])
        bytes_cost = pixel_count+self.small_packet_cost*len(regions)
        log("send_delayed_regions: bytes_cost=%s, bytes_threshold=%s, pixel_count=%s", bytes_cost, bytes_threshold, pixel_count)
        if bytes_cost>=bytes_threshold:
            #too many bytes to send lots of small regions..
            if exclude_region is None:
                send_full_window_update()
                return
            #make regions out of the rest of the window area:
            non_exclude = rectangle(0, 0, ww, wh).substract_rect(exclude_region)
            #and keep those that have damage areas in them:
            regions = [x for x in non_exclude if len([y for y in regions if x.intersects_rect(y)])>0]
            #TODO: should verify that is still better than what we had before..

        elif len(regions)>1:
            #try to merge all the regions to see if we save anything:
            merged = regions[0].clone()
            for r in regions[1:]:
                merged.merge_rect(r)
            #remove the exclude region if needed:
            if exclude_region:
                merged_rects = merged.substract_rect(exclude_region)
            else:
                merged_rects = [merged]
            merged_pixel_count = sum([r.width*r.height for r in merged_rects])
            merged_bytes_cost = pixel_count+self.small_packet_cost*len(merged_rects)
            if merged_bytes_cost<bytes_cost or merged_pixel_count<pixel_count:
                #better, so replace with merged regions:
                regions = merged_rects

        #check to see if the total amount of pixels makes us use a fullscreen update instead:
        if len(regions)>1:
            pixel_count = sum([rect.width*rect.height for rect in regions])
            log("send_delayed_regions: %s regions with %s pixels (coding=%s)", len(regions), pixel_count, coding)
            actual_encoding = get_encoding(pixel_count)
            if self.must_encode_full_frame(window, actual_encoding):
                #use full screen dimensions:
                self.process_damage_region(damage_time, window, 0, 0, ww, wh, actual_encoding, options)
                return

        #we're processing a number of regions separately:
        for region in regions:
            actual_encoding = get_encoding(region.width*region.height)
            if self.must_encode_full_frame(window, actual_encoding):
                #we may have sent regions already - which is now wasted, oh well..
                self.process_damage_region(damage_time, window, 0, 0, ww, wh, actual_encoding, options)
                #we can stop here (full screen update will include the other regions)
                return
            self.process_damage_region(damage_time, window, region.x, region.y, region.width, region.height, actual_encoding, options)


    def must_encode_full_frame(self, window, encoding):
        #WindowVideoSource overrides this method
        return self.full_frames_only or self.is_tray


    def get_best_encoding(self, batching, pixel_count, ww, wh, speed, quality, current_encoding, fallback=[]):
        want_alpha = self.is_tray or (self.has_alpha and self.supports_transparency)
        if (STRICT_MODE and current_encoding) or (current_encoding=="png/L" and "png/L" in self.common_encodings):
            #1) strict mode: always use what is selected
            #    (just choose between rgb24 and rgb32 if needed)
            #2) png/L: is a grayscale encoding,
            #   allowing other encodings may well include colours
            #   which would look horribly messy
            if current_encoding=="rgb":
                if want_alpha:
                    return self.pick_encoding(["rgb32", "rgb24"])
                else:
                    return self.pick_encoding(["rgb24", "rgb32"])
            return current_encoding
        if self.is_tray or (self.has_alpha and self.supports_transparency):
            #always use transparent encoding for tray or for transparent windows:
            options = self.transparent_encoding_options(current_encoding, pixel_count, speed, quality)
        else:
            options = self.get_encoding_options(batching, pixel_count, ww, wh, speed, quality, current_encoding)
        if current_encoding is None:
            #add all the fallbacks so something will match
            options += [x for x in fallback+self.common_encodings if x is not None and x not in options]
        e = self.do_get_best_encoding(options, current_encoding, fallback)
        return e

    def do_get_best_encoding(self, options, current_encoding, fallback):
        #non-video encodings: stick to what we have if we can:
        if current_encoding in options:
            return current_encoding
        return self.pick_encoding(options, fallback)

    def pick_encoding(self, encodings, fallback=None):
        """ choose an encoding from the list, or use the fallback """
        matches = [e for e in encodings if e is not None and ({"rgb32" : "rgb", "rgb24" : "rgb"}.get(e, e) in self.common_encodings)]
        if matches:
            return matches[0]
        return {"rgb" : "rgb24"}.get(fallback, fallback)

    def transparent_encoding_options(self, current_encoding, pixel_count, speed, quality):
        current_encoding = {"rgb" : "rgb32"}.get(current_encoding, current_encoding)
        WITH_ALPHA = ["rgb32", "png", "rgb24"]
        if current_encoding in ("png/P", "png/L"):
            #only allow the use of those encodings for transparency
            #if they are already selected as current encoding
            WITH_ALPHA.append(current_encoding)
        #webp is shockingly bad at high res and low speed:
        max_webp = 1024*1024*(200-quality)/100*speed/100
        can_webp = 16384<pixel_count<max_webp
        if can_webp:
            WITH_ALPHA.append("webp")
        options = []
        if current_encoding in WITH_ALPHA:
            #current selection is valid
            options.append(current_encoding)
        if (speed>=75 and quality>=75) or pixel_count<=MAX_PIXELS_PREFER_RGB:
            #rgb is the fastest, and also worth using on small regions:
            options.append("rgb32")
        if not self.is_tray and can_webp:
            #see note above about webp
            options.append("webp")
        if speed<75:
            #png is a bit slow!
            options.append("png")
        #add fallback options:
        v = options+WITH_ALPHA
        if current_encoding in WITH_ALPHA:
            #current selection is valid:
            options.insert(0, current_encoding)
        #log.info("transparent_encoding_options%s=%s", (current_encoding, pixel_count, speed, quality), v)
        return v

    def get_encoding_options(self, batching, pixel_count, ww, wh, speed, quality, current_encoding):
        current_encoding = {"rgb" : "rgb24"}.get(current_encoding, current_encoding)
        if current_encoding in ("rgb24", "rgb32", "png"):
            #the user has selected a lossless encoding:
            quality = 100
        options = []
        #use sliding scale for lossless threshold
        #(high speed favours switching to lossy sooner)
        lossless_q = 89+speed/10
        #calculate the threshold for using rgb
        smult = max(1, (speed-75)/5.0)
        max_rgb = int(MAX_PIXELS_PREFER_RGB * smult * (1 + int(self.is_OR)*2))
        #avoid large areas (too slow), especially at low speed and high quality:
        max_webp = 1024*1024 * (200-quality)/100 * speed/100
        #log.info("get_encoding_options%s lossless_q=%s, smult=%s, max_rgb=%s, max_webp=%s", (batching, pixel_count, ww, wh, speed, quality, current_encoding), lossless_q, smult, max_rgb, max_webp)
        if quality<lossless_q:
            #add lossy options
            ALL_OPTIONS = ["jpeg", "png/P", "png/L"]
            if speed>75 and (quality>80 or pixel_count<max_rgb):
                #high speed and high quality, rgb is still good
                options.append("rgb24")
            if speed>50:
                #at medium to high speed, jpeg is always good
                options.append("jpeg")
            if speed>30 and 16384<pixel_count<max_webp:
                #medium speed: webp compresses well (just a bit slow)
                options.append("webp")
        else:
            #lossless options:
            ALL_OPTIONS = ["png", "rgb24"]
            if speed>75 or pixel_count<max_rgb:
                #high speed, rgb is very good:
                options.append("rgb24")
            if 16384<pixel_count<max_webp:
                #don't enable webp for "true" lossless (q>99) unless speed is high
                #because webp forces speed=100 for true lossless mode
                #also avoid very small and very large areas (both slow)
                options.append("webp")
            #always allow png
            options.append("png")
        if current_encoding in ALL_OPTIONS:
            #current selection is valid:
            options.insert(0, current_encoding)
        return options+[x for x in ALL_OPTIONS if x not in options]


    def free_image_wrapper(self, image):
        """ when not running in the UI thread,
            call this method to free an image wrapper safely
        """
        if image.is_thread_safe():
            image.free()
        else:
            self.idle_add(image.free)


    def process_damage_region(self, damage_time, window, x, y, w, h, coding, options):
        """
            Called by 'damage' or 'send_delayed_regions' to process a damage region.

            Actual damage region processing:
            we extract the rgb data from the pixmap and place it on the damage queue,
            so the damage thread will call make_data_packet_cb which does the actual compression
            This runs in the UI thread.
        """
        if w==0 or h==0:
            return
        if not window.is_managed():
            log("the window %s is not composited!?", window)
            return
        sequence = self._sequence + 1
        if self.is_cancelled(sequence):
            log("get_window_pixmap: dropping damage request with sequence=%s", sequence)
            return

        assert coding is not None
        rgb_request_time = time.time()
        image = window.get_image(x, y, w, h, logger=log)
        if image is None:
            log("get_window_pixmap: no pixel data for window %s, wid=%s", window, self.wid)
            return
        if self.is_cancelled(sequence):
            image.free()
            return

        now = time.time()
        process_damage_time = now
        self._sequence += 1
        log("process_damage_regions: wid=%s, adding %s pixel data to queue, elapsed time: %.1f ms, request rgb time: %.1f ms",
                self.wid, coding, 1000.0*(now-damage_time), 1000.0*(now-rgb_request_time))
        self.statistics.encoding_pending[sequence] = (damage_time, w, h)
        self.queue_damage(self.make_data_packet_cb, window, damage_time, process_damage_time, self.wid, image, coding, sequence, options)

    def make_data_packet_cb(self, window, damage_time, process_damage_time, wid, image, coding, sequence, options):
        """ This function is called from the damage data thread!
            Extra care must be taken to prevent access to X11 functions on window.
        """
        try:
            packet = self.make_data_packet(damage_time, process_damage_time, wid, image, coding, sequence, options)
        finally:
            w = image.get_width()
            h = image.get_height()
            self.free_image_wrapper(image)
            del image
            try:
                del self.statistics.encoding_pending[sequence]
            except KeyError:
                #may have been cancelled whilst we processed it
                pass
        #NOTE: we MUST send it (even if the window is cancelled by now..)
        #because the code may rely on the client having received this frame
        if not packet:
            return
        #queue packet for sending:
        self.queue_damage_packet(packet, damage_time, process_damage_time)
        #now deal with auto refresh:
        if self.auto_refresh_delay<=0 or self.is_cancelled(sequence) or len(self.auto_refresh_encodings)==0 or self._mmap:
            #no: auto-refresh is disabled or not needed
            return
        #safe to call is_managed(), this is not an X11 function:
        if not window.is_managed():
            #no: window is gone
            return
        if options.get("auto_refresh", False):
            #no: this is from an auto-refresh already!
            return
        #the actual encoding used may be different from the global one we specify
        encoding = packet[6]
        client_options = packet[10]     #info about this packet from the encoder
        actual_quality = client_options.get("quality", 0)
        if encoding.startswith("png") or encoding.startswith("rgb"):
            actual_quality = 100
        lossy_csc = client_options.get("csc") in LOSSY_PIXEL_FORMATS
        scaled = client_options.get("scaled_size") is not None
        ww, wh = window.get_dimensions()
        if actual_quality>=AUTO_REFRESH_THRESHOLD and not lossy_csc and not scaled:
            refreshlog("auto refresh: was a high quality %s screen update, ignoring", encoding)
            #lossless already: small region sent lossless or encoding is already lossless
            #it is safe to call this method on window because it does not call down to X11:
            if self.refresh_timer and w*h>=ww*wh*90/100:
                #discard pending auto-refresh since this update covered more than 90% of the window
                self.cancel_refresh_timer()
            #don't change anything: if we have a timer, keep it
            return
        if self.refresh_timer and w*h<ww*wh*20/100:
            #a refresh is already due, and this update is small (20%), don't change anything
            return
        #if we're here: the window is still valid and this was a lossy update,
        #of some form (lossy encoding with low enough quality, or using CSC subsampling, or using scaling)
        #so we need an auto-refresh (re-schedule it if one was due already):
        self.idle_add(self.schedule_auto_refresh, window, options)

    def schedule_auto_refresh(self, window, damage_options):
        """ Must be called from the UI thread: this makes it easier
            to prevent races, and we can call window.get_dimensions() safely
        """
        #NOTE: there is a small potential race here:
        #if the damage packet queue is congested, new damage requests could come in,
        #in between the time we schedule the new refresh timer and the time it fires,
        #and if not batching,
        #we would then do a full_quality_refresh when we should not...
        #re-do some checks that may have changed:
        if not window.is_managed():
            #window is gone
            return
        if not self.auto_refresh_encodings or self.is_cancelled():
            #can happen during cleanup
            return
        #ok, so we will schedule a new refresh - cancel any pending one:
        self.cancel_refresh_timer()
        delay = int(max(50, self.auto_refresh_delay, self.batch_config.delay*4))
        refreshlog("schedule_auto_refresh: (re)scheduling auto refresh timer with delay %s", delay)
        def timer_full_refresh():
            refreshlog("timer_full_refresh()")
            self.refresh_timer = None
            self.full_quality_refresh(window, damage_options)
            return False
        self.refresh_timer = self.timeout_add(delay, timer_full_refresh)

    def full_quality_refresh(self, window, damage_options):
        if not window.is_managed():
            #this window is no longer managed
            return
        if not self.auto_refresh_encodings or self.is_cancelled():
            #can happen during cleanup
            return
        w, h = window.get_dimensions()
        log("full_quality_refresh() for %sx%s window", w, h)
        new_options = damage_options.copy()
        encoding = self.auto_refresh_encodings[0]
        new_options["encoding"] = encoding
        new_options["optimize"] = False
        new_options["auto_refresh"] = True
        new_options["quality"] = AUTO_REFRESH_QUALITY
        new_options["speed"] = AUTO_REFRESH_SPEED
        log("full_quality_refresh() with options=%s", new_options)
        self.damage(window, 0, 0, w, h, options=new_options)

    def queue_damage_packet(self, packet, damage_time, process_damage_time):
        """
            Adds the given packet to the damage_packet_queue,
            (warning: this runs from the non-UI thread 'data_to_packet')
            we also record a number of statistics:
            - damage packet queue size
            - number of pixels in damage packet queue
            - damage latency (via a callback once the packet is actually sent)
        """
        #packet = ["draw", wid, x, y, w, h, coding, data, self._damage_packet_sequence, rowstride, client_options]
        width = packet[4]
        height = packet[5]
        damage_packet_sequence = packet[8]
        actual_batch_delay = process_damage_time-damage_time
        def start_send(bytecount):
            now = time.time()
            self.statistics.damage_ack_pending[damage_packet_sequence] = [now, bytecount, 0, 0, width*height]
        def damage_packet_sent(bytecount):
            now = time.time()
            stats = self.statistics.damage_ack_pending.get(damage_packet_sequence)
            #if we timed it out, it may be gone already:
            if stats:
                stats[2] = now
                stats[3] = bytecount
                damage_out_latency = now-process_damage_time
                self.statistics.damage_out_latency.append((now, width*height, actual_batch_delay, damage_out_latency))
        now = time.time()
        damage_in_latency = now-process_damage_time
        self.statistics.damage_in_latency.append((now, width*height, actual_batch_delay, damage_in_latency))
        self.queue_packet(packet, self.wid, width*height, start_send, damage_packet_sent)

    def damage_packet_acked(self, damage_packet_sequence, width, height, decode_time):
        """
            The client is acknowledging a damage packet,
            we record the 'client decode time' (provided by the client itself)
            and the "client latency".
            If we were waiting for pending ACKs to send an expired damage packet,
            check for it.
            (warning: this runs from the non-UI network parse thread)
        """
        log("packet decoding sequence %s for window %s %sx%s took %.1fms", damage_packet_sequence, self.wid, width, height, decode_time/1000.0)
        if decode_time>0:
            self.statistics.client_decode_time.append((time.time(), width*height, decode_time))
        pending = self.statistics.damage_ack_pending.get(damage_packet_sequence)
        if pending is None:
            log("cannot find sent time for sequence %s", damage_packet_sequence)
            return
        del self.statistics.damage_ack_pending[damage_packet_sequence]
        if decode_time:
            start_send_at, start_bytes, end_send_at, end_bytes, pixels = pending
            bytecount = end_bytes-start_bytes
            #it is possible, though very unlikely,
            #that we get the ack before we've had a chance to call
            #damage_packet_sent, so we must validate the data:
            if bytecount>0 and end_send_at>0:
                self.global_statistics.record_latency(self.wid, decode_time, start_send_at, end_send_at, pixels, bytecount)
        else:
            #something failed client-side, so we can't rely on the delta being available
            self.last_pixmap_data = None
        if self._damage_delayed is not None and self._damage_delayed_expired:
            self.idle_add(self.may_send_delayed)
        if not self._damage_delayed:
            self.soft_expired = 0

    def make_data_packet(self, damage_time, process_damage_time, wid, image, coding, sequence, options):
        """
            Picture encoding - non-UI thread.
            Converts a damage item picked from the 'damage_data_queue'
            by the 'data_to_packet' thread and returns a packet
            ready for sending by the network layer.

            * 'mmap' will use 'mmap_send' + 'mmap_encode' - always if available, otherwise:
            * 'jpeg' and 'png' are handled by 'PIL_encode'.
            * 'webp' uses 'webp_encode'
            * 'h264' and 'vp8' use 'video_encode'
            * 'rgb24' and 'rgb32' use 'rgb_encode'
        """
        if self.is_cancelled(sequence) or self.suspended:
            log("make_data_packet: dropping data packet for window %s with sequence=%s", wid, sequence)
            return  None
        x, y, w, h, _ = image.get_geometry()

        #more useful is the actual number of bytes (assuming 32bpp)
        #since we generally don't send the padding with it:
        psize = w*h*4
        assert w>0 and h>0, "invalid dimensions: %sx%s" % (w, h)
        log("make_data_packet: image=%s, damage data: %s", image, (wid, x, y, w, h, coding))
        start = time.time()
        if self._mmap and self._mmap_size>0 and psize>256:
            mmap_data = mmap_send(self._mmap, self._mmap_size, image, self.rgb_formats, self.supports_transparency)
            if mmap_data:
                #success
                data, mmap_free_size, written = mmap_data
                self.global_statistics.mmap_bytes_sent += written
                self.global_statistics.mmap_free_size = mmap_free_size
                #hackish: pass data to mmap_encode using "options":
                coding = "mmap"         #changed encoding!
                options["mmap_data"] = data

        #if client supports delta pre-compression for this encoding, use it if we can:
        delta = -1
        store = -1
        if DELTA and not (self._mmap and self._mmap_size>0) and (coding in self.supports_delta) and self.min_delta_size<image.get_size()<self.max_delta_size:
            #we need to copy the pixels because some delta encodings
            #will modify the pixel array in-place!
            dpixels = image.get_pixels()[:]
            store = sequence
            lpd = self.last_pixmap_data
            if lpd is not None:
                lw, lh, lcoding, lsequence, ldata = lpd
                if lw==w and lh==h and lcoding==coding and len(ldata)==len(dpixels):
                    #xor with the last frame:
                    delta = lsequence
                    data = xor_str(dpixels, ldata)
                    image.set_pixels(data)

        #by default, don't set rowstride (the container format will take care of providing it):
        encoder = self._encoders.get(coding)
        if encoder is None:
            if self.is_cancelled(sequence):
                return None
            else:
                raise Exception("BUG: no encoder not found for %s" % coding)
        ret = encoder(coding, image, options)
        if ret is None:
            #something went wrong.. nothing we can do about it here!
            return  None

        coding, data, client_options, outw, outh, outstride, bpp = ret
        #check cancellation list again since the code above may take some time:
        #but always send mmap data so we can reclaim the space!
        if coding!="mmap" and (self.is_cancelled(sequence) or self.suspended):
            log("make_data_packet: dropping data packet for window %s with sequence=%s", wid, sequence)
            return  None
        #tell client about delta/store for this pixmap:
        if delta>=0:
            client_options["delta"] = delta
        csize = len(data)
        if store>0:
            if delta>0 and csize>=psize/3:
                #compressed size is more than 33% of the original
                #maybe delta is not helping us, so clear it:
                self.last_pixmap_data = None
                #TODO: could tell the clients they can clear it too
                #(add a new client capability and send it a zero store value)
            else:
                self.last_pixmap_data = w, h, coding, store, dpixels
                client_options["store"] = store
        encoding = coding
        if not self.generic_encodings:
            #old clients use non-generic encoding names:
            encoding = NEW_ENCODING_NAMES_TO_OLD.get(coding, coding)
        #actual network packet:
        packet = ("draw", wid, x, y, outw, outh, encoding, data, self._damage_packet_sequence, outstride, client_options)
        end = time.time()
        compresslog("compress: %5.1fms for %4ix%-4i pixels using %5s with ratio %4.1f%% (%5iKB to %5iKB), delta=%i",
                 (end-start)*1000.0, w, h, coding, 100.0*csize/psize, psize/1024, csize/1024, delta)
        self.global_statistics.packet_count += 1
        self.statistics.packet_count += 1
        self._damage_packet_sequence += 1
        self.statistics.encoding_stats.append((coding, w*h, bpp, len(data), end-start))
        #record number of frames and pixels:
        totals = self.statistics.encoding_totals.setdefault(coding, [0, 0])
        totals[0] = totals[0] + 1
        totals[1] = totals[1] + w*h
        self._last_sequence_queued = sequence
        self.encoding_last_used = coding
        #log("make_data_packet: returning packet=%s", packet[:7]+[".."]+packet[8:])
        return packet


    def webp_encode(self, coding, image, options):
        q = options.get("quality") or self.get_current_quality()
        s = options.get("speed") or self.get_current_speed()
        return webp_encode(coding, image, self.supports_transparency, q, s, options)

    def rgb_encode(self, coding, image, options):
        s = options.get("speed") or self.get_current_speed()
        return rgb_encode(coding, image, self.rgb_formats, self.supports_transparency, s,
                          self.rgb_zlib, self.rgb_lz4, self.encoding_client_options, self.supports_rgb24zlib)

    def PIL_encode(self, coding, image, options):
        #for more information on pixel formats supported by PIL / Pillow, see:
        #https://github.com/python-imaging/Pillow/blob/master/libImaging/Unpack.c
        assert coding in self.server_core_encodings
        q = options.get("quality") or self.get_current_quality()
        s = options.get("speed") or self.get_current_speed()
        return PIL_encode(coding, image, q, s, self.supports_transparency)

    def mmap_encode(self, coding, image, options):
        return mmap_encode(coding, image, options)
