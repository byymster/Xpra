#!/usr/bin/env python
# This file is part of Xpra.
# Copyright (C) 2013 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from tests.xpra.codecs.test_encoder import test_encoder, test_performance

def test_enc_xvid():
    print("test_enc_xvid()")
    from xpra.codecs.enc_xvid import encoder #@UnresolvedImport
    test_encoder(encoder)
    test_performance(encoder)


def main():
    test_enc_xvid()


if __name__ == "__main__":
    main()
