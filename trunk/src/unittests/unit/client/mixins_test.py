#!/usr/bin/env python
# This file is part of Xpra.
# Copyright (C) 2016-2017 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import unittest

from xpra.util import AdHocStruct, typedict
from xpra.os_util import monotonic_time
from xpra.client.mixins.network_state import NetworkState
from xpra.client.mixins.mmap_client import MmapClient


class MixinsTest(unittest.TestCase):

	def test_networkstate(self):
		x = NetworkState()
		opts = AdHocStruct()
		opts.pings = True
		x.init(opts)
		assert x.get_caps() is not None
		x.server_capabilities = typedict({"start_time" : monotonic_time()})
		x.parse_server_capabilities()
		assert x.server_start_time>=x.start_time, "server_start_time=%s vs start_time=%s" % (x.server_start_time, x.start_time)
		x.send_info_request()
		packet = ["info-response", {"foo" : "bar"}]
		x._process_info_response(packet)
		assert x.server_last_info.get("foo")=="bar"

	def test_mmap(self):
		x = MmapClient()
		opts = AdHocStruct()
		opts.mmap = "on"
		opts.mmap_group = False
		x.init(opts)
		assert x.get_caps() is not None
		conn = AdHocStruct()
		conn.filename = "/tmp/fake"
		x.setup_connection(conn)
		x.server_capabilities = typedict({
			"mmap.enabled"		: True,
			"mmap.token"		: x.mmap_token,
			"mmap.token_bytes"	: x.mmap_token_bytes,
			"mmap.token_index"	: x.mmap_token_index,
			})
		x.parse_server_capabilities()


def main():
	unittest.main()


if __name__ == '__main__':
	main()
