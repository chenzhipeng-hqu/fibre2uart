# !/usr/bin/python3
# -*- coding: utf-8 -*-
# @Time    : 2020/03/26
# @Author  : 陈志鹏
# @File    : xxx.py

import os
import sys
import time
import logging

class TestYield(object):

    def __init__(self):
        self.fsize = None

    def func(self):
        while True:
            print("before yield")
            x = yield
            print("after yield:", x)

    def send_data(self, fname):
        exist = os.path.exists(fname)
        if exist:
            # self.f_bin = open(self.fname, 'rb')
            self.fsize = os.path.getsize(fname)
            print("%s, %d bytes" % (fname, self.fsize))

            with open(fname, 'rb') as f_bin:
                while f_bin.tell() < self.fsize:
                    txbuf = f_bin.read(512)
                    print('[%5d/%d]' % (f_bin.tell(), self.fsize))
                    # if f_bin.tell() < self.fsize:
                    x = yield (self.fsize - f_bin.tell())
        print('over!')
        return -1


if __name__ == '__main__':
    test_yield = TestYield()
    # func = test_yield.func()
    # next(func)
    # func.send(1)
    # func.send(2)
    # next(func)

    fname = '../datas/ELOADER_SLAVE_20250320_A_MAIN.bin'
    func = test_yield.send_data(fname)
    # next(func)
    # x = func.send(1)
    # func.close()
    for chunk in func:
        print(chunk)
        # next(func)
    func.close()
