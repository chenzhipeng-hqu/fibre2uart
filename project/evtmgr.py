# !/usr/bin/python3
# -*- coding: utf-8 -*-
# @Time    : 2024/7/28
# @Author  : chenzhipeng3472
# @File    : event manager.py
# ref      : https://zhuanlan.zhihu.com/p/685140763


import os
import sys
import struct

work_path = os.path.join(os.path.dirname(sys.argv[0]), "../")
sys.path.append(os.path.abspath(work_path))
os.chdir(work_path)

# ImportError: unable to find Qt5Core.dll on PATH
# https://blog.csdn.net/zwyact/article/details/99778898  
if hasattr(sys, 'frozen'):
    os.environ['PATH'] = sys._MEIPASS + ";" + os.environ['PATH']

import time
import queue
import threading
from project import log

logger = log.Log(__name__, log_path=os.getcwd()).getlog()

# logger.setLevel(logging.WARNING)

now_time = lambda: int(round(time.time() * 1000))


class EvtManager(object):

    def __init__(self):
        self.que = queue.Queue()
        self.evt_active = threading.Event()
        # {evt:(handler0, handler1...)}
        self._handlers = dict()     # {事件:(处理事件的方法)}
        # self._stat = dict()

        self.evt_active.set()
        self._thread = threading.Thread(target=self._run)
        self._thread.start()

    def __del__(self):
        self.exit()

    def exit(self):
        self.que.queue.clear()
        self.evt_active.clear()
        if self._thread:
            self._thread.join()
        # self.que.join()
        logger.debug('exit')

    def _run(self):
        while self.evt_active.is_set():
            # try:
            # if self.que.empty():
            #     time.sleep(0.01)
            #     continue
            # logger.info('11111111111')
            # logger.info(self.que.qsize())
            # qevt = self.que.get(block=True)
            # qevt = self.que.get(block=False)
            try:
                qevt = self.que.get(block=True, timeout=0.01)
            # except queue.Empty:
            except Exception as err:
                continue
            # qevt = self.que.get_nowait()
            # self._stat[qevt.dict['name']] = qevt.dict
            # self._que_process(qevt)
            if qevt and qevt.evt in self._handlers:
                # self._stat[qevt.dict['name']]['stat'] = 'start'
                # logger.info(self._handlers)
                for handler in self._handlers[qevt.evt]:
                    ret = handler(qevt)
                # self._stat[qevt.dict['name']]['stat'] = 'finish'
            # except:
            #     logger.error('error!')
            self.que.task_done()

    # def _que_process(self, qevt):
    #     if qevt.evt in self._handlers:
    #         # logger.info(self._handlers)
    #         for handler in self._handlers[qevt.evt]:
    #             handler(qevt)

    def add_handler(self, evt, handler):
        # try:
        #     handler_list = self._handlers[evt]
        # except KeyError:
        #     handler_list = []
        #
        # if handler not in handler_list:
        #     handler_list.append(handler)
        #
        # self._handlers[evt] = handler_list

        if evt not in self._handlers:
            # self._handlers[evt] = list()
            self._handlers[evt] = set()
            # logger.info('evt not in _handlers')

        # if handler not in self._handlers[evt]:
            # logger.info('handler not in _handlers[evt]')
            # self._handlers[evt].append(handler)
        self._handlers[evt].add(handler)

    def del_handler(self, evt, handler):
        if evt in self._handlers \
                and handler in self._handlers[evt]:
            # self._handlers[evt].pop(handler)
            # self._handlers[evt].remove(handler)
            self._handlers[evt].discard(handler)

        # if self._handlers[evt]

    def transmit(self, qevt):
        self.que.put(qevt)
        return 0

    def size(self):
        return self.que.qsize()

    def clear(self):
        self.que.queue.clear()
        return 0

    # def evt_get_stat(self, evt_name):
    #     return self._stat[evt_name]

    def send_test(self):
        return 0


class Event:
    def __init__(self, evt=None):
        self.evt = evt      # 事件类型
        self.dict = {}      # 字典用于保存具体的事件数据


if __name__ == '__main__':
    def evt_test_handler(qevt):
        logger.info('1. {}'.format(qevt.dict))

    def evt_test_handler2(qevt):
        logger.info('2. {}'.format(qevt.dict))

    logger.info('\r\n\r\n ---------------- welcom to use -----------------')
    EVT_TEST = 'evt_test'
    EVT_TEST2 = 'evt_test2'
    evtmgr = EvtManager()
    evtmgr.add_handler(EVT_TEST, evt_test_handler)
    evtmgr.add_handler(EVT_TEST2, evt_test_handler2)
    # evtmgr.del_handler(EVT_TEST, evt_test_handler)

    evt = Event(EVT_TEST)
    evt.dict['name'] = 'test_name'
    evt.dict['test0'] = 'this is test0'
    evtmgr.transmit(evt)

    evt2 = Event(EVT_TEST2)
    evt2.dict['name'] = 'test2_name'
    evt2.dict['test2'] = 'this is test2'
    evtmgr.transmit(evt2)

    # evtmgr.evt_transmit(evt)

    # for handler in evtmgr._handlers:
    #     logger.info(type(handler))
    #     logger.info(handler)
    # temp = evtmgr.evt_get_stat('test_name')
    # logger.info(temp)
    # temp = evtmgr.evt_get_stat('test2_name')
    # logger.info(temp)

    time.sleep(1)
    logger.debug('exit')
