# !/usr/bin/python3
# -*- coding: utf-8 -*-
# @Time    : 2022/4/2
# @Author  : chenzhipeng3472 
# @File    : upgrade.py


import os
import sys
import struct

from PySide6.QtCore import (Signal, Slot, QTimer, QCoreApplication, QDate,
                            QDateTime, QLocale, QEvent, QMetaObject, QObject,
                            QPoint, QRect, QThread, QSize, QTime, QUrl, Qt)
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QCursor, QTextCursor,
                           QFont, QFontDatabase, QGradient, QIcon, QAction,
                           QImage, QKeySequence, QLinearGradient, QPainter,
                           QPalette, QPixmap, QRadialGradient, QTransform)
from PySide6.QtWidgets import (QComboBox, QApplication, QMainWindow, QMenuBar,
                               QPushButton, QProgressBar, QFileDialog, QMessageBox,
                               QSizePolicy, QStatusBar, QTextBrowser, QWidget)

work_path = os.path.join(os.path.dirname(sys.argv[0]), "../")
sys.path.append(os.path.abspath(work_path))
os.chdir(work_path)

# ImportError: unable to find Qt5Core.dll on PATH
# https://blog.csdn.net/zwyact/article/details/99778898
if hasattr(sys, 'frozen'):
    os.environ['PATH'] = sys._MEIPASS + ";" + os.environ['PATH']

import json
import time
import serial
import threading
import logging
from enum import Enum, IntEnum, auto
from project import log
from project.evtmgr import EvtManager, Event
from project import modbus
from project.modbus import Cmd, Chx

logger = log.Log(__name__, log_path=os.getcwd()).getlog()
# logger.setLevel(logging.WARNING)

CONFIG_FILE_NAME = './config.json'

now_time = lambda: int(round(time.time() * 1000))

TRY_MAX = int(3)
UPG_EN = True
UPG_FILE_NAME = str('')


class State(Enum):
    IDLE        = auto()
    GET_VERSION = auto()
    CHK_FILE    = auto()
    ENTER_BOOT  = auto()
    SEND_INFO   = auto()
    SEND_DATA   = auto()
    EXIT_BOOT   = auto()
    ERROR       = auto()
    END         = auto()


class UpgCmd(IntEnum):
    NONE = 0x00
    GET_VERSION = 0x01
    START = 2
    STOP = 3
    INFO = 4
    DATA = 5
    ENABLE = 0x06


class Evt(Enum):
    GET_VERSION= auto()
    START = auto()
    STOP = auto()
    INFO = auto()
    DATA = auto()
    ENABLE = auto()


def TimeStampToTime(timestamp):
    timeStruct = time.localtime(timestamp)
    return time.strftime('%Y-%m-%d %H:%M:%S', timeStruct)

def load_params():
    global UPG_EN
    global UPG_FILE_NAME
    with open(CONFIG_FILE_NAME, 'r', encoding='utf-8') as f:
        params = json.load(f)

    UPG_EN = params['UPG_EN']
    logger.info('UPG_EN:%d' % UPG_EN)

    UPG_FILE_NAME = params['UPG_FILE_NAME']

def save_params(fname):
    with open(CONFIG_FILE_NAME, 'r', encoding='utf-8') as f:
        params = json.load(f)

    params['UPG_FILE_NAME'] = fname

    with open(CONFIG_FILE_NAME, 'w', encoding='utf-8') as f:
        json.dump(params, f, indent=4)


