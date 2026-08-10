#
# -*- coding: utf-8 -*-

import os

from PyInstaller.__main__ import run

'''
__bref__    : In this example, we create a simple window in PyQt5.
__author__  : chenzhipeng3472
__data__    : 04-July-2018

# https://blog.csdn.net/weixin_39000819/article/details/80942423
# -n: 生成的.exe文件和.spec的文件名, 默认用户脚本的名称
# -y: 如果dist文件夹内已经存在生成文件，则不询问用户，直接覆盖, 默认：询问是否覆盖
# -F, --onefile: 打包成一个EXE文件
# -D, --onedir: 创建一个目录，包含exe文件，但会依赖很多文件（默认选项）
# -w, –windowed, –noconsole: 不带console输出控制台，window窗体格式
# -c, –console, –nowindowed 使用控制台，无界面(默认)
# -p, --paths：依赖包路径
# -i, --icon：图标
# --noupx：不用upx压缩
# --clean：清理掉临时文件
# –-distpath: 在哪里放置捆绑的应用程序（默认：./dist）
# –-workpath: 在哪里放置所有的临时工作文件，.log，.pyz等（默认：./build）
# -–specpath: 存储生成的spec文件的文件夹（默认：当前目录）

'''

import platform

if __name__ == '__main__':

    try:
        opts = ['../project/main.py',
                '-D',
                '-y',
				'-n=eloader_upgrade',
                '--clean',
                '--distpath=../datas',
                '-i=../datas/image.ico',
                '--exclude-module=Pyside'
                ]
        platform_info = platform.system()
        if platform_info == 'Windows':
            opts += [
                ]
        elif platform_info == 'Linux':
            opts += [
                '-p=/home/chenzhipeng3472/anaconda3/lib/python3.7/site-packages/PyQt5',
                ]
        else:
            print(platform_info, '!!!!!!')

        run(opts)

    except Exception as err:
        print(err)
        print('\r\n     build error!!!\r\n')
        while True:
            pass

    finally:
        input('\r\n 请按回车键结束。\r\n')
