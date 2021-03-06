#!/usr/bin/env python
# coding=utf8
# This file is part of Xpra.
# Copyright (C) 2014 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from xpra.server.region import rectangle

R1 = rectangle(0, 0, 20, 20)
R2 = rectangle(0, 0, 20, 20)
R3 = rectangle(0, 0, 40, 40)
R4 = rectangle(10, 10, 50, 50)
R5 = rectangle(100, 100, 100, 100)


def test_eq():
    assert R1==R2
    assert R1!=R3
    assert R2!=R3

def test_merge():
    r1 = R1.clone()
    r1.merge_rect(R4)
    assert r1.x==0 and r1.x==0 and r1.width==60 and r1.height==60
    r2 = R2.clone()
    r2.merge_rect(R5)
    assert r2.x==0 and r2.y==0 and r2.width==200 and r2.height==200

def test_intersection():
    r1 = rectangle(0, 0, 100, 100)
    r2 = rectangle(50, 50, 200, 200)
    i = r1.intersection_rect(r2)
    assert i.x==50 and i.y==50 and i.width==50 and i.height==50
    i2 = r2.intersection_rect(r1)
    assert i2==i
    r3 = rectangle(100, 100, 50, 50)
    i = r2.intersection_rect(r3)
    assert i==r3
    r4 = rectangle(0, 0, 10, 10)
    i = r3.intersection_rect(r4)
    assert i is None

def test_contains():
    assert R1.contains_rect(R2)
    assert not R1.contains_rect(R3)
    assert R3.contains_rect(R1)
    assert not R1.contains_rect(R4)
    assert not R1.contains_rect(R5)

def test_substract():
    #  ##########          ##########
    #  #        #          ##########
    #  #        #          ##########
    #  #        #          ##########
    #  #   ##   #    ->                + ####  +  ####  +
    #  #   ##   #                        ####     ####
    #  #        #                                          ##########
    #  #        #                                          ##########
    #  #        #                                          ##########
    #  ##########                                          ##########
    r = rectangle(0, 0, 100, 100)
    sub = rectangle(40, 40, 20, 20)
    l = r.substract_rect(sub)
    assert len(l)==4
    #verify total area has not changed:
    total = r.width*r.height
    assert total == sum([r.width*r.height for r in (l+[sub])])
    assert rectangle(0, 0, 100, 40) in l
    assert rectangle(0, 40, 40, 20) in l
    assert rectangle(0, 40, 40, 20) in l
    # at (0,0)
    # ##########
    # #        #
    # #        #
    # #        #
    # #        #         at (50, 50)
    # #        #         ##########
    # #        #         #        #
    # #        #    -    #        #
    # #        #         #        #
    # ##########         #        #
    #                    #        #
    #                    #        #
    #                    #        #
    #                    #        #
    #                    ##########
    r = rectangle(0, 0, 100, 100)
    sub = rectangle(50, 50, 100, 100)
    l = r.substract_rect(sub)
    assert len(l)==2
    assert rectangle(0, 0, 100, 50) in l
    assert rectangle(0, 50, 50, 50) in l
    assert rectangle(200, 200, 0, 0) not in l


def main():
    test_eq()
    test_merge()
    test_intersection()
    test_contains()
    test_substract()


if __name__ == "__main__":
    main()
