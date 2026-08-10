# !/usr/bin/python3
# -*- coding: utf-8 -*-
# @Time    : 2020/03/26
# @Author  : 陈志鹏
# @File    : xxx.py

import unittest
from project import log
# from test_all import *

logger = log.Log(__name__).getlog()

def test_suite():
    '''
    https://blog.csdn.net/xiaoquantouer/article/details/75089200
    '''

    suite = unittest.TestSuite()

    # tests = [TestDict("test_init"),
    #          TestDict("test_key"),
    #          TestDict("test_attr"),
    #          TestDict("test_keyerror"),
    #          TestDict("test_attrerror")]
    #
    # suite.addTests(tests)

    with open('UnittestTextReport.txt', 'w') as f:
        runner = unittest.TextTestRunner(stream=f, verbosity=2)
        runner.run(suite)

    # runner = unittest.TextTestRunner(verbosity=2)
    # runner.run(suite)


if __name__ == '__main__':
    test_suite()