class Upgrade_UI(QWidget):
    sig_msgbox = Signal(str, int)
    sig_probar = Signal(int, int)
    sig_stabar = Signal(str, int)
    sig_log = Signal(str)

    def __init__(self, ui, upg):
        super().__init__()
        self.read_version_cnt = None
        self.fname = None
        self.start_time = None
        self.last_open_path = './'
        self.upg = upg
        self.ui = ui
        self.devid_list = list()

        self.upg.set_msg_cb(self.upg_msg_cb)
        self.upg.set_cb(self.upg_cb)
        self.ui.btn_calib_upgrade.clicked.connect(self.btn_calib_upgrade_cb)
        self.ui.btn_calib_select_file.clicked.connect(self.btn_calib_select_file_cb)
        self.ui.btn_calib_read_version.clicked.connect(self.btn_calib_read_version_cb)
        self.ui.radiobtn_calib_ch0.clicked.connect(self.radiobtn_calib_ch0_cb)
        self.ui.radiobtn_calib_ch1.clicked.connect(self.radiobtn_calib_ch1234_cb)
        self.ui.radiobtn_calib_ch2.clicked.connect(self.radiobtn_calib_ch1234_cb)
        self.ui.radiobtn_calib_ch3.clicked.connect(self.radiobtn_calib_ch1234_cb)
        self.ui.radiobtn_calib_ch4.clicked.connect(self.radiobtn_calib_ch1234_cb)

        if UPG_EN:
            self.check_file_exist(UPG_FILE_NAME)
        else:
            self.ui.btn_calib_select_file.setEnabled(False)
            self.ui.btn_calib_upgrade.setEnabled(False)

    def radiobtn_calib_ch0_cb(self, state):
        # logger.info(state)
        if state:
            self.ui.radiobtn_calib_ch1.setChecked(False)
            self.ui.radiobtn_calib_ch2.setChecked(False)
            self.ui.radiobtn_calib_ch3.setChecked(False)
            self.ui.radiobtn_calib_ch4.setChecked(False)

    def radiobtn_calib_ch1234_cb(self, state):
        # logger.info(state)
        if state:
            self.ui.radiobtn_calib_ch0.setChecked(False)

    def btn_calib_upgrade_cb(self):
        if self.ui.btn_calib_read_en.isChecked():
            self.ui.btn_calib_read_en.click()
        if self.ui.btn_calib_adc_en.isChecked():
            self.ui.btn_calib_adc_en.click()
        if self.ui.btn_read_en.isChecked():
            self.ui.btn_read_en.click()
        logger.info('dev_id:%d, fname:%s' % (self.ui.dev_id, self.fname))
        # if self.ui.dev_id == Chx.HOST:
        if self.ui.radiobtn_calib_ch0.isChecked():
            if 'HOST' not in self.fname or 'MAIN' not in self.fname:
                msg = '主通道升级文件错误, 请重新选择'
                logger.info(msg)
                self.sig_msgbox.emit(msg, 0)
                return
        else:
            if 'SLAVE' not in self.fname or 'MAIN' not in self.fname:
                msg = '通道%d升级文件错误, 请重新选择' % self.ui.dev_id
                logger.info(msg)
                self.sig_msgbox.emit(msg, 0)
                return

        self.devid_list.clear()
        if self.ui.radiobtn_calib_ch0.isChecked():
            self.devid_list.append(Chx.HOST)
        else:
            if self.ui.radiobtn_calib_ch1.isChecked():
                self.devid_list.append(Chx.SLV_1)
            if self.ui.radiobtn_calib_ch2.isChecked():
                self.devid_list.append(Chx.SLV_2)
            if self.ui.radiobtn_calib_ch3.isChecked():
                self.devid_list.append(Chx.SLV_3)
            if self.ui.radiobtn_calib_ch4.isChecked():
                self.devid_list.append(Chx.SLV_4)
        logger.info(self.devid_list)
        if len(self.devid_list) > 0:
            self.ui.btn_calib_upgrade.setText('升级中...')
            self.ui.btn_calib_upgrade.setEnabled(False)
            self.upg.start(self.ui.addr, self.devid_list, self.fname)
        else:
            msg = '请选择升级通道！'
            self.sig_msgbox.emit(msg, 0)
        # self.upg.start(self.ui.addr, self.ui.dev_id, self.fname)
        self.start_time = time.time()
        self.sig_probar.emit(0, 1000)

    def check_file_exist(self, fname):
        if os.path.isfile(fname):
            self.last_open_path = os.path.dirname(fname)
            # self.ui.btn_calib_select_file.setText(os.path.basename(self.fname))
            self.ui.btn_calib_select_file.setText(fname)
            self.ui.btn_calib_select_file.setToolTip(fname)
            self.sig_log.emit(fname + '\n')
            self.ui.btn_calib_upgrade.setEnabled(True)
            self.ui.btn_calib_upgrade.setText('开始升级')
            self.fname = fname
            save_params(fname)
            return True
        else:
            logger.info('%s 文件不存在' % fname)
            self.ui.btn_calib_upgrade.setEnabled(False)

        return False

    def btn_calib_select_file_cb(self):
        # if self.ui.dev_id == Chx.HOST:
        if self.ui.radiobtn_calib_ch0.isChecked():
            filter = 'bin files (*HOST*MAIN*.bin);;All files (*)'
        else:
            filter = 'bin files (*SLAVE*MAIN*.bin);;All files (*)'
        fname, ftype = QFileDialog.getOpenFileName(self, '选择升级bin文件', self.last_open_path, filter)
        self.ui.btn_calib_select_file.setToolTip(fname)
        # self.ui.btn_calib_select_file.adjustSize()
        if fname != '':
            self.check_file_exist(fname)

    def btn_calib_read_version_cb(self):
        self.sig_probar.emit(0, 1000)
        self.upg.enable(self.ui.addr, 0, 0, self.rx_ind_upg_enable)
        self.sig_log.emit('获取地址:%02d版本:\n' % self.ui.addr)
        self.read_version_cnt = 0
        self.ui.btn_calib_read_version.setEnabled(False)
        for devid in range(0, 5):
            self.upg.get_version(self.ui.addr, devid ,
                                             self.rx_ind_calib_read_version)
        # self.ui.btn_calib_auto_en.setEnabled(True)
        self.sig_probar.emit(50, 1000)

    def rx_ind_calib_read_version(self, addr, rx_data, size):
        # logger.info("rx size:%d" % size)
        # logger.info(rx_data)
        self.read_version_cnt = self.read_version_cnt + 1
        if self.read_version_cnt >=5:
            self.ui.btn_calib_read_version.setEnabled(True)
        if size == 9 and addr == self.ui.addr and rx_data[1] == modbus.Cmd.UPGRADE:
            devid = rx_data[3]
            version = struct.unpack('>L', bytes(rx_data[5:9]))
            logger.info(hex(version[0]))
            module = (version[0] >> 24) & 0x0f
            year = ((version[0] >> 18) & 0xf) + 2020
            month = (version[0] >> 14) & 0x0f
            day = (version[0] >> 9) & 0x1f
            hour = (version[0] >> 4) & 0x1f
            hard_ware = ((version[0]) & 0xf) + 0x41
            version = '%4d-%02d-%02d_%02d' % (year, month, day, hour)
            msg = '通道%d, module:%d, %s, hw:%c' % (devid, module, version, hard_ware)
            logger.info(msg)
            self.sig_stabar.emit(msg, 2000)
            self.sig_log.emit(msg+'\n')
            self.sig_probar.emit(100, 1000)
        else:
            msg = 'timeout'
            logger.info(msg)
            self.sig_log.emit(msg+'\n')

    def rx_ind_upg_enable(self, addr, rx_data, size):
        logger.info("rx size:%d" % size)
        logger.info(rx_data)
        if size == 6 and addr == rx_data[0] and rx_data[1] == Cmd.UPGRADE:
            return 0
        else:
            return -1

    def upg_cb(self, state, rate):
        if state == State.SEND_INFO:
            self.sig_probar.emit(0, 10)
        elif state == State.SEND_DATA:
            self.sig_probar.emit(rate, 10)
        elif state == State.ERROR:
            self.ui.btn_calib_upgrade.setEnabled(True)
            self.ui.btn_calib_upgrade.setText('开始升级')
            time_dif = (time.time() - self.start_time)
            msg = '升级失败\n用时：%ds' % time_dif
            self.sig_msgbox.emit(msg, 0)
            self.sig_log.emit(msg + '\n')
        elif state == State.END:
            self.ui.btn_calib_upgrade.setEnabled(True)
            self.ui.btn_calib_upgrade.setText('开始升级')
            time_dif = (time.time() - self.start_time)
            msg = '升级完成\n用时：%ds' % time_dif
            self.sig_msgbox.emit(msg, 0)
            self.sig_log.emit(msg + '\n')
            self.sig_probar.emit(100, 1000)

    def upg_msg_cb(self, msg):
        # logger.info(msg)
        self.sig_log.emit(msg)


class Upgrade(object):

    def __init__(self, _modbus):
        # self.f_bin = None
        self.dev_id = int(0)
        self.file_sender = None
        self.num = int(0)
        self.callback = None
        self.evt_send_busy = threading.Event()
        self.evt_send_busy.clear()
        self.evt_active = threading.Event()
        self.evt_active.set()
        self.state_next = State.IDLE
        self.state = self.state_next
        self.file_size = int(1)
        self.send_tell = int(0)
        self.msg_cb = None
        self.module = int(0)
        self.hard_ware = int(0)
        self.version = int(0)
        self.try_cnt = int(0)
        self.stop_flag = int(0)
        self._modbus = _modbus
        self.evtmgr = EvtManager()

        self.evtmgr.add_handler(Evt.GET_VERSION, self.evt_cb)
        self.evtmgr.add_handler(Evt.START, self.evt_cb)
        self.evtmgr.add_handler(Evt.STOP, self.evt_cb)
        self.evtmgr.add_handler(Evt.INFO, self.evt_cb)
        self.evtmgr.add_handler(Evt.DATA, self.evt_cb)
        self.evtmgr.add_handler(Evt.ENABLE, self.evt_cb)

        self.handler = {
            State.IDLE:         self.handler_idle,
            State.GET_VERSION:  self.handler_get_version,
            State.CHK_FILE:     self.handler_chk_file,
            State.ENTER_BOOT: self.handler_enter_boot,
            State.SEND_INFO: self.handler_send_info,
            State.SEND_DATA: self.handler_send_data,
            State.EXIT_BOOT: self.handler_exit_boot,
            State.ERROR:    self.handler_error,
            State.END:      self.handler_end,
        }

        load_params()

        self._thread = threading.Thread(target=self._run)
        self._thread.start()

    def __exit__(self):
        logger.info('__exit__')

    # def __del__(self):
    #     logger.info('__del__')

    def exit(self):
        self.evtmgr.exit()
        self._modbus.exit()
        self.evt_active.clear()
        if self._thread:
            self._thread.join()
            self._thread = None
        logger.debug('exit')

    def evt_cb(self, qevt):
        addr = qevt.dict['addr']
        dev_id = qevt.dict['dev_id']
        sys_cmd = qevt.dict['sys_cmd']
        tx_buffer = qevt.dict['data']
        callback = qevt.dict['callback']
        if 'timeout' in qevt.dict:
            timeout = qevt.dict['timeout']
        else:
            timeout = 2
        return self._modbus.upgrade_cmd(addr, dev_id, sys_cmd, tx_buffer, callback, timeout)

    def get_version(self, addr, dev_id, callback=None):
        if callback:
            evt = Event(Evt.GET_VERSION)
            evt.dict['addr'] = addr
            evt.dict['dev_id'] = dev_id
            evt.dict['sys_cmd'] = UpgCmd.GET_VERSION
            evt.dict['data'] = [0]
            evt.dict['callback'] = callback
            evt.dict['timeout'] = 0.5
            ret = self.evtmgr.transmit(evt)
        else:
            tx_buffer = [0]
            ret = self._modbus.upgrade_cmd(addr, dev_id, UpgCmd.GET_VERSION,
                                    tx_buffer, self.rx_ind_upg_get_version, 1)

        # return ret, self.version
        return ret, self.module, self.version, self.hard_ware

    def rx_ind_upg_get_version(self, addr, rx_data, size):
        logger.info("rx size:%d" % size)
        logger.info(rx_data)
        if size == 9 and addr == rx_data[0] and rx_data[1] == Cmd.UPGRADE:
            version = struct.unpack('>L', bytes(rx_data[5:9]))
            # logger.info(hex(version[0]))
            self.module = (version[0] >> 24) & 0x0f
            year = ((version[0] >> 18) & 0xf) + 2020
            month = (version[0] >> 14) & 0x0f
            day = (version[0] >> 9) & 0x1f
            hour = (version[0] >> 4) & 0x1f
            self.hard_ware = ((version[0]) & 0xf) + 0x40
            self.version = '%4d-%02d-%02d_%02d' % (year, month, day, hour)
            return 0
        else:
            return -1

    def enable(self, addr, dev_id, enable, callback=None):
        tx_buf = struct.pack('<B', enable)
        if callback:
            evt = Event(Evt.ENABLE)
            evt.dict['addr'] = addr
            evt.dict['dev_id'] = dev_id
            evt.dict['sys_cmd'] = UpgCmd.ENABLE
            evt.dict['data'] = tx_buf
            evt.dict['callback'] = callback
            ret = self.evtmgr.transmit(evt)
        else:
            ret = self._modbus.upgrade_cmd(addr, dev_id, UpgCmd.ENABLE,
                                    tx_buf, self.rx_ind_upg_enable)

        # return ret, self.version
        return ret

    def rx_ind_upg_enable(self, addr, rx_data, size):
        logger.info("rx size:%d" % size)
        logger.info(rx_data)
        if size == 6 and addr == rx_data[0] and rx_data[1] == Cmd.UPGRADE:
            return 0
        else:
            return -1

    def enter_bl(self, addr, dev_id, enter, callback=None):
        if enter:
            sys_cmd = UpgCmd.START
        else:
            sys_cmd = UpgCmd.STOP

        if callback:
            evt = Event(Evt.START)
            evt.dict['addr'] = addr
            evt.dict['dev_id'] = dev_id
            evt.dict['sys_cmd'] = sys_cmd
            evt.dict['data'] = [0]
            evt.dict['callback'] = callback
            ret = self.evtmgr.transmit(evt)
        else:
            tx_buffer = [0]
            ret = self._modbus.upgrade_cmd(addr, dev_id, sys_cmd,
                                    tx_buffer, self.rx_ind_upg_enter_bl)

        return ret

    def rx_ind_upg_enter_bl(self, addr, rx_data, size):
        logger.info("rx size:%d" % size)
        logger.info(rx_data)
        if size == 6 and addr == rx_data[0] and rx_data[1] == Cmd.UPGRADE:
            return 0
        else:
            return -1

    def send_info(self, addr, dev_id, file_size, callback=None):
        self.evt_send_busy.set()
        tx_buf = struct.pack('>L', file_size)
        if callback:
            evt = Event(Evt.INFO)
            evt.dict['addr'] = addr
            evt.dict['dev_id'] = dev_id
            evt.dict['sys_cmd'] = UpgCmd.INFO
            evt.dict['data'] = tx_buf
            evt.dict['callback'] = callback
            ret = self.evtmgr.transmit(evt)
        else:
            ret = self._modbus.upgrade_cmd(addr, dev_id, UpgCmd.INFO,
                                    tx_buf, self.rx_ind_upg_send_info)
        return ret

    def rx_ind_upg_send_info(self, addr, rx_data, size):
        logger.info("rx size:%d" % size)
        logger.info(rx_data)
        if size == 6 and addr == rx_data[0] and rx_data[1] == Cmd.UPGRADE:
            self.evt_send_busy.clear()
            return 0
        else:
            return -1

    def send_data(self, addr, dev_id, num, buf, callback=None):
        self.evt_send_busy.set()
        tx_buf = struct.pack('<B', num) + buf
        # logger.info(tx_buf)
        if callback:
            evt = Event(Evt.DATA)
            evt.dict['addr'] = addr
            evt.dict['dev_id'] = dev_id
            evt.dict['sys_cmd'] = UpgCmd.DATA
            evt.dict['data'] = tx_buf
            evt.dict['callback'] = callback
            ret = self.evtmgr.transmit(evt)
        else:
            ret = self._modbus.upgrade_cmd(addr, dev_id, UpgCmd.DATA,
                                    tx_buf, self.rx_ind_upg_send_data)
        return ret

    def rx_ind_upg_send_data(self, addr, rx_data, size):
        # logger.info("rx size:%d" % size)
        # logger.info(rx_data)
        if size == 6 and addr == rx_data[0] and rx_data[1] == Cmd.UPGRADE:
            self.evt_send_busy.clear()
            # if rx_data[5] == 1:
            #     logger.info('继续升级')
            # elif rx_data[5] == 2:
            #     msg = '升级完成'
            #     logger.info(msg)
            # elif rx_data[5] == 3:
            #     logger.info('升级失败')
            return 0
        else:
            return -1

    def start(self, addr, devid_list, fname):
        self.addr = addr
        # self.dev_id = devid_list
        self.devid_list = devid_list
        self.dev_id = self.devid_list.pop(0)
        # logger.info('dev_id:%d' % self.dev_id)
        self.fname = fname
        self.file_sender = None
        self.state_next = State.GET_VERSION

    def handler_idle(self):
        return 0

    def handler_get_version(self):
        ret, module, version, hw = self.get_version(self.addr, self.dev_id)
        msg = '通道%d, module:%d, %s hw:%c\n' % (self.dev_id, module, version, hw)
        msg = msg + '正在升级 通道%d...' % self.dev_id
        logger.info(msg)
        self.msg_cb(msg + '\n')
        self.state_next = State.CHK_FILE
        return 0

    def handler_chk_file(self):
        exist = os.path.exists(self.fname)
        if exist:
            # self.f_bin = open(self.fname, 'rb')
            self.file_size = os.path.getsize(self.fname)
            creat_time = os.path.getmtime(self.fname)
            logger.debug("%s  %s  %d bytes" % (self.fname, TimeStampToTime(creat_time), self.file_size))
            self.state_next = State.ENTER_BOOT
        else:
            msg = '找不到该文件  %s , 请放置该文件到该目录下,放置后自动开始下载' % self.fname
            logger.debug(msg)
            time.sleep(4)
        return 0

    def handler_enter_boot(self):
        ret = self.enable(self.addr, 0, True)
        ret = self.enter_bl(self.addr, self.dev_id, True)
        time.sleep(1)
        self.state_next = State.SEND_INFO
        return 0

    def handler_send_info(self):
        ret = 0
        for i in range(0, 2):
            ret = self.send_info(self.addr, self.dev_id, self.file_size)

        if ret == 0:
            self.send_tell = int(0)
            self.num = int(0)
            self.evt_send_busy.clear()
            self.file_sender = self.send_file_generator(self.fname)
            self.state_next = State.SEND_DATA
        else:
            logger.error('send info fail!')
            self.state_next = State.ERROR
        return 0

    def send_file_generator(self, fname):
        exist = os.path.exists(fname)
        if not exist:
            return -1

        fsize = os.path.getsize(fname)
        num = int(0)
        with open(fname, 'rb') as f_bin:
            while f_bin.tell() < fsize:
                txbuf = f_bin.read(512)
                self.send_data(self.addr, self.dev_id, num, txbuf, self.rx_ind_upg_send_data)
                num = (num + 1) & 0xFF
                msg = 'num:%3d, [%5d/%d]' % (num, f_bin.tell(), fsize)
                logger.info(msg)
                yield f_bin.tell() * 100 / fsize

    def handler_send_data(self):
        if self.evt_send_busy.is_set():
            return 0

        n = next(self.file_sender)
        self.callback(self.state, n)
        if n == 100:
            self.state_next = State.EXIT_BOOT

        # with open(self.fname, 'rb') as f_bin:
        #     f_bin.seek(self.send_tell)
        #     txbuf = f_bin.read(512)
        #     self.send_data(self.addr, self.dev_id, self.num, txbuf, self.rx_ind_upg_send_data)
        #     self.send_tell = f_bin.tell()
        #
        # self.num = (self.num + 1) & 0xFF
        # msg = 'num:%3d, [%5d/%d]' % (self.num, self.send_tell, self.file_size)
        # logger.info(msg)
        #
        # self.callback(self.state, (self.send_tell*100/self.file_size))
        # if self.send_tell >= self.file_size:
        #     self.state_next = State.EXIT_BOOT

        # txbuf = self.f_bin.read(512)
        # self.send_data(self.addr, self.dev_id, self.num, txbuf, self.rx_ind_upg_send_data)
        # self.num = (self.num + 1) & 0xFF
        # msg = 'num:%3d, [%5d/%d]' % (self.num, self.f_bin.tell(), self.file_size)
        # logger.info(msg)
        # self.callback(self.state, (self.f_bin.tell()*100/self.file_size))
        #
        # if self.f_bin.tell() >= self.file_size:
        #     self.state_next = State.EXIT_BOOT

        return 0

    def handler_exit_boot(self):
        self.enter_bl(self.addr, self.dev_id, 0)
        self.enable(self.addr, 0, 0)
        if len(self.devid_list) > 0:
            # logger.info('dev_id:%d' % self.dev_id)
            msg = '通道%d 升级完成.' % self.dev_id
            self.dev_id = self.devid_list.pop(0)
            logger.info(msg)
            self.msg_cb(msg+'\n')
            self.state_next = State.GET_VERSION
        else:
            self.state_next = State.END
        return 0

    def handler_error(self):
        # self.f_bin.close()
        if self.file_sender:
            self.file_sender.close()
        self.state_next = State.IDLE
        return 0

    def handler_end(self):
        # self.f_bin.close()
        if self.file_sender:
            self.file_sender.close()
        self.state_next = State.IDLE
        return 0

    def set_cb(self, callback):
        self.callback = callback

    def set_msg_cb(self, callback):
        self.msg_cb = callback

    def _run(self):
        while self.evt_active.is_set():
            time.sleep(0.001)
            ret = self.handler[self.state]()
            if 0 == ret:
                self.try_cnt = 0
            elif self.try_cnt > TRY_MAX:
                logger.error('try_cnt:%d, state:%s' % (self.try_cnt, self.state))
                self.state_next = State.IDLE
            else:
                self.try_cnt = self.try_cnt + 1

            if self.stop_flag:
                self.stop_flag = False
                self.state_next = State.IDLE

            if self.state != self.state_next:
                # logger.info('%s->%s' % (self.state, self.state_next))
                self.state = self.state_next
                if self.callback:
                    self.callback(self.state, 0)



if __name__ == '__main__':
    logger.info('\r\n\r\n ---------------- welcom to use -----------------')

    addr = 0
    # dev_id = 0
    dev_id = 1
    modbus = modbus.Modbus()
    modbus.set_dev('/dev/ttyUSB1', 38400)
    upg = Upgrade(modbus)

    run_time = now_time()

    fname = 'ELOADER_SLAVE_20250306_A_MAIN.bin'
    # fname = 'ELOADER_HOST_20250226_A_MAIN.bin'
    upg.start(addr, dev_id, fname)

    while upg.state != State.IDLE:
        time.sleep(1)

    logger.info('runing time is {}s '.format((now_time() - run_time) / 1000))

    upg.exit()
