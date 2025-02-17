from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, pyqtSlot, QTimer
from PyQt5.QtGui import QIcon, QTextCursor
from napari_plugin_engine import napari_hook_implementation
import napari
import numpy as np
from ctypes import *
import os
import warnings
import subprocess
import cv2
import time
import queue
import json
import pickle
import csv
from threading import Lock
from astropy.stats import sigma_clipped_stats
import multiprocessing
from multiprocessing import shared_memory
from datetime import datetime, timedelta
import pytz

# Import custom modules
from .save_video import SaveThread
from .histogramWidget import HistogramWidget
from .memory_updated import MemoryMonitorThread
from .setting import SettingsDialog
from .language import translations
from .fits_header import FitsHeaderEditor
from .auto_exposure import AutoExposureDialog
from .auto_white_balance import AutoWhiteBalanceDialog
from .astrometry import AstrometrySolver, AstrometryDialog
from .planned_shooting import PlannedShootingDialog
from .qhyccd_sdk import QHYCCDSDK
from .accept_sdk_data import AcceptSDKData

class CameraControlWidget(QWidget):
    def __init__(self, napari_viewer):
        super().__init__()
        self.viewer = napari_viewer
        self.initialize_settings()
        
        self.init_parameters()
        self.init_ui()
        self.init_sdk()
        
        self.viewer.layers.selection.events.changed.connect(self.on_selection_changed)
        self.append_text(translations[self.language]['qhyccd_capture']['init_complete'])

        '''初始化相机资源'''
        try:
            if self.settings_dialog.qhyccd_path_label.text() is None or self.settings_dialog.qhyccd_path_label.text() == "" or self.settings_dialog.qhyccd_path_label.text() == " ":
                self.init_qhyccdResource()
            else:
                self.init_qhyccdResource(self.settings_dialog.qhyccd_path_label.text())
        except Exception as e:
            self.append_text(f"{translations[self.language]['debug']['init_failed']}: {str(e)}",True)

    def cleanup(self):
        self.disconnect_camera()
        self.release_qhyccd_resource()
 
    def clear_queue(self,q):
        try:
            while True:  # 持续尝试获取元素直到队列为空
                q.get_nowait()
        except queue.Empty:
            pass 

    def release_qhyccd_resource(self):
        self.sdk_input_queue.put({"order":"stop", "data":''})
        self.memory_monitor_thread.stop()
    
    def stop_qhyccd_process_success(self):
        self.qhyccd_process = None
        self.preview_state = False
        self.clear_queue(self.sdk_input_queue)
        self.clear_queue(self.sdk_output_queue)
        self.init_sdk()
        if self.settings_dialog.qhyccd_path_label.text() is None or self.settings_dialog.qhyccd_path_label.text() == "" or self.settings_dialog.qhyccd_path_label.text() == " ":
            self.init_qhyccdResource()
        else:
            self.init_qhyccdResource(self.settings_dialog.qhyccd_path_label.text())

    def init_parameters(self):
        self.system_name = os.name
        
        self.sdk_input_queue = None
        self.sdk_output_queue = None
        
        # 初始化相机状态
        self.init_state = False
        self.camera_state = False
        
        self.camera = None
        self.camera_name = None
        
        self.shm1 = None
        self.shm2 = None
        
        # 初始化对比度限制连接
        self.contrast_limits_connection = None
        
        self.histogram_window = None  # 用于存储直方图窗口
        self.histogram_layer_name = None # 用于存储直方图显示的图层名称

        # 初始化ROI区域
        self.roi_layer = None
        self.roi_created = False

        # 初始化相机分辨率
        self.camera_W = 0
        self.camera_H = 0
        self.camera_bit = 0
        
        # 初始化像素合并Bin
        self.bin = [1,1]

        # 初始化图像分辨率
        self.image_x = 0
        self.image_y = 0
        self.image_w = 0
        self.image_h = 0
        self.image_c = 1
        
        # 初始化相机ID
        self.camera_ids = None
        self.camhandle = 0
        self.init_camera_id = -1
        
        # # 初始化图像队列
        # self.buffer_queue = queue.Queue()  # 用于保存图像数据的队列

        # 初始化时间
        self.last_update_time = None
        self.last_histogram_update_time = None
        self.is_recording = False
        
        # 初始化文件格式
        self.file_format = None
        
        # 初始化锁
        self.lock = Lock()
        
        # 初始化相机连接线程
        self.camera_connection_thread = None
        
        # 初始化当前图像
        self.current_image = None
        self.current_image_name = None
        self.preview_image = None
        self.contrast_limits_name = None
    
        self.is_color_camera = False  # 新增变量，判断相机是否是彩色相机
        
        self.planned_shooting_dialog = PlannedShootingDialog(self,language=self.language) # 创建计划拍摄对话框
        self.planned_shooting_dialog.plan_running_signal.connect(self.on_plan_running)
        
        self.temperature_update_timer = QTimer(self)
        self.temperature_update_timer.timeout.connect(self.update_current_temperature)
        
        self.humidity_update_timer = QTimer(self)
        self.humidity_update_timer.timeout.connect(self.update_current_humidity)
        
        self.is_CFW_control = False  # 新增变量，判断相机是否连接滤镜轮
        
        self.planned_shooting_data = None
        
        self.GPS_control = False
        
        self.roi_points = []
        self.roi_layer = None
        self.roi_created = False
        self.viewer.mouse_drag_callbacks.append(self.on_mouse_click)
        self.viewer.mouse_double_click_callbacks.append(self.on_mouse_double_click)
        self.star_table = None
        # 星点解析库
        self.astrometrySolver = None
        try:
            self.astrometrySolver = AstrometrySolver(language=self.language)
            self.astrometrySolver.finished.connect(self.on_astrometry_finished)
            self.astrometrySolver.error.connect(self.on_astrometry_error)
            self.astrometrySolver.star_info.connect(self.on_astrometry_star_info)
        except Exception as e:
            self.append_text(f"Failed to initialize AstrometrySolver: {str(e)}",True)

    def init_ui(self):
        # 创建一个主布局
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_content = QWidget()
        # 将原有的布局添加到滚动区域的内容布局中
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_area.setWidget(self.scroll_content)
        
        
        self.initialize_histogram_and_memory_monitor()
        self.init_start_settings_ui()
        self.init_settings_ui()
        self.init_capture_control_ui()
        self.init_video_control_ui()
        self.init_image_control_ui()
        self.init_temperature_control_ui()
        self.init_CFW_control_ui()
        self.init_external_trigger_ui()
        self.init_GPS_ui()
        self.init_ui_state()
        
    def init_sdk(self):
        if self.sdk_input_queue is None:
            self.sdk_input_queue = multiprocessing.Queue()
        if self.sdk_output_queue is None:
            self.sdk_output_queue = multiprocessing.Queue()
        self.qhyccd_process = QHYCCDSDK(self.sdk_input_queue, self.sdk_output_queue,self.language)
        self.qhyccd_process.start()
        self.accept_sdk_data = AcceptSDKData(self.sdk_output_queue)
        self.accept_sdk_data.data_signal.connect(self.on_sdk_data_received)
        self.accept_sdk_data.start()
        
    def initialize_settings(self):
        # 加载配置
        self.settings_file = "settings.json"  # 设置文件路径
        self.load_settings()  # 加载设置
        self.luts = {}
        if os.path.exists('luts.pkl'):
            with open('luts.pkl', 'rb') as f:
                self.luts = pickle.load(f)
        else:
            self.create_luts([255, 65535], 0, 2.0, 1/100)

    def initialize_histogram_and_memory_monitor(self):
        # 初始化直方图和内存监控
        self.histogram_window = None
        self.memory_monitor_thread = MemoryMonitorThread()
        self.memory_monitor_thread.memory_updated.connect(self.update_memory_progress)
        self.memory_monitor_thread.start()

    def init_start_settings_ui(self):
        self.connect_box = QGroupBox(translations[self.language]['qhyccd_capture']['connect_settings'])
        start_setting_layout = QFormLayout()
        
        # 设置设置按钮
        # 创建一个水平布局
        h_layout = QHBoxLayout()
        
        self.settings_dialog = SettingsDialog(self)
        self.settings_button = QPushButton(translations[self.language]['qhyccd_capture']['settings'])
        self.settings_button.clicked.connect(self.show_settings_dialog)
        
        self.state_label = QTextEdit()
        self.state_label.setReadOnly(True)
        self.state_label.setLineWrapMode(QTextEdit.NoWrap)
        self.state_label.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # type: ignore
        self.state_label.setFixedHeight(self.state_label.fontMetrics().height() + 10)  # 设置固定高度为一行文本的高度加上一些边距
        self.state_label.setStyleSheet("""
            QScrollBar:vertical { width: 2px; }
        """)  # 设置垂直滚动条的宽度为2像素
        
        # 将控件添加到水平布局中
        h_layout.addWidget(QLabel(translations[self.language]['qhyccd_capture']['status']))
        h_layout.addWidget(self.state_label)
        h_layout.addWidget(self.settings_button)
        
        # 将水平布局添加到表单布局中
        start_setting_layout.addRow(h_layout)
        # 相机选择
        self.camera_selector = QComboBox()
        self.camera_selector.currentIndexChanged.connect(self.on_camera_changed)

        start_setting_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['select_camera']),self.camera_selector)
        
         # 读出模式选择框
        self.readout_mode_name_dict= {}
        self.readout_mode_selector = QComboBox()
        self.readout_mode_selector.addItems(list(self.readout_mode_name_dict.keys()))  # 示例项
        # self.readout_mode_selector.currentIndexChanged.connect(self.on_readout_mode_changed)
        start_setting_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['readout_mode']), self.readout_mode_selector)
        
        # 相机模式选择
        self.camera_mode_selector = QComboBox()
        # self.camera_mode_selector.currentIndexChanged.connect(self.on_camera_mode_changed)
        start_setting_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['select_camera_mode']),self.camera_mode_selector)
        
        # 连接和断开按钮
        grid_layout = QGridLayout()
        self.connect_button = QPushButton(translations[self.language]['qhyccd_capture']['connect'])
        self.disconnect_button = QPushButton(translations[self.language]['qhyccd_capture']['disconnect'])
        self.reset_camera_button = QPushButton(translations[self.language]['qhyccd_capture']['reset_camera'])
        
        self.connect_button.clicked.connect(self.connect_camera)
        self.connect_button.setEnabled(False)
        self.disconnect_button.clicked.connect(self.disconnect_camera)
        self.disconnect_button.setEnabled(False)
        self.reset_camera_button.clicked.connect(self.read_camera_name)
        self.reset_camera_button.setEnabled(False)
        
        grid_layout.addWidget(self.connect_button,0,0)
        grid_layout.addWidget(self.disconnect_button,0,1)
        grid_layout.addWidget(self.reset_camera_button,0,2)
        start_setting_layout.addRow(grid_layout)
        grid_layout = QGridLayout()
        # 相机配置显示
        self.config_label = QLabel(translations[self.language]['qhyccd_capture']['not_connected'])
        self.config_label.setStyleSheet("color: red;")  # 设置字体颜色为红色
        grid_layout.addWidget(self.config_label,0,0)
        
        self.fps_label = QLabel(translations[self.language]['qhyccd_capture']['fps'])
        self.fps_label.setVisible(False)
        grid_layout.addWidget(self.fps_label,0,1)
        
        self.memory_label = QLabel(translations[self.language]['qhyccd_capture']['memory'])
        grid_layout.addWidget(self.memory_label,0,2)
        
        self.memory_progress_bar = QProgressBar(self)  # 创建进度条控件
        self.memory_progress_bar.setRange(0, 100)  # 设置进度条范围（0% 到 100%）
        self.memory_progress_bar.setValue(0)
        
        grid_layout.addWidget(self.memory_progress_bar,0,3)
        start_setting_layout.addRow(grid_layout)
        
        # 区显示开关
        grid_layout = QGridLayout()
        
        self.show_settings_checkbox = QPushButton()
        self.show_settings_checkbox.setIcon(QIcon(os.path.abspath(os.path.join(os.path.dirname(__file__), 'icon/camera_icon.png'))))  # 设置图标路径
        self.show_settings_checkbox.setToolTip(translations[self.language]['qhyccd_capture']['show_camera_settings'])  # 设置提示文本

        self.show_control_checkbox = QPushButton()
        self.show_control_checkbox.setIcon(QIcon(os.path.abspath(os.path.join(os.path.dirname(__file__), 'icon/instagram_icon.png'))))  # 设置图标路径
        self.show_control_checkbox.setToolTip(translations[self.language]['qhyccd_capture']['show_capture_control'])

        self.show_image_control_checkbox = QPushButton()
        self.show_image_control_checkbox.setIcon(QIcon(os.path.abspath(os.path.join(os.path.dirname(__file__), 'icon/film_icon.png'))))  # 设置图标路径
        self.show_image_control_checkbox.setToolTip(translations[self.language]['qhyccd_capture']['show_image_control'])

        self.show_temperature_control_checkbox = QPushButton()
        self.show_temperature_control_checkbox.setIcon(QIcon(os.path.abspath(os.path.join(os.path.dirname(__file__), 'icon/thermometer_icon.png'))))  # 设置图标路径
        self.show_temperature_control_checkbox.setToolTip(translations[self.language]['qhyccd_capture']['show_temperature_control'])

        self.show_CFW_control_checkbox = QPushButton()
        self.show_CFW_control_checkbox.setIcon(QIcon(os.path.abspath(os.path.join(os.path.dirname(__file__), 'icon/toggle_right_icon.png'))))  # 设置图标路径
        self.show_CFW_control_checkbox.setToolTip(translations[self.language]['qhyccd_capture']['show_cfw_control'])

        self.show_video_control_checkbox = QPushButton()
        self.show_video_control_checkbox.setIcon(QIcon(os.path.abspath(os.path.join(os.path.dirname(__file__), 'icon/video_icon.png'))))  # 设置图标路径
        self.show_video_control_checkbox.setToolTip(translations[self.language]['qhyccd_capture']['show_video_control'])
        
        self.show_external_trigger_checkbox = QPushButton()
        self.show_external_trigger_checkbox.setIcon(QIcon(os.path.abspath(os.path.join(os.path.dirname(__file__), 'icon/trigger_icon.png'))))  # 设置图标路径
        self.show_external_trigger_checkbox.setToolTip(translations[self.language]['qhyccd_capture']['show_external_trigger'])
        
        self.show_GPS_control_checkbox = QPushButton()
        self.show_GPS_control_checkbox.setIcon(QIcon(os.path.abspath(os.path.join(os.path.dirname(__file__), 'icon/GPS_icon.png'))))  # 设置图标路径
        self.show_GPS_control_checkbox.setToolTip(translations[self.language]['qhyccd_capture']['show_GPS_control'])
        
        self.show_settings_checkbox.clicked.connect(lambda: self.toggle_settings_box())
        self.show_control_checkbox.clicked.connect(lambda: self.toggle_control_box())
        self.show_image_control_checkbox.clicked.connect(lambda: self.toggle_image_control_box())
        self.show_temperature_control_checkbox.clicked.connect(lambda: self.toggle_temperature_control_box())
        self.show_CFW_control_checkbox.clicked.connect(lambda: self.toggle_CFW_control_box())
        self.show_video_control_checkbox.clicked.connect(lambda: self.toggle_video_control_box())
        self.show_external_trigger_checkbox.clicked.connect(lambda: self.toggle_external_trigger_box())
        self.show_GPS_control_checkbox.clicked.connect(lambda: self.toggle_GPS_control_box())
        
        grid_layout.addWidget(self.show_settings_checkbox,1,0)
        grid_layout.addWidget(self.show_control_checkbox,1,1)
        grid_layout.addWidget(self.show_image_control_checkbox,1,2)
        grid_layout.addWidget(self.show_video_control_checkbox,1,3)
        grid_layout.addWidget(self.show_temperature_control_checkbox,1,4)
        grid_layout.addWidget(self.show_CFW_control_checkbox,1,5)
        grid_layout.addWidget(self.show_external_trigger_checkbox,1,6)
        grid_layout.addWidget(self.show_GPS_control_checkbox,1,7)
        start_setting_layout.addRow(grid_layout)
        
        # 创建一个垂直方向的 spacer
        spacer = QSpacerItem(20, 2, QSizePolicy.Minimum, QSizePolicy.Expanding)

        # 将 spacer 添加到布局的底部
        start_setting_layout.addItem(spacer)
        
        # 将 start_setting_layout 包装在一个 QWidget 中
        self.connect_box.setLayout(start_setting_layout)
        self.scroll_layout.addWidget(self.connect_box)
        
    def init_settings_ui(self):
        self.settings_box = QGroupBox(translations[self.language]['qhyccd_capture']['camera_settings'])
        settings_layout = QFormLayout()

        self.burst_mode = False
        self.burst_mode_selector = QCheckBox(translations[self.language]['qhyccd_capture']['burst_mode'])
        self.burst_mode_selector.stateChanged.connect(self.toggle_burst_control_box)
        self.burst_mode_selector.setVisible(False)
        settings_layout.addRow(self.burst_mode_selector)

        # 创建水平布局以在同一行显示最小值和最大值输入控件
        value_range_layout = QHBoxLayout()

        # 添加最小值输入控件
        self.burst_mode_min_value_selector_label = QLabel(translations[self.language]['qhyccd_capture']['min_value'])
        self.burst_mode_min_value_selector = QSpinBox()
        self.burst_mode_min_value_selector.setRange(0, 1000)  # 设置最小值和最大值范围
        self.burst_mode_min_value_selector.setValue(0)  # 设置默认值
        self.burst_mode_min_value_selector.setVisible(False)
        self.burst_mode_min_value_selector_label.setVisible(False)
        self.burst_mode_min_value_selector.valueChanged.connect(self.on_burst_mode_min_value_changed)
        value_range_layout.addWidget(self.burst_mode_min_value_selector_label)
        value_range_layout.addWidget(self.burst_mode_min_value_selector)

        # 添加最大值输入控件
        self.burst_mode_max_value_selector_label = QLabel(translations[self.language]['qhyccd_capture']['max_value'])
        self.burst_mode_max_value_selector = QSpinBox()
        self.burst_mode_max_value_selector.setRange(0, 1000)  # 设置最小值和最大值范围
        self.burst_mode_max_value_selector.setValue(10)  # 设置默认值
        self.burst_mode_max_value_selector.setVisible(False)
        self.burst_mode_max_value_selector_label.setVisible(False)
        self.burst_mode_max_value_selector.valueChanged.connect(self.on_burst_mode_max_value_changed)
        value_range_layout.addWidget(self.burst_mode_max_value_selector_label)
        value_range_layout.addWidget(self.burst_mode_max_value_selector)

        # 将水平布局添加到设置布局中
        settings_layout.addRow(value_range_layout)

        # 像素合并Bin选择框
        self.camera_pixel_bin = {'1*1':[1,1],'2*2':[2,2],'3*3':[3,3],'4*4':[4,4]}
        self.pixel_bin_selector = QComboBox()
        self.pixel_bin_selector.addItems(list(self.camera_pixel_bin.keys()))  # 示例项
        self.pixel_bin_selector.currentIndexChanged.connect(self.on_pixel_bin_changed)
        settings_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['pixel_bin']), self.pixel_bin_selector)

        # 图像位深度选择框
        self.camera_depth_options = {}
        self.depth_selector = QComboBox()  # 创建图像位深度选择框
        self.depth_selector.addItems(list(self.camera_depth_options.keys()))  
        self.depth_selector.currentIndexChanged.connect(self.on_depth_changed)
        self.depth_name = QLabel(translations[self.language]['qhyccd_capture']['image_depth'])
        settings_layout.addRow(self.depth_name, self.depth_selector)
        
        self.camera_Debayer_mode = {translations[self.language]['qhyccd_capture']['debayer_mode_true']:True,translations[self.language]['qhyccd_capture']['debayer_mode_false']:False}
        self.Debayer_mode = False
        self.Debayer_mode_selector = QComboBox()
        self.Debayer_mode_selector.addItems(list(self.camera_Debayer_mode.keys()))
        self.Debayer_mode_selector.currentIndexChanged.connect(self.on_Debayer_mode_changed)
        self.Debayer_mode_label = QLabel(translations[self.language]['qhyccd_capture']['debayer_mode'])
        settings_layout.addRow(self.Debayer_mode_label, self.Debayer_mode_selector)
        
        # 分辨率选择框
        # 变量定义
        self.x = QSpinBox()
        self.y = QSpinBox()
        self.w = QSpinBox()
        self.h = QSpinBox()
        self.set_resolution_button = QPushButton(translations[self.language]['qhyccd_capture']['set_resolution'])
        self.set_original_resolution_button = QPushButton(translations[self.language]['qhyccd_capture']['reset_resolution'])
        self.set_original_resolution_button.setToolTip(translations[self.language]['qhyccd_capture']['reset_resolution_tooltip'])
        self.show_roi_button = QPushButton(translations[self.language]['qhyccd_capture']['roi'])
        self.show_roi_button.setToolTip(translations[self.language]['qhyccd_capture']['roi_tooltip'])

        # 布局设置
        grid_layout = QGridLayout()
        grid_layout.addWidget(QLabel(translations[self.language]['qhyccd_capture']['x']),0,0)
        grid_layout.addWidget(self.x,0,1)
        grid_layout.addWidget(QLabel(translations[self.language]['qhyccd_capture']['y']),0,2)
        grid_layout.addWidget(self.y,0,3)
        grid_layout.addWidget(QLabel(translations[self.language]['qhyccd_capture']['w']),1,0)
        grid_layout.addWidget(self.w,1,1)
        grid_layout.addWidget(QLabel(translations[self.language]['qhyccd_capture']['h']),1,2)
        grid_layout.addWidget(self.h,1,3)
        
        # 创建一个QWidget容器来放置Grid布局
        grid_widget = QWidget()
        grid_widget.setLayout(grid_layout)
        
        settings_layout.addRow(grid_widget)
        
        # 创建一个水平布局
        button_layout = QHBoxLayout()
        button_layout.addWidget(self.show_roi_button)
        button_layout.addWidget(self.set_resolution_button)
        button_layout.addWidget(self.set_original_resolution_button)

        # 将水平布局添加到 QFormLayout
        settings_layout.addRow(button_layout)

        # 连接信号和槽
        self.set_resolution_button.clicked.connect(self.on_set_resolution_clicked)
        self.set_original_resolution_button.clicked.connect(self.on_set_original_resolution_clicked)
        self.show_roi_button.clicked.connect(self.show_roi_component)

        # 初始化拖动框
        self.shapes_layer = None

        self.settings_box.setLayout(settings_layout)

        # 主布局
        self.scroll_layout.addWidget(self.settings_box)
        
    def init_capture_control_ui(self):
        self.control_box = QGroupBox(translations[self.language]['qhyccd_capture']['capture_control'])
        control_layout = QFormLayout()
        
        exposure_layout = QHBoxLayout()
        self.exposure_time = QDoubleSpinBox()  # 修改为 QDoubleSpinBox
        self.exposure_time.setSuffix(' ms')  # 设置单位为毫秒
        self.exposure_time.setDecimals(3)  # 保留三位小数
        self.exposure_time.valueChanged.connect(self.update_exposure_time)
        exposure_layout.addWidget(self.exposure_time)
        
        self.auto_exposure_dialog = None
        self.auto_exposure_button = QPushButton(translations[self.language]['qhyccd_capture']['auto_exposure'])
        self.auto_exposure_button.clicked.connect(self.toggle_auto_exposure)
        
        # self.auto_exposure_button.setEnabled(False)
        exposure_layout.addWidget(self.auto_exposure_button)
        
        control_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['exposure_time']), exposure_layout)
        
        # 增益设置
        self.gain = QDoubleSpinBox()
        # self.gain.setSuffix(' dB')
        self.gain.valueChanged.connect(self.update_gain)
        control_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['gain']), self.gain)

        # 偏移量设置
        self.offset = QDoubleSpinBox()
        # self.offset.setSuffix(' units')
        self.offset.valueChanged.connect(self.update_offset)
        control_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['offset']), self.offset)

        # USB 传输设置
        self.usb_traffic = QSpinBox()
        # self.usb_traffic.setSuffix(' MB/s')
        self.usb_traffic.setRange(1, 500)  # 设置 USB 输范围
        self.usb_traffic.valueChanged.connect(self.update_usb_traffic)
        control_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['usb_traffic']), self.usb_traffic)
        
        # 添加图像显示方式选择框
        self.display_mode_selector = QComboBox()
        self.display_mode_selector.addItems([translations[self.language]['qhyccd_capture']['distributed_display'], translations[self.language]['qhyccd_capture']['single_display'], translations[self.language]['qhyccd_capture']['sequential_display']])
        
        control_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['display_mode']), self.display_mode_selector)

        # 添加 Bayer 类型转换组件
        self.bayer_conversion_selector = QComboBox()
        self.bayer_conversion_selector.addItems(["None", "RGGB", "BGGR", "GRBG", "GBRG"])
        self.bayer_conversion_selector.currentIndexChanged.connect(self.on_bayer_conversion_changed)
        self.bayer_conversion = "None"
        self.bayer_name = QLabel(translations[self.language]['qhyccd_capture']['bayer_conversion'])
        # 将 Bayer 类型转换组件添加到布局中
        control_layout.addRow(self.bayer_name, self.bayer_conversion_selector)
        
        grid_layout = QGridLayout()
        self.start_button = QPushButton(translations[self.language]['qhyccd_capture']['start_capture'])
        self.planned_shooting_button = QPushButton(translations[self.language]['qhyccd_capture']['planned_shooting'])
        
        self.save_button = QPushButton(translations[self.language]['qhyccd_capture']['save'])
        self.save_button.setToolTip(translations[self.language]['qhyccd_capture']['save_tooltip'])
        self.save_button.setVisible(False) # 默认隐藏保存按钮 -------------------------------------

        self.start_button.clicked.connect(self.start_capture)
        self.planned_shooting_button.clicked.connect(self.show_planned_shooting_dialog)
        self.save_button.clicked.connect(self.save_image)
        grid_layout.addWidget(self.start_button,0,0)
        grid_layout.addWidget(self.planned_shooting_button,0,1)
        grid_layout.addWidget(self.save_button,0,2)
        control_layout.addRow(grid_layout)
        
        self.capture_in_progress = False
        self.capture_status_label = QLabel('')
        control_layout.addRow(self.capture_status_label)
        
        self.control_box.setLayout(control_layout)
        self.scroll_layout.addWidget(self.control_box)

    def init_video_control_ui(self):
        self.preview_state = False
        
        # 录像区域
        self.video_control_box = QGroupBox(translations[self.language]['qhyccd_capture']['recording'])
        video_layout = QFormLayout()
        
        grid_layout = QGridLayout()
        # 预览模式控件
        self.preview_status = False
        self.preview_checkbox = QCheckBox(translations[self.language]['qhyccd_capture']['preview_mode'])
        self.preview_checkbox.setToolTip(translations[self.language]['qhyccd_capture']['preview_mode_tooltip'])
        self.preview_checkbox.stateChanged.connect(self.toggle_preview_mode)
        grid_layout.addWidget(self.preview_checkbox,0,0)
        # 是否顶置的控件
        # self.top_checkbox_status = False
        self.top_checkbox = QCheckBox(translations[self.language]['qhyccd_capture']['top_checkbox'])
        self.top_checkbox.setToolTip(translations[self.language]['qhyccd_capture']['top_checkbox_tooltip'])
        # self.top_checkbox.stateChanged.connect(self.toggle_top_checkbox)
        grid_layout.addWidget(self.top_checkbox,0,1)
        # 创建水平布局，将两个复选框放在左边
        checkbox_layout = QHBoxLayout()
        checkbox_layout.addWidget(self.preview_checkbox)
        checkbox_layout.addWidget(self.top_checkbox)

        # 保存进度显示
        self.save_progress_indicator = QLabel("")
        # self.save_progress_indicator.setVisible(False)  # 初始隐藏
        grid_layout.addWidget(self.save_progress_indicator,0,3)
        video_layout.addRow(grid_layout)  # 将转圈指示器添加到布局中

        # 默认路径和文件名
        self.default_path = os.getcwd()  # 当前工作目录
        self.default_filename = f"qhyccd_now-time"
        self.save_thread = None
        
        # 保存方式选择框
        self.save_mode = translations[self.language]['qhyccd_capture']['single_frame_storage']
        self.save_mode_selector = QComboBox()
        self.save_mode_selector.addItems([translations[self.language]['qhyccd_capture']['single_frame_storage'], translations[self.language]['qhyccd_capture']['video_storage']])
        self.save_mode_selector.currentIndexChanged.connect(self.on_save_mode_changed)
        # 添加到布局中1chin
        video_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['save_mode']), self.save_mode_selector)
        
        # 路径选择控件
        self.path_selector = QLineEdit()
        self.path_selector.setText(self.default_path)
        self.path_button = QPushButton(translations[self.language]['qhyccd_capture']['select_path'])
        self.path_button.clicked.connect(self.select_path)
        path_layout = QHBoxLayout()
        path_layout.addWidget(self.path_selector)
        path_layout.addWidget(self.path_button)
        video_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['record_path']), path_layout)
        
        # 录像文件名选择控件
        self.record_file_name = QLineEdit()
        self.record_file_name.setPlaceholderText(translations[self.language]['qhyccd_capture']['record_file_name'])
        self.record_file_name.setText(self.default_filename)
        
        # 添加保存格式选择控件
        self.fits_header = None
        self.save_format_selector = QComboBox()
        if self.save_mode == translations[self.language]['qhyccd_capture']['single_frame_storage']:
            self.save_format_selector.addItems(['png', 'jpeg', 'tiff', 'fits'])  # 图片格式
        elif self.save_mode == translations[self.language]['qhyccd_capture']['video_storage']:
            self.save_format_selector.addItems(['avi', 'mp4', 'mkv'])  # 视频格式
        self.save_format_selector.currentIndexChanged.connect(self.on_save_format_changed)
 
        self.jpeg_quality = QDoubleSpinBox()
        self.jpeg_quality.setRange(0, 100)
        self.jpeg_quality.setValue(100)
        self.jpeg_quality.setDecimals(0) 
        self.jpeg_quality.setSuffix('%')
        self.jpeg_quality.setToolTip(translations[self.language]['qhyccd_capture']['quality_tooltip'])
        self.jpeg_quality.setVisible(False)

        self.tiff_compression = QComboBox()
        self.tiff_compression.setVisible(False)
        
        self.show_fits_header = QPushButton(translations[self.language]['qhyccd_capture']['fits_header'])
        self.show_fits_header.setToolTip(translations[self.language]['qhyccd_capture']['fits_header_tooltip'])
        self.show_fits_header.setVisible(False)
        self.show_fits_header.clicked.connect(self.toggle_fits_header)

        self.fits_header_dialog = FitsHeaderEditor(self.viewer,self.language)
        name_layout = QHBoxLayout()
        name_layout.addWidget(self.record_file_name)
        name_layout.addWidget(self.save_format_selector)
        name_layout.addWidget(self.jpeg_quality) 
        name_layout.addWidget(self.tiff_compression)
        name_layout.addWidget(self.show_fits_header)
        video_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['record_file_name']), name_layout)   
        
        # 录像模式选择控件
        self.record_mode = translations[self.language]['qhyccd_capture']['continuous_mode']
        self.record_mode_ids = {translations[self.language]['qhyccd_capture']['continuous_mode']:0,translations[self.language]['qhyccd_capture']['time_mode']:1,translations[self.language]['qhyccd_capture']['frame_mode']:2}
        self.record_mode_selector = QComboBox()
        self.record_mode_selector.addItems(list(self.record_mode_ids.keys()))
        self.record_mode_selector.currentIndexChanged.connect(self.on_record_mode_changed)
        video_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['record_mode']), self.record_mode_selector)
        
        # 显示时间输入框
        self.start_save_time = None
        self.record_time_input = QSpinBox()
        self.record_time_input.setSuffix(translations[self.language]['qhyccd_capture']['seconds'])
        self.record_time_input.setRange(1, 3600)  # 设置范围为1秒到3600秒
        self.record_time_input_label = QLabel(translations[self.language]['qhyccd_capture']['record_time'])
        video_layout.addRow(self.record_time_input_label, self.record_time_input)
        self.record_time_input.setVisible(False)
        self.record_time_input_label.setVisible(False)
        
        # 显示帧数输入框
        self.record_frame_count = 0
        self.frame_count_input = QSpinBox()
        self.frame_count_input.setRange(1, 10000)  # 设置范围为1到10000帧
        self.frame_count_input_label = QLabel(translations[self.language]['qhyccd_capture']['record_frames'])
        video_layout.addRow(self.frame_count_input_label, self.frame_count_input)
        self.frame_count_input.setVisible(False)
        self.frame_count_input_label.setVisible(False)

        grid_layout = QGridLayout()
        # 开启录像按钮
        self.record_button = QPushButton(translations[self.language]['qhyccd_capture']['start_record'])
        grid_layout.addWidget(self.record_button,0,0)
        self.record_button.clicked.connect(self.start_recording)
        
        # 停止录像按钮
        self.stop_record_button = QPushButton(translations[self.language]['qhyccd_capture']['stop_record'])
        self.stop_record_button.clicked.connect(self.stop_recording)
        grid_layout.addWidget(self.stop_record_button,0,1)
        video_layout.addRow(grid_layout)
        
        # 添加进度条
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)  # 设置进度条范围
        self.progress_bar.setValue(0)  # 初始值为0
        # 将进度条添加到布局中
        video_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['record_progress']), self.progress_bar)
        
        self.video_control_box.setLayout(video_layout)
        self.scroll_layout.addWidget(self.video_control_box)

    def init_image_control_ui(self):
        # 图像控制区域
        self.image_control_box = QGroupBox(translations[self.language]['qhyccd_capture']['image_processing'])
        image_control_layout = QVBoxLayout()
        
        # 添加直方图控制
        histogram_group = QGroupBox(translations[self.language]['qhyccd_capture']['histogram'])
        histogram_layout = QFormLayout()
        
        # 添加是否显示直方图的复选框
        self.show_histogram_checkbox = QCheckBox(translations[self.language]['qhyccd_capture']['show_histogram'])
        self.show_histogram_checkbox.stateChanged.connect(self.toggle_histogram_display)
        histogram_layout.addRow(self.show_histogram_checkbox)
        
        # 添加绘图区域
        self.img_buffer = queue.Queue()
        self.histogram_widget = HistogramWidget(self.viewer,self.img_buffer,self.language)

        self.last_time = 0  # 初始化上次更新时间
        self.update_interval = 1   # 每秒 1 次更新的时间间隔
        self.preview_contrast_limits_connection = None
        self.contrast_limits_connection = None
        
        histogram_group.setLayout(histogram_layout)
        
        # 添加白平衡控制
        self.wb_group = QGroupBox(translations[self.language]['qhyccd_capture']['white_balance_control'])
        wb_layout = QFormLayout()
        self.wb_red = QSlider(Qt.Orientation.Horizontal)
        self.wb_green = QSlider(Qt.Orientation.Horizontal)
        self.wb_blue = QSlider(Qt.Orientation.Horizontal)
        
        wb_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['red']), self.wb_red)
        wb_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['green']), self.wb_green)
        wb_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['blue']), self.wb_blue)
        
        self.auto_white_balance_dialog = None
        self.auto_white_balance_button = QPushButton(translations[self.language]['qhyccd_capture']['auto_white_balance'])
        self.auto_white_balance_button.clicked.connect(self.toggle_auto_white_balance)
        
        wb_layout.addRow(self.auto_white_balance_button)
        self.wb_group.setLayout(wb_layout)
        
        # 将新的布局添加到主布局中
        image_control_layout.addWidget(histogram_group)
        image_control_layout.addWidget(self.wb_group)
        
        
        self.star_group = QGroupBox(translations[self.language]['qhyccd_capture']['star_analysis'])
        star_layout = QFormLayout()
        
        self.star_fwhm = QDoubleSpinBox()
        self.star_fwhm.setRange(1, 100)
        self.star_fwhm.setSingleStep(1)
        self.star_fwhm.setValue(3)
        self.star_fwhm.setToolTip(translations[self.language]['qhyccd_capture']['star_fwhm_tooltip'])
        star_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['star_fwhm']), self.star_fwhm)
        
        self.star_analysis_choise = ['photutils']
        if self.system_name == 'posix':
            # 检查solve-field命令是否存在
            try:
                subprocess.run(['solve-field', '--version'], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.star_analysis_choise.append('Astrometry')
            except subprocess.CalledProcessError as e:
                self.star_analysis_choise.append('Astrometry')

        # 星点解析方法选择
        self.star_analysis_method_selector = QComboBox()
        self.star_analysis_method_selector.addItems(self.star_analysis_choise)
        star_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['star_analysis_method']), self.star_analysis_method_selector)
        
        # 星点解析和保存表格按钮的水平布局
        star_button_layout = QHBoxLayout()
        
        # 星点解析按钮
        self.star_analysis_button = QPushButton(translations[self.language]['qhyccd_capture']['star_analysis'])
        self.star_analysis_button.clicked.connect(self.star_analysis)
        star_button_layout.addWidget(self.star_analysis_button)
        
        # 保存表格按钮
        self.save_star_table_button = QPushButton(translations[self.language]['qhyccd_capture']['save_star_table'])
        self.save_star_table_button.clicked.connect(self.save_star_table)
        star_button_layout.addWidget(self.save_star_table_button)
        
        # 将按钮布局添加到星点解析布局中
        star_layout.addRow(star_button_layout)
        
        # 添加循环进度条
        self.star_progress_bar = QProgressBar()
        self.star_progress_bar.setRange(0, 100)  # 设置为循环模式
        star_layout.addRow(self.star_progress_bar)
        
        self.star_group.setLayout(star_layout)
        image_control_layout.addWidget(self.star_group)
        
        self.image_control_box.setLayout(image_control_layout)
        self.scroll_layout.addWidget(self.image_control_box)

    def init_temperature_control_ui(self):
        '''温度控制布局'''
        # 温度控制
        self.temperature_control_box = QGroupBox(translations[self.language]['qhyccd_capture']['temperature_control'])
        temperature_layout = QFormLayout()
        
        self.temperature_setpoint = QDoubleSpinBox()
        self.temperature_setpoint.setSuffix(' °C')
        self.temperature_setpoint.valueChanged.connect(self.update_temperature_setpoint)
        temperature_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['set_temperature']), self.temperature_setpoint)
        
        grid_layout = QGridLayout()
        self.current_temperature_label = QLabel(translations[self.language]['qhyccd_capture']['temperature'])
        self.current_humidity_label = QLabel(translations[self.language]['qhyccd_capture']['humidity'])
        grid_layout.addWidget(self.current_temperature_label,0,0)
        grid_layout.addWidget(self.current_humidity_label,0,1)
        temperature_layout.addRow(grid_layout)
        
        self.temperature_control_box.setLayout(temperature_layout)
        self.scroll_layout.addWidget(self.temperature_control_box)
        
    def init_CFW_control_ui(self):
        '''滤镜轮控制布局'''
        # 滤镜轮控制区域
        self.CFW_control_box = QGroupBox(translations[self.language]['qhyccd_capture']['CFW_control'])
        CFW_layout = QFormLayout()
        
        self.CFW_id = None # 当前选中滤镜轮ID
        
        self.CFW_number_ids = {}
        self.CFW_filter_selector = QComboBox()
        self.CFW_filter_selector.addItems(list(self.CFW_number_ids.keys()))  # 示例项
        self.CFW_filter_selector.currentIndexChanged.connect(self.on_CFW_filter_changed)
        
        CFW_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['CFW_position']), self.CFW_filter_selector)
        self.CFW_control_box.setLayout(CFW_layout)
        self.scroll_layout.addWidget(self.CFW_control_box)
    
    def init_external_trigger_ui(self):
        '''外部触发布局'''
        self.external_trigger_box = QGroupBox(translations[self.language]['qhyccd_capture']['external_trigger'])
        external_trigger_layout = QFormLayout()

        # 启用外部触发的复选框
        self.enable_external_trigger_checkbox = QCheckBox(translations[self.language]['qhyccd_capture']['enable_external_trigger'])
        self.enable_external_trigger_checkbox.stateChanged.connect(self.toggle_external_trigger_enabled)
        external_trigger_layout.addRow(self.enable_external_trigger_checkbox)

        # 触发接口选择框
        self.trigger_interface_choise = {}
        self.trigger_interface_selector = QComboBox()
        external_trigger_layout.addRow(QLabel(translations[self.language]['qhyccd_capture']['trigger_interface']), self.trigger_interface_selector)

        # 使用触发输出的复选框
        self.use_trigger_output_checkbox = QCheckBox(translations[self.language]['qhyccd_capture']['use_trigger_output'])
        external_trigger_layout.addRow(self.use_trigger_output_checkbox)

        self.external_trigger_box.setLayout(external_trigger_layout)
        self.scroll_layout.addWidget(self.external_trigger_box)
         
    def init_GPS_ui(self):
        '''GPS布局'''
        self.GPS_control_box = QGroupBox(translations[self.language]['qhyccd_capture']['GPS_control'])
        GPS_layout = QFormLayout()
        
        self.GPS_selector = QCheckBox(translations[self.language]['qhyccd_capture']['GPS_start'])
        self.GPS_selector.stateChanged.connect(self.toggle_GPS_control)
        GPS_layout.addRow(self.GPS_selector)
        
        self.GPS_data_label = QLabel()
        GPS_layout.addRow(self.GPS_data_label)
        
        self.GPS_control_box.setLayout(GPS_layout)
        self.scroll_layout.addWidget(self.GPS_control_box)
         
    def init_ui_state(self):
        '''初始化UI状态'''
        # 创建一个垂直方向的 spacer
        # 创建一个垂直方向的 spacer
        spacer = QSpacerItem(20, 2, QSizePolicy.Minimum, QSizePolicy.Expanding)

        # 将 spacer 添加到布局的底部
        self.scroll_layout.addItem(spacer)

        '''主布局'''
        self.setLayout(QVBoxLayout())
        self.layout().addWidget(self.scroll_area)   # type: ignore
        
        '''初始化所有区域为隐藏状态'''
        self.settings_box.setVisible(False)
        self.control_box.setVisible(False)
        self.image_control_box.setVisible(False)
        self.temperature_control_box.setVisible(False)
        self.CFW_control_box.setVisible(False)
        self.video_control_box.setVisible(False)
        self.external_trigger_box.setVisible(False)
        self.GPS_control_box.setVisible(False)
        
        # 禁用复选框
        self.show_settings_checkbox.setEnabled(False)
        self.show_control_checkbox.setEnabled(False)
        self.show_image_control_checkbox.setEnabled(False)
        self.show_temperature_control_checkbox.setEnabled(False)
        self.show_CFW_control_checkbox.setEnabled(False)
        self.show_video_control_checkbox.setEnabled(False)    
        self.show_external_trigger_checkbox.setEnabled(False)   
        self.show_GPS_control_checkbox.setEnabled(False)
    
    def append_text(self, text, is_error=False):
        try:
            self.state_label.moveCursor(QTextCursor.End)
            # 创建一个 QTextCursor 对象
            cursor = self.state_label.textCursor()
            # 向 QTextEdit 添加文本
            now_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # 设置文本颜色
            if is_error:
                cursor.insertHtml(f'<br><span style="color:red;">{text}</span>')  # 红色文本，使用 <br> 作为 HTML 中的换行
                print(f"\033[91m{now_time}: {text}\033[0m")  # 控制台输出红色文本
            else:
                cursor.insertHtml(f"<br>{text}")  # 默认颜色文本，使用 <br> 作为 HTML 中的换行
                print(f"{now_time}: {text}")  # 控制台输出默认颜色文本

            # 自动滚动到底部
            self.state_label.moveCursor(QTextCursor.End)
            self.state_label.moveCursor(QTextCursor.StartOfLine)  # 滚动到最左边
        except Exception as e:
            self.append_text(f"{translations[self.language]['debug']['append_text_failed']}: {e}", is_error=True)

    def show_settings_dialog(self):
        try:
            self.settings_dialog.exec_()
        except Exception as e:
            self.append_text(f"{translations[self.language]['debug']['show_settings_dialog_failed']}: {e}")
        
    def load_settings(self):
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r') as f:
                    settings = json.load(f)
                    self.qhyccd_path = settings.get("qhyccd_path", "")
                    self.language = settings.get("language", "en")
            else:
                self.qhyccd_path = ""
                self.language = "en"  # 默认语言
        except Exception as e:
            self.append_text(f"{translations[self.language]['debug']['load_settings_failed']}: {e}")
            
    def update_memory_progress(self, used_memory):
        if used_memory > 0:
            memory_usage_percentage = int(used_memory)
            self.memory_progress_bar.setValue(memory_usage_percentage)  # 更新进度条值
            
            # 根据内存占用设置进度条颜色
            if memory_usage_percentage < 60:
                self.memory_progress_bar.setStyleSheet("QProgressBar::chunk { background-color: green; }")  # 低于60%为绿色
            elif memory_usage_percentage < 80:
                # 渐变为黄色
                yellow_value = int((memory_usage_percentage - 60) * 255 / 20)  # 计算黄色值
                self.memory_progress_bar.setStyleSheet(f"QProgressBar::chunk {{ background-color: rgb({yellow_value}, 255, 0); }}")
            else:
                # 渐变为红色
                red_value = int((memory_usage_percentage - 80) * 255 / 20)  # 计算红色值
                self.memory_progress_bar.setStyleSheet(f"QProgressBar::chunk {{ background-color: rgb(255, {255 - red_value}, 0); }}")
    
    def on_sdk_data_received(self, data):
        if data['order'] == 'init_qhyccd_resource_success':
            self.init_qhyccdResource_success(data['data'])
        elif data['order'] == 'readCameraName_success':
            self.read_camera_name_success(data['data'])
        elif data['order'] == 'getPlannedShootingData_success':
            self.update_planned_shooting_data(data['data'])
        elif data['order'] == 'openCamera_success':
            self.open_camera_success(data['data'])
        elif data['order'] == 'readoutModeName_success':
            self.get_readout_mode_success(data['data'])
        elif data['order'] == 'streamAndCaptureMode_success':
            self.get_stream_and_capture_mode_success(data['data'])
        elif data['order'] == 'initCamera_success':
            self.already_connected_signal(data['data'])
        elif data['order'] == 'closeCamera_success':
            self.already_disconnected_signal()
        elif data['order'] == 'getIsColorCamera_success':
            self.update_camera_color_success(data['data'])
        elif data['order'] == 'getLimitSelector_success':
            self.update_limit_selector_success(data['data'])
        elif data['order'] == 'getEffectiveArea_success':
            self.get_effective_area_success(data['data'])
        elif data['order'] == 'getCameraConfig_success':
            self.update_camera_config_success(data['data'])
        elif data['order'] == 'getCameraPixelBin_success':
            self.update_camera_pixel_bin_success(data['data'])
        elif data['order'] == 'setCameraPixelBin_success':
            self.on_set_pixel_bin_success()
        elif data['order'] == 'getCameraDepth_success':
            self.update_depth_selector_success(data['data'])
        elif data['order'] == 'setDebayerMode_success':
            self.append_text(data['data'])
        elif data['order'] == 'getIsTemperatureControl_success':
            self.update_camera_temperature_success(data['data'])
        elif data['order'] == 'getCFWInfo_success':
            self.update_CFW_control_success(data['data'])
        elif data['order'] == 'getAutoExposureIsAvailable_success':
            self.update_auto_exposure_success(data['data'])
        elif data['order'] == 'getAutoExposureLimits_success':
            self.auto_exposure_dialog.update_limits_success(data['data'])   # type: ignore
        elif data['order'] == 'setAutoExposure_success':
            self.auto_exposure_dialog.apply_changes_success(data['data'])   # type: ignore
        elif data['order'] == 'getExposureValue_success':
            self.on_auto_exposure_value_changed(data['data'])   # type: ignore
        elif data['order'] == 'getAutoWhiteBalanceIsAvailable_success':
            self.update_auto_white_balance_success(data['data'])
        elif data['order'] == 'setAutoWhiteBalance_success':
            self.auto_white_balance_dialog.start_auto_white_balance_success(data)   # type: ignore
        elif data['order'] == 'getAutoWhiteBalanceValues_success':
            self.on_auto_white_balance_complete(data['data'])   # type: ignore
        elif data['order'] == 'setExposureTime_success':
            self.update_exposure_time_success(data['data'])
        elif data['order'] == 'setUsbTraffic_success':
            self.update_usb_traffic_success(data['data'])
        elif data['order'] == 'error':
            self.append_text(data['data'], is_error=True)
        elif data['order'] == 'start_preview_success':
            self.start_preview_success(data['data'])
        elif data['order'] == 'preview_frame':
            self.data_received(data['data'])
        elif data['order'] == 'tip':
            self.append_text(data['data'])
        elif data['order'] == 'singleCapture_success':
            self.on_capture_finished(data['data'])
        elif data['order'] == 'setDepth_success':
            self.on_set_depth_success(data['data'])
        elif data['order'] == 'stop_preview_success':
            self.stop_preview_success()
        elif data['order'] == 'setResolution_success':
            self.on_set_resolution_success()
        elif data['order'] == 'getImageBufferSize_success':
            self.update_image_buffer_size_success(data['data'])
        elif data['order'] == 'getTemperature_success':
            self.get_temperature_success(data['data'])
        elif data['order'] == 'stop_success':
            self.stop_qhyccd_process_success()
        elif data['order'] == 'runPlan_success':
            self.on_plan_success(data['data'])
        elif data['order'] == 'setCFWFilter_success':
            self.on_set_CFW_filter_success(data['data'])
        elif data['order'] == 'burst_mode_frame':
            self.on_burst_mode_frame(data['data'])
        elif data['order'] == 'stopExternalTrigger_success':
            self.stop_external_trigger_success(data['data'])    
        elif data['order'] == 'setGPSControl_success':
            self.on_GPS_control_success(data['data'])
        elif data['order'] == 'get_humidity_success':
            self.update_camera_humidity_text(data['data'])
        elif data['order'] == 'record_end':
            self.stop_recording_success(data['data'])
        elif data['order'] == 'save_end':
            self.on_save_thread_finished()
        elif data['order'] == 'progress_bar_value':
            self.progress_bar.setValue(data['data'])
           
    def init_qhyccdResource(self,file_path=None):
        if self.sdk_input_queue is None:
            return
        self.sdk_input_queue.put({'order':'init_qhyccd_resource', 'data':file_path})
        
    def init_qhyccdResource_success(self,qhyccddll):
        if self.sdk_input_queue is None:
            return
        self.settings_dialog.qhyccd_path_label.setText(qhyccddll)
        self.disconnect_button.setEnabled(False)
        self.connect_button.setEnabled(True)
        self.reset_camera_button.setEnabled(True)
        self.init_state = True
        self.sdk_input_queue.put({'order':'read_camera_name', 'data':''})
    
    def update_image_buffer_size_success(self, image_buffer_size):
        if self.sdk_input_queue is None:
            return
        try:
            if self.shm1 is not None:
                # 确保释放所有对shm1的引用
                if hasattr(self, 'shm1_view'):
                    self.shm1_view.release()
                    del self.shm1_view  # 显式删除引用
                self.shm1.close()
                self.shm1.unlink()
            if self.shm2 is not None:
                # 确保释放所有对shm2的引用
                if hasattr(self, 'shm2_view'):
                    self.shm2_view.release()
                    del self.shm2_view  # 显式删除引用
                self.shm2.close()
                self.shm2.unlink()
        except BufferError as e:
            pass
        finally:
            # 创建新的共享内存
            self.shm1 = shared_memory.SharedMemory(create=True, size=image_buffer_size)
            self.shm2 = shared_memory.SharedMemory(create=True, size=image_buffer_size)
            self.sdk_input_queue.put({'order': 'set_image_buffer', 'data': {'shm1': self.shm1.name, 'shm2': self.shm2.name}})
            self.reset_camera_button.setEnabled(True)
        
    def get_readout_mode_success(self,readout_mode_name_dict):
        self.readout_mode_name_dict = readout_mode_name_dict
        self.readout_mode_selector.clear()
        self.readout_mode_selector.addItems(list(self.readout_mode_name_dict.keys()))
        
    def get_stream_and_capture_mode_success(self,stream_and_capture_mode_dict):
        self.camera_mode_ids = stream_and_capture_mode_dict
        self.camera_mode_selector.clear()
        self.camera_mode_selector.addItems(list(self.camera_mode_ids.keys()))

    def read_camera_name(self):
        if self.sdk_input_queue is None:
            return
        self.reset_camera_button.setEnabled(False)
        if not self.init_state:
            self.sdk_input_queue.put({'order':'init_qhyccd_resource', 'data':self.settings_dialog.qhyccd_path_label.text()})
        self.sdk_input_queue.put({'order':'read_camera_name', 'data':''})
    
    def read_camera_name_success(self,camera_ids):
        if self.sdk_input_queue is None:
            return
        self.camera_ids = camera_ids
        self.connect_button.setEnabled(True)
        if self.camera_ids == []:
            self.camera_selector.clear()
            self.reset_camera_button.setEnabled(True)
            return
        else:
            self.camera_selector.clear()
            self.camera_selector.addItems(self.camera_ids)
            self.sdk_input_queue.put({'order':'open_camera', 'data':self.camera_selector.currentText()})
            self.sdk_input_queue.put({'order':'get_image_buffer_size', 'data':''})
            
    def connect_camera(self):
        if self.sdk_input_queue is None:
            return
        if self.qhyccd_process is None:
            self.init_sdk()
        if not self.camera_state:
            self.reset_camera_button.setEnabled(False)
            self.connect_button.setEnabled(False)
            self.disconnect_button.setEnabled(False)
            self.camera_selector.setEnabled(False)
            self.camera_mode_selector.setEnabled(False)
            self.readout_mode_selector.setEnabled(False)  
            self.camera_name = self.camera_selector.currentText()
            self.camera_mode = self.camera_mode_selector.currentText()
            self.sdk_input_queue.put({'order':'init_camera', 'data':[self.camera_name, self.readout_mode_selector.currentText(), self.camera_mode_selector.currentText()]})

    def already_connected_signal(self,data):
        try:
            self.camera_W = data['readout_w']
            self.camera_H = data['readout_h']
            self.update_camera_color_success(data['is_color'])
            self.update_camera_config_success(data['config'])
            self.update_limit_selector_success(data['limit'])
            self.update_camera_pixel_bin_success(data['pixel_bin'])
            self.update_debayer_mode(data['debayer'])
            self.get_effective_area_success(data['effective_area'])
            self.update_resolution(0,0,self.image_w,self.image_h)        
            self.update_depth_selector_success(data['depth'])
            self.update_camera_mode()
            self.update_camera_temperature_success(data['temperature'])
            self.update_camera_humidity_success(data['humidity'])
            self.update_CFW_control_success(data['CFW'])
            self.update_auto_exposure_success(data['auto_exposure'])
            self.update_auto_white_balance_success(data['auto_white_balance'])
            self.update_external_trigger_success(data['external_trigger'])
            self.update_tiff_compression()
            self.update_burst_mode_success(data['burst_mode'])
            self.update_GPS_control_success(data['GPS_control'])
            # 启用复选框
            self.show_settings_checkbox.setEnabled(True)
            self.show_control_checkbox.setEnabled(True)
            self.show_image_control_checkbox.setEnabled(True)
            
            # 禁用自动曝光和自动白平衡
            self.auto_exposure_button.setVisible(False)
            self.auto_white_balance_button.setVisible(False)
            
            # 显示复选框
            self.toggle_settings_box(True)
            self.toggle_control_box(True)
            self.toggle_image_control_box(True)
            
            self.camera_state = True
            
            self.config_label.setText(f'{translations[self.language]["qhyccd_capture"]["connected"]}')
            self.config_label.setStyleSheet("color: green;")  # 设置字体颜色为绿色
            
            self.reset_camera_button.setEnabled(False)
            self.connect_button.setEnabled(False)
            self.disconnect_button.setEnabled(True) 
        except Exception as e:
            self.disconnect()
            self.camera_selector.setEnabled(True)
            self.camera_mode_selector.setEnabled(True)
            self.readout_mode_selector.setEnabled(True)  
            self.connect_button.setEnabled(True)
            self.reset_camera_button.setEnabled(True)
            self.disconnect_button.setEnabled(False)
            self.append_text(f"{translations[self.language]['debug']['already_connected_signal_failed']}: {e}",True)

    def disconnect_camera(self):
        """断开相机连接"""
        try:    
            self.init_camera_id = -1
            self.disconnect_button.setEnabled(False)
            self.connect_button.setEnabled(False)
            self.reset_camera_button.setEnabled(False)
            if self.capture_in_progress:
                self.cancel_capture()
            if self.preview_state :
                self.stop_preview()
                
            self.sdk_input_queue.put({'order':'close_camera', 'data':True})
        except Exception as e:
            self.disconnect_button.setEnabled(True)
            self.append_text(f"{translations[self.language]['debug']['disconnect_camera_failed']}: {e}")

    def already_disconnected_signal(self):
        self.camhandle = 0
        self.config_label.setText(f'{translations[self.language]["qhyccd_capture"]["disconnected"]}')
        self.config_label.setStyleSheet("color: red;")  # 设置字体颜色为红色
 
        self.current_image = None
        self.current_image_name = None
        try:    
            if self.is_color_camera:    
                for slider in [self.wb_red, self.wb_green, self.wb_blue]:
                    slider.valueChanged.disconnect()  # 断开之前的连接
        except Exception as e:
            warnings.warn(f"{translations[self.language]['debug']['disconnect_white_balance_failed']}: {e}")
            self.append_text(f"{translations[self.language]['debug']['disconnect_white_balance_failed']}: {e}")
        
        # 初始化所有区域为隐藏状态
        self.settings_box.setVisible(False)
        self.control_box.setVisible(False)
        self.image_control_box.setVisible(False)
        self.temperature_control_box.setVisible(False)
        self.CFW_control_box.setVisible(False)
        self.video_control_box.setVisible(False)
        self.external_trigger_box.setVisible(False)
        self.GPS_control_box.setVisible(False)
        # 取消选项
        self.toggle_settings_box(False)
        self.toggle_control_box(False)
        self.toggle_image_control_box(False)
        self.toggle_temperature_control_box(False)
        self.toggle_CFW_control_box(False)
        self.toggle_video_control_box(False)
        self.toggle_external_trigger_box(False)
        self.toggle_GPS_control_box(False)
        # 禁用复选框
        self.show_settings_checkbox.setEnabled(False)
        self.show_control_checkbox.setEnabled(False)
        self.show_image_control_checkbox.setEnabled(False)
        self.show_temperature_control_checkbox.setEnabled(False)
        self.show_CFW_control_checkbox.setEnabled(False)
        self.show_video_control_checkbox.setEnabled(False)
        self.show_external_trigger_checkbox.setEnabled(False)
        self.show_GPS_control_checkbox.setEnabled(False)
        
        self.camera_state = False
        
        self.temperature_update_timer.stop()
        self.humidity_update_timer.stop()
    
        self.planned_shooting_dialog.clearTable()
        self.settings_dialog.camera_info_label.setText(f'{translations[self.language]["qhyccd_capture"]["camera_info_disconnected"]}')
        
        self.disconnect_button.setEnabled(False)
        self.reset_camera_button.setEnabled(True)
        self.connect_button.setEnabled(True)
        self.camera_selector.setEnabled(True)
        self.camera_mode_selector.setEnabled(True)
        self.readout_mode_selector.setEnabled(True)

    def on_camera_changed(self):
        if self.sdk_input_queue is None:
            return
        self.camera_selector.setEnabled(False)
        if self.camhandle != 0:
            self.sdk_input_queue.put({'order':'close_camera', 'data':False})
        if self.camera_selector.currentText() != '' and self.readout_mode_selector.currentText() != '' and self.camera_mode_selector.currentText() != '':
            self.sdk_input_queue.put({'order':'open_camera', 'data':self.camera_selector.currentText()})

    def open_camera_success(self,data):
        self.camhandle = data['id']
        self.readout_mode_name_dict = data['readout_mode_name_dict']
        self.readout_mode_selector.clear()
        self.readout_mode_selector.addItems(list(self.readout_mode_name_dict.keys()))
        self.camera_mode_ids = data['stream_and_capture_mode_dict']
        self.camera_mode_selector.clear()
        self.camera_mode_selector.addItems(list(self.camera_mode_ids.keys()))
        self.camera_selector.setEnabled(True)
        if self.sdk_input_queue is not None:
            self.sdk_input_queue.put({'order':'get_planned_shooting_data', 'data':''})
               
    def end_capture(self):
        if self.capture_status_label.text().startswith(translations[self.language]["qhyccd_capture"]["capturing"]):
            self.capture_status_label.setText(translations[self.language]["qhyccd_capture"]["capture_complete"])
               
    def update_camera_color(self):
        if self.sdk_input_queue is not None:
            self.sdk_input_queue.put({'order':'get_is_color_camera', 'data':''})
        
    def update_camera_color_success(self, is_color_camera):
        self.is_color_camera = is_color_camera
        if not self.is_color_camera:
            self.bayer_conversion = "None"
            self.bayer_conversion_selector.setCurrentText("None")
            # 隐藏控件并移除占用空间
            self.bayer_conversion_selector.setEnabled(False)
            self.bayer_conversion_selector.setVisible(False)

            self.wb_group.setVisible(False)
            self.bayer_name.setVisible(False)
             
            self.Debayer_mode_selector.setEnabled(False)
            self.Debayer_mode_selector.setVisible(False)
            self.Debayer_mode_label.setVisible(False)

        else:
            self.wb_group.setVisible(True)
            self.wb_group.setEnabled(True)
            self.bayer_conversion_selector.setVisible(True)
            self.bayer_conversion_selector.setEnabled(True)
            
            self.Debayer_mode_selector.setVisible(True)
            self.Debayer_mode_selector.setEnabled(True)
            self.Debayer_mode_label.setVisible(True)
            
            self.bayer_name.setVisible(True)
            if self.camera_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"]:  
                for slider in [self.wb_red, self.wb_green, self.wb_blue]:
                    slider.valueChanged.connect(self.apply_white_balance_hardware)
            else:
                for slider in [self.wb_red, self.wb_green, self.wb_blue]:
                    slider.valueChanged.connect(lambda: self.on_set_white_balance_clicked())
            
        # self.append_text(f'{translations[self.language]["qhyccd_capture"]["update_camera_color"]}:{self.is_color_camera}')
            
    def update_limit_selector(self):
        self.sdk_input_queue.put({'order':'get_limit_selector', 'data':''})
    
    def update_limit_selector_success(self,limit_dict):
        # 设置曝光限制
        min_data, max_data, step ,exposure = limit_dict["exposure"]
        self.exposure_time.blockSignals(True)
        self.exposure_time.setRange(min_data/1000, max_data/1000)  # 使用 QDoubleSpinBox 设置范围
        self.exposure_time.setValue(exposure/1000)  
        self.exposure_time.setSingleStep(1/1000)
        self.exposure_time.blockSignals(False)
        
        # 设置增益
        min_data, max_data, step ,gain = limit_dict["gain"]
        self.gain.blockSignals(True)
        self.gain.setRange(int(min_data), int(max_data))
        self.gain.setSingleStep(float(step))
        self.gain.setValue(int(gain))
        self.gain.blockSignals(False)
        
        # 设置偏移
        min_data, max_data, step ,offset = limit_dict["offset"]
        self.offset.blockSignals(True)
        self.offset.setRange(int(min_data), int(max_data))
        self.offset.setSingleStep(float(step))
        self.offset.setValue(int(offset))
        self.offset.blockSignals(False)
        
        # 设置USB宽带
        min_data, max_data, step ,usb_traffic = limit_dict["usb_traffic"]
        self.usb_traffic.blockSignals(True)
        self.usb_traffic.setRange(int(min_data), int(max_data))
        self.usb_traffic.setSingleStep(int(step))
        self.usb_traffic.setValue(int(usb_traffic))
        self.usb_traffic.blockSignals(False)
        
        # 设置白平衡限制
        if not self.is_color_camera:
            self.wb_red.setVisible(False)
            self.wb_red.setEnabled(False)
        else:
            self.wb_red.setVisible(True)
            self.wb_red.setEnabled(True)
            
            min_data, max_data, step ,wb_red = limit_dict["wb_red"]
            if self.camera_mode == translations[self.language]["qhyccd_capture"]["single_frame_mode"]:
                self.wb_red.blockSignals(True)
                self.wb_red.setRange(int(-100), int(100))
                self.wb_red.setSingleStep(int(1))
                self.wb_red.setValue(int(0))
                self.wb_red.blockSignals(False)
            else:   
                self.wb_red.blockSignals(True)
                self.wb_red.setRange(int(min_data), int(max_data))
                self.wb_red.setSingleStep(int(step))
                self.wb_red.setValue(int(wb_red))
                self.wb_red.blockSignals(False)
        
        if not self.is_color_camera:
            self.wb_green.setVisible(False)
            self.wb_green.setEnabled(False)
        else:
            self.wb_green.setVisible(True)
            self.wb_green.setEnabled(True)
            min_data, max_data, step ,wb_green = limit_dict["wb_green"]
            if self.camera_mode == translations[self.language]["qhyccd_capture"]["single_frame_mode"]:
                self.wb_green.blockSignals(True)
                self.wb_green.setRange(int(-100), int(100))
                self.wb_green.setSingleStep(int(1))
                self.wb_green.setValue(int(0))
                self.wb_green.blockSignals(False)
            else:
                self.wb_green.blockSignals(True)
                self.wb_green.setRange(int(min_data), int(max_data))
                self.wb_green.setSingleStep(int(step))
                self.wb_green.setValue(int(wb_green))
                self.wb_green.blockSignals(False)
        
        if not self.is_color_camera:
            self.wb_blue.setVisible(False)
            self.wb_blue.setEnabled(False)
        else:
            self.wb_blue.setVisible(True)
            self.wb_blue.setEnabled(True)
            min_data, max_data, step ,wb_blue = limit_dict["wb_blue"]
            if self.camera_mode == translations[self.language]["qhyccd_capture"]["single_frame_mode"]:
                self.wb_blue.blockSignals(True)
                self.wb_blue.setRange(int(-100), int(100))
                self.wb_blue.setSingleStep(int(1))
                self.wb_blue.setValue(int(0))
                self.wb_blue.blockSignals(False)
            else:
                self.wb_blue.blockSignals(True)
                self.wb_blue.setRange(int(min_data), int(max_data))
                self.wb_blue.setSingleStep(int(step))
                self.wb_blue.setValue(int(wb_blue))
                self.wb_blue.blockSignals(False)
    
    def get_effective_area(self):
        self.sdk_input_queue.put({'order':'get_effective_area', 'data':''})
        
    def get_effective_area_success(self,effective_area_dict):
        # 获取相机有效扫描范围
        sizeX = effective_area_dict["sizeX"]
        sizeY = effective_area_dict["sizeY"]
        if self.camera_H > sizeY:
            self.camera_H = sizeY
        if self.camera_W > sizeX:
            self.camera_W = sizeX
        self.image_x = 0
        self.image_y = 0
        self.image_w = self.camera_W
        self.image_h = self.camera_H
        self.x.setRange(int(self.image_x),int(self.image_x+self.image_w))
        self.y.setRange(int(self.image_y),int(self.image_y+self.image_h))
        self.w.setRange(1,int(self.image_w))
        self.h.setRange(1,int(self.image_h))
        # self.sdk_input_queue.put({'order':'update_resolution', 'data':(startX,startY,sizeX,sizeY)})

    def update_camera_config(self):
        """更新相机配置显示"""
        self.sdk_input_queue.put({'order':'get_camera_config', 'data':''})

    def update_camera_config_success(self,camera_config_dict):
        if self.camera_name is None:
            self.camera_name = self.camera_selector.currentText()
        chipW = round(camera_config_dict["chipW"], 5)
        chipH = round(camera_config_dict["chipH"], 5)
        imageW = camera_config_dict["imageW"]
        imageH = camera_config_dict["imageH"]
        pixelW = camera_config_dict["pixelW"]
        pixelH = camera_config_dict["pixelH"]
        imageB = camera_config_dict["imageB"]
        self.camera_bit = imageB
        if self.camera_W > imageW:
            self.camera_W = imageW
        if self.camera_H > imageH:
            self.camera_H = imageH
        self.image_w = self.camera_W
        self.image_h = self.camera_H
 
        # 计算最长的标签长度
        max_label_length = max(
            len(translations[self.language]["qhyccd_capture"]["camera_info_name"]),
            len(translations[self.language]["qhyccd_capture"]["camera_info_chip"]),
            len(translations[self.language]["qhyccd_capture"]["camera_info_image"]),
            len(translations[self.language]["qhyccd_capture"]["camera_info_pixel"])
        )

        # 计算最长的值字符串长度
        max_value_length = max(
            len(self.camera_name) if self.camera_name is not None else 0,
            len(f"{str(chipW)}um * {str(chipH)}um"),
            len(f"{str(imageW)}px * {str(imageH)}px {str(imageB)}bit"),
            len(f"{str(pixelW)}um * {str(pixelH)}um")
        )

        # 设置文本并居中对齐
        self.settings_dialog.camera_info_label.setText(
            f'\n'
            f'{translations[self.language]["qhyccd_capture"]["camera_info_name"].center(max_label_length)}: {self.camera_name.center(max_value_length)}\n'
            f'{translations[self.language]["qhyccd_capture"]["camera_info_chip"].center(max_label_length)}: {f"{str(chipW)}um * {str(chipH)}um".center(max_value_length)}\n'
            f'{translations[self.language]["qhyccd_capture"]["camera_info_image"].center(max_label_length)}: {f"{str(imageW)}px * {str(imageH)}px {str(imageB)}bit".center(max_value_length)}\n'
            f'{translations[self.language]["qhyccd_capture"]["camera_info_pixel"].center(max_label_length)}: {f"{str(pixelW)}um * {str(pixelH)}um".center(max_value_length)}\n'
        )

    def update_camera_pixel_bin(self):
        self.sdk_input_queue.put({'order':'get_camera_pixel_bin', 'data':''})
        
    def update_camera_pixel_bin_success(self,camera_pixel_bin_dict):
        self.camera_pixel_bin = camera_pixel_bin_dict
        self.pixel_bin_selector.clear()  # 清空现有选项
        self.pixel_bin_selector.addItems(list(self.camera_pixel_bin.keys()))  # 添加新的选项
        # self.sdk_input_queue.put({'order':'set_camera_pixel_bin', 'data':self.pixel_bin_selector.currentText()})
        self.image_w = int(self.camera_W/self.camera_pixel_bin[self.pixel_bin_selector.currentText()][0])
        self.image_h = int(self.camera_H/self.camera_pixel_bin[self.pixel_bin_selector.currentText()][1])
        # self.update_resolution(0,0,self.image_w,self.image_h)
        
    def update_depth_selector(self):
        self.sdk_input_queue.put({'order':'get_camera_depth', 'data':''})
    
    def update_depth_selector_success(self,camera_depth_options):
        self.camera_depth_options = camera_depth_options
        self.depth_selector.clear()  # 清空现有选项
        self.depth_selector.addItems(list(self.camera_depth_options.keys()))
        self.camera_bit = self.camera_depth_options[self.depth_selector.currentText()]
        
    def update_debayer_mode(self,debayer_mode):
        self.Debayer_mode = debayer_mode
        if not self.is_color_camera or self.camera_mode == translations[self.language]["qhyccd_capture"]["single_frame_mode"]:
            self.Debayer_mode_selector.setEnabled(False)
            self.Debayer_mode_selector.setVisible(False)
            self.Debayer_mode_label.setVisible(False)
            self.Debayer_mode = False
        elif self.camera_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"] and self.is_color_camera and debayer_mode:
            self.Debayer_mode_selector.setEnabled(True)
            self.Debayer_mode_selector.setVisible(True)
            self.Debayer_mode_label.setVisible(True)
            self.Debayer_mode_selector.setCurrentText(translations[self.language]["qhyccd_capture"]["debayer_mode_false"])
            self.Debayer_mode = False
    
    def update_resolution(self,x,y,w,h):
        self.sdk_input_queue.put({'order':'update_resolution', 'data':(x,y,w,h)})
        self.x.setRange(0,w-1)
        self.x.setValue(x)
        self.y.setRange(0,h-1)
        self.y.setValue(y)
        self.w.setRange(0,w)
        self.w.setValue(w)
        self.h.setRange(0,h)
        self.h.setValue(h)

    def update_camera_mode(self):
        # 判断相机是单帧模式还是连续模式
        if self.camera_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"]:
            self.video_control_box.setVisible(True)
            self.show_video_control_checkbox.setVisible(True)
            self.show_video_control_checkbox.setEnabled(True)
            self.toggle_video_control_box(True)
            self.start_preview()
            self.fps_label.setVisible(True)
            self.fps_label.setStyleSheet("color: green;")
            self.burst_mode_selector.setVisible(True)
        else:
            self.video_control_box.setVisible(False)
            self.show_video_control_checkbox.setVisible(False)
            self.show_video_control_checkbox.setEnabled(False)
            self.fps_label.setVisible(False)
            self.burst_mode_selector.setVisible(False)
        # self.append_text(f'{translations[self.language]["qhyccd_capture"]["update_camera_mode"]}：{self.camera_mode}')
    
    def update_camera_temperature(self):
        self.sdk_input_queue.put({'order':'get_is_temperature_control', 'data':''})
        
    def update_camera_temperature_success(self,has_temperature_control):
        # 判断相机是否支持温度控制
        self.has_temperature_control = has_temperature_control
        
        if self.has_temperature_control:
            self.show_temperature_control_checkbox.show()
            self.temperature_control_box.setVisible(True)
            self.toggle_temperature_control_box()
            self.show_temperature_control_checkbox.setEnabled(True)
            self.update_current_temperature()
            self.temperature_update_timer.start(5000)  # 每5秒更新一次温度
        else:
            self.temperature_control_box.setVisible(False)
            self.show_temperature_control_checkbox.hide()
            self.temperature_update_timer.stop()
            
    def update_camera_humidity(self):
        if self.sdk_input_queue is None:
            return
        self.sdk_input_queue.put({'order':'get_is_humidity_control', 'data':''})
        
    def update_camera_humidity_success(self,has_humidity_control):
        self.has_humidity_control = has_humidity_control
        if self.has_humidity_control:
            self.humidity_update_timer.start(5000)  # 每5秒更新一次湿度
            self.current_humidity_label.setVisible(True)
        else:
            self.humidity_update_timer.stop()
            self.current_humidity_label.setVisible(False)
    
    def update_CFW_control(self):
        self.sdk_input_queue.put({'order':'get_cfw_info', 'data':''})
    
    def update_CFW_control_success(self,data):
        self.is_CFW_control = data[0]
        self.CFW_number_ids = data[1]
        
        if self.is_CFW_control:
            self.show_CFW_control_checkbox.show()
            self.CFW_control_box.setVisible(True)
            self.toggle_CFW_control_box(True)
            self.show_CFW_control_checkbox.setEnabled(True)
            self.CFW_filter_selector.clear()
            self.CFW_filter_selector.addItems(list(self.CFW_number_ids.keys()))  # 示例项
        else:
            self.CFW_control_box.setVisible(False)
            self.show_CFW_control_checkbox.hide()
            self.toggle_CFW_control_box(False)
            self.show_CFW_control_checkbox.setEnabled(False)
        # self.append_text(f'{translations[self.language]["qhyccd_capture"]["update_CFW_control"]}')   
    
    def update_tiff_compression(self):
        self.tiff_compression_dict = {
            "None":1,
            "CCITT Huffman RLE":2,
            "CCITT Group 3 Fax":3,
            "CCITT Group 4 Fax":4,
            "LZW":5,
            "JPEG":7,
            "ZLIB (DEFLATE)":8,
            "PackBits":32773
        }
        if self.camera_bit == 16 and self.Debayer_mode is False and self.bayer_conversion == 'None':
            update = self.tiff_compression_dict
            del update['CCITT Huffman RLE']
            del update['CCITT Group 3 Fax']
            del update['CCITT Group 4 Fax']
            del update['JPEG']
            self.tiff_compression.clear()
            self.tiff_compression.addItems(list(update.keys()))
        elif self.camera_bit == 8 and self.Debayer_mode is False and self.bayer_conversion == 'None':
            update = self.tiff_compression_dict
            del update['CCITT Huffman RLE']
            del update['CCITT Group 3 Fax']
            del update['CCITT Group 4 Fax']
            del update['JPEG']
            self.tiff_compression.clear()
            self.tiff_compression.addItems(list(self.tiff_compression_dict.keys()))
        elif self.camera_bit == 16 and (self.Debayer_mode is True or self.bayer_conversion != 'None') and self.is_color_camera:
            update = self.tiff_compression_dict
            del update['CCITT Huffman RLE']
            del update['CCITT Group 3 Fax']
            del update['CCITT Group 4 Fax']
            del update['JPEG']
            self.tiff_compression.clear()
            self.tiff_compression.addItems(list(update.keys()))
        elif self.camera_bit == 8 and (self.Debayer_mode is True or self.bayer_conversion != 'None') and self.is_color_camera:
            update = self.tiff_compression_dict
            del update['CCITT Huffman RLE']
            del update['CCITT Group 3 Fax']
            del update['CCITT Group 4 Fax']
            del update['JPEG']
            self.tiff_compression.clear()
            self.tiff_compression.addItems(list(update.keys()))
    
    def update_auto_exposure(self):
        self.sdk_input_queue.put({'order':'get_auto_exposure_is_available', 'data':''})
        
    def update_auto_exposure_success(self,auto_exposure_is_available):
        if auto_exposure_is_available:
            self.auto_exposure_dialog = AutoExposureDialog(self.camera,self.language,self.sdk_input_queue)
            self.auto_exposure_dialog.mode_changed.connect(self.on_auto_exposure_changed)
            self.auto_exposure_button.setVisible(True)
        else:
            self.auto_exposure_button.setVisible(False)
        
    def update_auto_white_balance(self):
        self.sdk_input_queue.put({'order':'get_auto_white_balance_is_available', 'data':''})
        
    def update_auto_white_balance_success(self,auto_white_balance_is_available):
        if auto_white_balance_is_available:
            self.auto_white_balance_button.setVisible(True)
            self.auto_white_balance_dialog = AutoWhiteBalanceDialog(self.camera,self.sdk_input_queue,self.language)
        else:
            self.auto_white_balance_button.setVisible(False)
       
    def update_external_trigger_success(self,data):
        self.external_trigger_is_available = data[0]
        self.trigger_interface_choise = data[1]
        if self.external_trigger_is_available and len(self.trigger_interface_choise) > 0:
           self.show_external_trigger_checkbox.show()
           self.external_trigger_box.setVisible(True)
           self.toggle_external_trigger_box(True)
           self.show_external_trigger_checkbox.setEnabled(True)
           self.trigger_interface_selector.clear()
           self.trigger_interface_selector.addItems(list(self.trigger_interface_choise.keys()))
        else:
            self.external_trigger_box.setVisible(False)
            self.show_external_trigger_checkbox.hide()
            self.toggle_external_trigger_box(False)
            self.show_external_trigger_checkbox.setEnabled(False)
            
    def update_burst_mode(self):
        if self.sdk_input_queue is None:
            return
        self.sdk_input_queue.put({'order':'get_burst_mode_is_available', 'data':''})
        
    def update_burst_mode_success(self,data):
        self.burst_mode_is_available = data
        if self.burst_mode_is_available and self.camera_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"]:
            self.burst_mode_selector.setVisible(True)
        else:
            self.burst_mode_selector.setVisible(False)
    
    def update_GPS_control(self):
        if self.sdk_input_queue is None:
            return
        self.sdk_input_queue.put({'order':'get_GPS_control', 'data':''})
        
    def update_GPS_control_success(self,data):
        if data:
            self.show_GPS_control_checkbox.show()
            self.GPS_control_box.setVisible(True)
            self.toggle_GPS_control_box(True)
            self.show_GPS_control_checkbox.setEnabled(True)
        else:
            self.GPS_control_box.setVisible(False)
            self.show_GPS_control_checkbox.hide()
            self.toggle_GPS_control_box(False)
            self.show_GPS_control_checkbox.setEnabled(False)
    
    # 设置分辨率
    def on_set_resolution_clicked(self):
        x = int(self.x.value())
        y = int(self.y.value())
        w = int(self.w.value())
        h = int(self.h.value())

        if x+w > self.camera_W:
            w = self.camera_W-x
            self.w.setValue(w)
        if y+h > self.camera_H:
            h = self.camera_H-y
            self.h.setValue(h)

        self.image_w = w
        self.image_h = h
        
        self.image_x = x
        self.image_y = y
        
        # 在这里添加处理分辨率设置的代码
        if self.camera_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"] and self.preview_state :
            self.sdk_input_queue.put({'order':'set_preview_pause', 'data':True})
        self.sdk_input_queue.put({'order':'set_resolution', 'data':(x,y,w,h)})
        
    def on_set_resolution_success(self):
        if self.camera_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"] and self.preview_state :
            self.update_shared_image()
            self.sdk_input_queue.put({'order':'set_preview_pause', 'data':False})
   
    def on_set_original_resolution_clicked(self):
        self.image_w = self.camera_W//self.camera_pixel_bin[self.pixel_bin_selector.currentText()][0]
        self.image_h = self.camera_H//self.camera_pixel_bin[self.pixel_bin_selector.currentText()][1]
        self.image_x = 0
        self.image_y = 0
        self.x.setValue(0)
        self.y.setValue(0)
        self.w.setValue(self.image_w)
        self.h.setValue(self.image_h)
        self.on_set_resolution_clicked()

    '''
    控制相机设置区域的显示与隐藏
    '''
    def toggle_settings_box(self,state = None):
        if state is None:   
            # 切换相机设置区域的显示与隐藏
            visible = not self.settings_box.isVisible()
        else:
            visible = state
        self.settings_box.setVisible(visible)
        self.show_settings_checkbox.setStyleSheet("background-color: green;" if visible else "")  # 设置按钮颜色

        if not visible:
            self.layout().removeWidget(self.settings_box) # type: ignore
    
    def toggle_control_box(self,state = None):
        if state is None:   
            # 切换拍摄控制区域的显示与隐藏
            visible = not self.control_box.isVisible()
        else:
            visible = state
        self.control_box.setVisible(visible)
        self.show_control_checkbox.setStyleSheet("background-color: green;" if visible else "")  # 设置按钮颜色

        if not visible:
            self.layout().removeWidget(self.control_box) # type: ignore
    
    def toggle_image_control_box(self,state = None):
        if state is None:  
            # 切换图像控制区域的显示与隐藏
            visible = not self.image_control_box.isVisible()
        else:
            visible = state
        self.image_control_box.setVisible(visible)
        self.show_image_control_checkbox.setStyleSheet("background-color: green;" if visible else "")  # 设置按钮颜色

        if not visible:
            self.layout().removeWidget(self.image_control_box) # type: ignore
    
    def toggle_temperature_control_box(self,state = None):
        if state is None:   
            # 切换温度控制区域的显示与隐藏
            visible = not self.temperature_control_box.isVisible()
        else:
            visible = state
        self.temperature_control_box.setVisible(visible)
        self.show_temperature_control_checkbox.setStyleSheet("background-color: green;" if visible else "")  # 设置按钮颜色

        if not visible:
            self.layout().removeWidget(self.temperature_control_box) # type: ignore
    
    def toggle_CFW_control_box(self,state = None):
        if state is None:   
            # 切换CFW控制区域的显示与隐藏
            visible = not self.CFW_control_box.isVisible()
        else:
            visible = state
        self.CFW_control_box.setVisible(visible)
        self.show_CFW_control_checkbox.setStyleSheet("background-color: green;" if visible else "")  # 设置按钮颜色

        if not visible:
            self.layout().removeWidget(self.CFW_control_box) # type: ignore
    
    def toggle_video_control_box(self,state = None):
        if state is None:   
            # 切换录像控制区域的显示与隐藏
            visible = not self.video_control_box.isVisible()
        else:
            visible = state
        self.video_control_box.setVisible(visible)
        self.show_video_control_checkbox.setStyleSheet("background-color: green;" if visible else "")  # 设置按钮颜色

        if not visible:
            self.layout().removeWidget(self.video_control_box) # type: ignore
    
    def toggle_external_trigger_box(self,state = None):
        if state is None:
            visible = not self.external_trigger_box.isVisible()
        else:
            visible = state
        self.external_trigger_box.setVisible(visible)
        self.show_external_trigger_checkbox.setStyleSheet("background-color: green;" if visible else "")  # 设置按钮颜色
     
    def toggle_burst_control_box(self,state):
        if self.sdk_input_queue is None:
            return
        if self.camera_mode != translations[self.language]["qhyccd_capture"]["continuous_mode"]:
            self.burst_mode_selector.setVisible(False)
            return
        
        if state == Qt.CheckState.Checked:
            self.burst_mode = True
            self.sdk_input_queue.put({'order':'set_burst_mode', 'data':(True,self.burst_mode_min_value_selector.value(),self.burst_mode_max_value_selector.value())})
            self.video_control_box.setEnabled(False)
            self.burst_mode_max_value_selector_label.setVisible(True)
            self.burst_mode_max_value_selector.setVisible(True)
            self.burst_mode_max_value_selector.setEnabled(True)
            self.burst_mode_min_value_selector_label.setVisible(True)
            self.burst_mode_min_value_selector.setVisible(True)
            self.burst_mode_min_value_selector.setEnabled(True)
        else:
            self.burst_mode = False
            self.sdk_input_queue.put({'order':'set_burst_mode', 'data':(False,0,0)})
            self.video_control_box.setEnabled(True)
            self.burst_mode_max_value_selector_label.setVisible(False)
            self.burst_mode_max_value_selector.setVisible(False)
            self.burst_mode_max_value_selector.setEnabled(False)
            self.burst_mode_min_value_selector_label.setVisible(False)
            self.burst_mode_min_value_selector.setVisible(False)
            self.burst_mode_min_value_selector.setEnabled(False)
     
    def toggle_GPS_control_box(self,state = None): 
        if state is None:
            visible = not self.GPS_control_box.isVisible()
        else:
            visible = state
        self.GPS_control_box.setVisible(visible)
        self.show_GPS_control_checkbox.setStyleSheet("background-color: green;" if visible else "")  # 设置按钮颜色
     
    @pyqtSlot(int)
    def on_pixel_bin_changed(self, index):
        if self.sdk_input_queue is None:
            return
        if not self.camera_state:
            return 
        bin_size = self.pixel_bin_selector.itemText(index)
        if bin_size == ' ' or bin_size is None or bin_size == '':
            return 
        self.pixel_bin_selector.setEnabled(False)
        self.bin = self.camera_pixel_bin[bin_size]
        if self.camera_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"] and self.preview_state:
            self.sdk_input_queue.put({"order":"set_preview_pause",'data':True})
        self.sdk_input_queue.put({'order':'set_camera_pixel_bin', 'data':bin_size})
        
        self.image_x = 0
        self.image_y = 0
        self.image_w = int(self.camera_W/self.camera_pixel_bin[bin_size][0])
        self.image_h = int(self.camera_H/self.camera_pixel_bin[bin_size][1])
        self.update_resolution(self.image_x,self.image_y,self.image_w,self.image_h)
    
    def on_set_pixel_bin_success(self):
        bin_size = self.pixel_bin_selector.currentText()
        if self.bin[0] == 1 and self.bin[1] == 1 and self.camera_bit == 8:
            self.Debayer_mode_selector.setEnabled(True)
        else:
            self.Debayer_mode_selector.setEnabled(False)
        self.image_x = 0
        self.image_y = 0
        self.image_w = int(self.camera_W/self.camera_pixel_bin[bin_size][0])
        self.image_h = int(self.camera_H/self.camera_pixel_bin[bin_size][1])

        self.update_resolution(self.image_x,self.image_y,self.image_w,self.image_h)
        if self.camera_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"] and self.preview_state:
            self.update_shared_image()
        self.pixel_bin_selector.setEnabled(True)
        return 

    @pyqtSlot(int)
    def on_depth_changed(self, index):
        # 获取选中的输出格式
        depth = self.depth_selector.itemText(index)
        if depth == ' ' or depth is None or depth == '':
            return 
        
        if self.camera_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"]:
            if self.camera_depth_options[depth] == 16 and self.is_color_camera:
                if self.Debayer_mode_selector.currentText() == translations[self.language]["qhyccd_capture"]["debayer_mode_true"] or self.Debayer_mode == True:
                    self.sdk_input_queue.put({'order':'update_debayer_mode', 'data':False})
                    self.Debayer_mode_selector.setCurrentText(translations[self.language]["qhyccd_capture"]["debayer_mode_false"])
                    self.Debayer_mode = False
                self.Debayer_mode_selector.setEnabled(False)
            elif self.camera_depth_options[depth] == 8 and self.is_color_camera:
                if self.bin[0] == 1 and self.bin[1] == 1:
                    self.Debayer_mode_selector.setEnabled(True)
                else:
                    self.Debayer_mode_selector.setEnabled(False)
            self.camera_bit = self.camera_depth_options[depth]
            if self.preview_state :
                self.sdk_input_queue.put({'order':'set_preview_pause', 'data':True})
        self.sdk_input_queue.put({'order':'set_camera_depth', 'data':self.camera_depth_options[depth]})
        
    def on_set_depth_success(self,data):
        self.camera_bit = data
        if self.camera_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"] and self.preview_state:
            self.update_shared_image()
            self.update_tiff_compression()
        
    @pyqtSlot(int)
    def on_Debayer_mode_changed(self, index):
        if self.sdk_input_queue is None:
            return
        if not self.camera_state:
            return 
        # 获取选中的Debayer模式
        mode = self.Debayer_mode_selector.itemText(index)
        if mode == ' ' or mode is None :
            return 
        self.Debayer_mode = self.camera_Debayer_mode[mode]
        if self.camera_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"]:
            self.sdk_input_queue.put({'order':'set_preview_pause', 'data':True})
            self.on_set_original_resolution_clicked()
            self.set_original_resolution_button.setEnabled(not self.camera_Debayer_mode[mode])
            self.set_resolution_button.setEnabled(not self.camera_Debayer_mode[mode])
            self.show_roi_button.setEnabled(not self.camera_Debayer_mode[mode])
        self.sdk_input_queue.put({'order':'update_debayer_mode', 'data':self.camera_Debayer_mode[mode]})
        if self.camera_Debayer_mode[mode] :
            self.image_c = 3
        else:
            self.image_c = 1
        if self.camera_Debayer_mode[mode]:
            self.pixel_bin_selector.setEnabled(False)
        else:
            self.pixel_bin_selector.setEnabled(True)
        if self.camera_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"]:
            self.update_shared_image()
        self.append_text(f"{translations[self.language]['qhyccd_capture']['update_debayer_mode']}{mode}")
        self.update_tiff_compression()
    
    def start_capture(self):
        if self.sdk_input_queue is None:
            return
        if self.burst_mode:
            self.sdk_input_queue.put({'order':'start_burst_mode', 'data':(True,self.burst_mode_min_value_selector.value(),self.burst_mode_max_value_selector.value())})
            return
        if self.capture_in_progress :
            self.cancel_capture()
            return
        self.capture_in_progress = True
        if self.camera_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"]:
            if 'QHY-Preview' in self.viewer.layers:
                preview_image = self.viewer.layers['QHY-Preview'].data
                self.on_capture_finished({'img':preview_image,'gps_data':None})
            return
        
        self.start_button.setText(translations[self.language]["qhyccd_capture"]["cancel_capture"])
        self.append_text(translations[self.language]["qhyccd_capture"]["start_capture"])
        if self.camera_mode == translations[self.language]["qhyccd_capture"]["single_frame_mode"]:
            if self.is_color_camera and self.Debayer_mode:
                self.image_c = 3
            else:
                self.image_c = 1
            self.sdk_input_queue.put({'order':'singleCapture', 'data':(self.image_w, self.image_h, self.image_c,self.camera_bit,)})
            
    def on_capture_finished(self, data):
        imgdata_np = data['img']
        gps_data = data['gps_data']
        if not self.capture_in_progress :
            return

        if self.bayer_conversion != "None" and imgdata_np.ndim == 2:
            imgdata_np = self.convert_bayer(imgdata_np, self.bayer_conversion)
        
        if self.file_format == "fits":
                dict_value = {
                    "SIMPLE": "T",
                    "BITPIX": imgdata_np.dtype.name,
                    "NAXIS": imgdata_np.ndim,
                    "NAXIS1": self.image_w,
                    "NAXIS2": self.image_h,
                    "EXTEND": "T",
                    "DATE-OBS": datetime.now().strftime("%Y-%m-%d_%H-%M-%S"),
                    "EXPTIME": f"{self.exposure_time.value():.3f} ms",
                    "TELESCOP": self.camera_name,     
                }
                self.fits_header_dialog.update_table_with_dict(dict_value)

        if gps_data is not None:
            self.update_GPS_data(gps_data)

        # 获取当前时间并格式化
        current_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        camera_name = self.camera_selector.currentText()
        
        display_mode = self.display_mode_selector.currentText()
        
        if display_mode == translations[self.language]["qhyccd_capture"]["distributed_display"]:
            self.current_image = imgdata_np
            self.current_image_name = f'{camera_name}-{current_time}'
            self.viewer.add_image(self.current_image, name=self.current_image_name)
            if self.camera_bit == 16:
                self.viewer.layers[self.current_image_name].contrast_limits = (0, 65535)
            else:
                self.viewer.layers[self.current_image_name].contrast_limits = (0, 255)
            if self.camera_mode == translations[self.language]["qhyccd_capture"]["single_frame_mode"]:
                imgdata_np = self.apply_white_balance_software(imgdata_np=self.current_image.copy())
                self.viewer.layers[self.current_image_name].data = imgdata_np
        elif display_mode == translations[self.language]["qhyccd_capture"]["single_display"]:
            self.current_image_name = f'{camera_name}-one'
            if self.current_image is not None and self.current_image_name in self.viewer.layers and self.current_image.ndim == imgdata_np.ndim and self.current_image.shape == imgdata_np.shape:
                self.current_image = imgdata_np

                self.viewer.layers[self.current_image_name].data = self.current_image
            else:
                self.current_image = imgdata_np
                if self.current_image_name in self.viewer.layers:
                    self.viewer.layers.pop(self.current_image_name)
                self.viewer.add_image(self.current_image, name=self.current_image_name)
            
            if self.camera_mode == translations[self.language]["qhyccd_capture"]["single_frame_mode"]:
 
                imgdata_np = self.apply_white_balance_software(imgdata_np=self.current_image.copy())
                self.viewer.layers[self.current_image_name].data = imgdata_np
            
        elif display_mode == translations[self.language]["qhyccd_capture"]["sequential_display"]:
            if imgdata_np.ndim == 2:
                imgdata_np_3c = np.stack([imgdata_np] * 3, axis=-1)
                imgdata_np_3c = imgdata_np_3c[np.newaxis, ...]
            if imgdata_np.ndim == 3:
                imgdata_np_3c = imgdata_np[np.newaxis, ...]

            if self.current_image is None or (imgdata_np_3c.ndim != self.current_image.ndim or imgdata_np_3c.shape[1:] != self.current_image.shape[1:] or imgdata_np_3c.dtype != self.current_image.dtype) or self.current_image.ndim != 4:
                self.current_image = imgdata_np_3c
                self.current_image_name = f'{camera_name}-sequence'
            else:
                self.current_image = np.concatenate((self.current_image, imgdata_np_3c), axis=0)
                self.current_image_name = f'{camera_name}-sequence'
            
            # 检查是否已经存在名为 'qhy-{camera_name}-sequence' 的图层
            if self.current_image_name in self.viewer.layers:
                self.viewer.layers[self.current_image_name].data = self.current_image
            else:
                self.viewer.add_image(self.current_image, name=self.current_image_name)
                if self.camera_bit == 16:
                    self.viewer.layers[self.current_image_name].contrast_limits = (0, 65535)
                else:
                    self.viewer.layers[self.current_image_name].contrast_limits = (0, 255)
            
            if self.camera_mode == translations[self.language]["qhyccd_capture"]["single_frame_mode"] and self.is_color_camera:
                imgdata_np = self.apply_white_balance_software(imgdata_np=self.current_image.copy())
                self.viewer.layers[self.current_image_name].data = imgdata_np

            # 定位显示拍摄的最后一张图片
            self.viewer.layers[self.current_image_name].refresh()
            self.viewer.dims.set_point(0, self.current_image.shape[0] - 1)
            
        # 检查图层是否存在于图层列表中
        if self.current_image_name in self.viewer.layers:
            if self.current_image is None:
                self.current_image = self.viewer.layers[self.current_image_name].data
            # 获取当前图层
            layer = self.viewer.layers[self.current_image_name]
            # 获取当前图层的索引
            current_index = self.viewer.layers.index(layer)
            # 计算最上层的索引（即图层列表的长度减一）
            top_index = len(self.viewer.layers) - 1
            # 如果图层不在最上层，则移动到最上层
            if current_index != top_index and display_mode == translations[self.language]["qhyccd_capture"]["sequential_display"]:
                # 获取当前图层
                layer = self.viewer.layers[self.current_image_name]
                # 移除当前图层
                self.viewer.layers.remove(layer)
                # 重新添加图层到列表末尾，使其显示在最上层
                self.viewer.layers.append(layer)
                # 定位显示拍摄的最后一张图片
                self.viewer.layers[self.current_image_name].refresh()
                self.viewer.dims.set_point(0, self.current_image.shape[0] - 1)
            else:
                # 移动图层到最上层
                self.viewer.layers.move(current_index, top_index)
            if self.histogram_layer_name != self.current_image_name:
                # 设置为当前活跃的图层
                self.viewer.layers.selection.active = layer
            else:
                self.on_selection_changed(None)
            self.viewer.layers[self.current_image_name].contrast_limits = (0, 65535) if self.camera_bit == 16 else (0, 255)
            
        self.append_text(translations[self.language]["qhyccd_capture"]["capture_finished"])
        
        self.capture_in_progress = False
        self.start_button.setText(translations[self.language]["qhyccd_capture"]["start_capture"])
        self.capture_status_label.setText(translations[self.language]["qhyccd_capture"]["capture_finished"])

    def cancel_capture(self):
        self.capture_in_progress = False
        self.start_button.setText(translations[self.language]["qhyccd_capture"]["start_capture"])
        self.capture_status_label.setText(translations[self.language]["qhyccd_capture"]["capture_canceled"])
        self.append_text(translations[self.language]["qhyccd_capture"]["cancel_capture"])
        self.sdk_input_queue.put({"order":"cancel_capture",'data':''})

    def save_image(self):
        if self.current_image is not None:
            options = QFileDialog.Options()
            file_path, _ = QFileDialog.getSaveFileName(self, translations[self.language]["qhyccd_capture"]["save_image"], "", "PNG Files (*.png);;JPEG Files (*.jpg);;All Files (*)", options=options)
            if file_path:
                if not (file_path.endswith('.png') or file_path.endswith('.jpg')):
                    file_path += '.png'

                if self.current_image.ndim == 2:
                    cv2.imwrite(file_path, self.current_image)
                elif self.current_image.ndim == 3 and self.current_image.shape[2] == 3:
                    cv2.imwrite(file_path, cv2.cvtColor(self.current_image, cv2.COLOR_RGB2BGR))
                self.append_text(f"{translations[self.language]['qhyccd_capture']['save_image_success']}:{file_path}")
        else:
            self.append_text(translations[self.language]["qhyccd_capture"]["save_image_failed"])
    
    def update_exposure_time(self):
        # 处理曝光时间变化的逻辑
        exposure_time = int(self.exposure_time.value()*1000)
        self.sdk_input_queue.put({'order':'set_exposure_time', 'data':exposure_time})
    
    def update_exposure_time_success(self,data):
        if self.camera_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"]:
            self.sdk_input_queue.put({'order':'clear_fps_data', 'data':''})
    
    def update_gain(self, value):
        self.sdk_input_queue.put({'order':'set_gain', 'data':value})

    def update_offset(self, value):
        self.sdk_input_queue.put({'order':'set_offset', 'data':value})

    def update_usb_traffic(self, value):
        self.sdk_input_queue.put({'order':'set_usb_traffic', 'data':value})
        
    def update_usb_traffic_success(self,data):
        if self.camera_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"]:
            self.sdk_input_queue.put({'order':'clear_fps_data', 'data':''})
         
    def show_roi_component(self):
        # 检查是否存在以QHY开头的图片
        if not any(layer.name.startswith('QHY') for layer in self.viewer.layers ) :
            warnings.warn(f"{translations[self.language]['debug']['no_qhy_image']}")
            self.append_text(f"{translations[self.language]['debug']['no_qhy_image']}")
            return

        if not self.roi_created:
            self.roi_created = True
            self.show_roi_button.setText(translations[self.language]["qhyccd_capture"]["apply_roi"])
            self.set_resolution_button.setEnabled(False)
            self.viewer.camera.interactive = False  # 锁定图像
            self.append_text(translations[self.language]["qhyccd_capture"]["roi_activated"])
        else:
            self.clear_roi()
            self.roi_created = False
            self.show_roi_button.setText(translations[self.language]["qhyccd_capture"]["roi"])
            self.set_resolution_button.setEnabled(True)
            self.viewer.camera.interactive = True  # 解锁图像
            self.append_text(translations[self.language]["qhyccd_capture"]["roi_closed"])

    def on_mouse_click(self, viewer, event):
        if not self.roi_created:
            return

        if event.type == 'mouse_press' and event.button == 1:
            if len(self.roi_points) >= 2:
                self.clear_roi()

            # 将鼠标点击位置转换为图像坐标
            image_coords = self.viewer.layers[-1].world_to_data(event.position)
            self.roi_points.append(image_coords)

            if len(self.roi_points) == 2:
                self.update_roi_layer()
                self.update_resolution_display()

    def on_mouse_double_click(self, viewer, event):
        self.clear_roi()
        
    def on_save_mode_changed(self, index):
        self.save_mode = self.save_mode_selector.itemText(index)
        if self.save_mode == translations[self.language]["qhyccd_capture"]["single_frame_storage"]:
            self.save_format_selector.clear()
            self.save_format_selector.addItems(['png', 'jpeg', 'tiff', 'fits'])  # 图片格式
        elif self.save_mode == translations[self.language]["qhyccd_capture"]["video_storage"]:
            self.save_format_selector.clear()
            self.save_format_selector.addItems(['avi', 'mp4', 'mkv'])  # 视频格式
    
    def on_save_format_changed(self, index):
        self.file_format = self.save_format_selector.itemText(index)
        if self.save_mode == translations[self.language]["qhyccd_capture"]["single_frame_storage"]:
            if self.file_format == 'png':
                self.file_format = 'png'
                self.jpeg_quality.setVisible(False)
                self.tiff_compression.setVisible(False)
                self.show_fits_header.setVisible(False)
            elif self.file_format == 'jpeg':
                self.file_format = 'jpg'
                self.jpeg_quality.setVisible(True)
                self.tiff_compression.setVisible(False)
                self.show_fits_header.setVisible(False)
            elif self.file_format == 'tiff':
                self.file_format = 'tif'
                self.jpeg_quality.setVisible(False)
                self.tiff_compression.setVisible(True)
                self.show_fits_header.setVisible(False)
            elif self.file_format == 'fits':
                self.file_format = 'fits'
                self.jpeg_quality.setVisible(False)
                self.tiff_compression.setVisible(False)
                self.show_fits_header.setVisible(True)

    def toggle_fits_header(self):
        # 切换FITS头编辑器的显示状态
        self.fits_header_dialog.toggle_window()

    def clear_roi(self):
        try:    
            if self.roi_layer is not None and self.roi_layer in self.viewer.layers:
                self.viewer.layers.remove(self.roi_layer)
        except Exception as e:
            warnings.warn(f"{translations[self.language]['debug']['clear_roi_failed']}: {e}")
            self.append_text(f"{translations[self.language]['debug']['clear_roi_failed']}: {e}")
        self.roi_layer = None
        self.roi_points = []

    def update_roi_layer(self):
        if self.roi_layer is not None:
            self.viewer.layers.remove(self.roi_layer)

        if len(self.roi_points) == 2:
            x0, y0 = self.roi_points[0][-2:]
            x1, y1 = self.roi_points[1][-2:]
            rect = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
            self.roi_layer = self.viewer.add_shapes(
                rect,
                shape_type='rectangle',
                edge_width=10,  # 设置边框宽度为10个像素
                edge_color='green',  # 设置边框颜色为绿色
                face_color='transparent',
                name='ROI'
            )

    def update_resolution_display(self):
        if len(self.roi_points) == 2:
            y0,x0 = self.roi_points[0][-2:]
            if x0 < 0:
                x0 = 0
            if x0 > self.image_w:
                x0 = self.image_w
            if y0 < 0:
                y0 = 0
            if y0 > self.image_h:
                y0 = self.image_h
            y1,x1 = self.roi_points[1][-2:]
            if x1 < 0:
                x1 = 0
            if x1 > self.image_w:
                x1 = self.image_w
            if y1 < 0:
                y1 = 0
            if y1 > self.image_h:
                y1 = self.image_h
            x = int(min(x0, x1))+self.image_x
            y = int(min(y0, y1))+self.image_y
            h = int(abs(y1 - y0))
            w = int(abs(x1 - x0))
            # 确保 x, y, w, h 是偶数
            if x % 2 != 0:
                x += 1  # 如果 x 是奇数，则加 1
            if y % 2 != 0:
                y += 1  # 如果 y 是奇数，则加 1
            if w % 2 != 0:
                w += 1  # 如果 w 是奇数，则加 1
            if h % 2 != 0:
                h += 1  # 如果 h 是奇数，则加 1
            self.x.setValue(int(x))
            self.y.setValue(int(y))
            self.w.setValue(int(w))
            self.h.setValue(int(h))

    def toggle_histogram_display(self, state):
        """切换直方图显示"""
        if state == Qt.Checked: # type: ignore
            self.histogram_widget.show_widget()
        else:
            self.histogram_widget.hide_widget()  # 隐藏直方图窗口
                   
    def on_set_white_balance_clicked(self):
        if self.current_image is None or self.current_image.ndim != 3 or self.current_image.shape[2] == 1:
            return
        red_gain = 1+self.wb_red.value()/ self.wb_red.maximum() # 获取红色增益
        green_gain = 1+self.wb_green.value() / self.wb_green.maximum()  # 获取绿色增益
        blue_gain = 1+self.wb_blue.value() / self.wb_blue.maximum()  # 获取蓝色增益
        imgdata_np = self.apply_white_balance_software(self.current_image.copy(),red_gain,green_gain,blue_gain)
        if imgdata_np is None:
            return
        if self.camera_mode == translations[self.language]["qhyccd_capture"]["single_frame_mode"] and len(self.viewer.layers) > 0 and self.viewer.layers[-1].name.startswith('QHY') and imgdata_np.ndim == self.viewer.layers[-1].data.ndim:
            self.viewer.layers[-1].data = imgdata_np
            self.img_buffer.put(imgdata_np)
    
    def apply_white_balance_software(self, imgdata_np=None, red_gain=None, green_gain=None, blue_gain=None):
        if imgdata_np is None:
            return
        # 获取增益值
        if red_gain is None:
            red_gain = 1 + self.wb_red.value() / self.wb_red.maximum()   # 获取红色增益
        if green_gain is None:
            green_gain = 1 + self.wb_green.value() / self.wb_green.maximum()  # 获取绿色增益
        if blue_gain is None:
            blue_gain = 1 + self.wb_blue.value() / self.wb_blue.maximum()  # 获取蓝色增益

        # 处理单帧图像
        if self.is_color_camera and imgdata_np.ndim == 3 and imgdata_np.shape[-1] == 3:
            imgdata_np = self._apply_gain_to_image(imgdata_np, red_gain, green_gain, blue_gain)

        # 处理序列图像
        elif self.is_color_camera and imgdata_np.ndim == 4 and imgdata_np.shape[-1] == 3:
            imgdata_np[-1] = self._apply_gain_to_image(imgdata_np[-1], red_gain, green_gain, blue_gain)
            self.current_image[-1] = imgdata_np[-1] # type: ignore

        return imgdata_np  # 返回处理后的图像
    
    def _apply_gain_to_image(self, image, red_gain, green_gain, blue_gain):
        """应用增益到单帧图像，优化版本"""
        # 根据图像的数据类型设置最大值
        # start_time = time.time()
        max_value = 65535 if image.dtype == np.uint16 else 255
        # 将增益转换为字符串键
        str_red_gain = f"{red_gain:.2f}"
        str_green_gain = f"{green_gain:.2f}"
        str_blue_gain = f"{blue_gain:.2f}"
        lut_red = self.luts[max_value][str_red_gain]
        lut_green = self.luts[max_value][str_green_gain]
        lut_blue = self.luts[max_value][str_blue_gain]

        # 应用查找表
        image[:, :, 0] = lut_red[image[:, :, 0]]
        image[:, :, 1] = lut_green[image[:, :, 1]]
        image[:, :, 2] = lut_blue[image[:, :, 2]]
        return image    
    
    # 生成全局查找表
    def create_luts(self, max_values, gain_start, gain_end, gain_step):
        """
        创建并保存映射表。
        :param max_values: 像素最大值列表，例如 [255, 65535]
        :param gain_start: 增益起始值
        :param gain_end: 增益结束值
        :param gain_step: 增益步长
        :return: None
        """
        # 使用 np.linspace 确保包括结束值
        num_steps = int((gain_end - gain_start) / gain_step) + 1
        gains = np.linspace(gain_start, gain_end, num_steps)
        
        luts = {}
        for max_value in max_values:
            luts[max_value] = {}
            for gain in gains:
                # 四舍五入增益值到合适的小数位数
                rounded_gain = round(gain, 2)
                original_values = np.arange(max_value + 1)
                adjusted_values = np.clip(original_values * rounded_gain, 0, max_value)
                lut = adjusted_values.astype(np.uint16 if max_value > 255 else np.uint8)
                # 使用四舍五入后的增益值作为键
                luts[max_value][f"{rounded_gain:.2f}"] = lut

        self.luts = luts
        # 保存查找表到文件
        with open('luts.pkl', 'wb') as f:
            pickle.dump(luts, f)
    
    def apply_white_balance_hardware(self):
        red_gain = self.wb_red.value()
        green_gain = self.wb_green.value()
        blue_gain = self.wb_blue.value()
        self.sdk_input_queue.put({"order":"set_white_balance","data":(red_gain, green_gain, blue_gain)})

        return None
    
    def on_bayer_conversion_changed(self, index):
        self.bayer_conversion = self.bayer_conversion_selector.itemText(index)
        self.update_tiff_compression()

    def convert_bayer(self, img, pattern):
        if img.ndim == 2:
            if pattern == "RGGB":
                img_bgr = cv2.cvtColor(img, cv2.COLOR_BAYER_RG2BGR_EA if img.dtype == np.uint16 else cv2.COLOR_BAYER_RG2BGR)
            elif pattern == "BGGR":
                img_bgr = cv2.cvtColor(img, cv2.COLOR_BAYER_BG2BGR_EA if img.dtype == np.uint16 else cv2.COLOR_BAYER_BG2BGR)
            elif pattern == "GRBG":
                img_bgr = cv2.cvtColor(img, cv2.COLOR_BAYER_GR2BGR_EA if img.dtype == np.uint16 else cv2.COLOR_BAYER_GR2BGR)
            elif pattern == "GBRG":
                img_bgr = cv2.cvtColor(img, cv2.COLOR_BAYER_GB2BGR_EA if img.dtype == np.uint16 else cv2.COLOR_BAYER_GB2BGR)
            else:
                return img
            return img_bgr
        return img
        # return cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)  # 将BGR转换为RGB

    def update_current_temperature(self):
        """更新当前温度显示"""
        if self.sdk_input_queue is not None:
            self.sdk_input_queue.put({"order":"get_temperature",'data':''})
              
    def get_temperature_success(self,data):
        self.current_temperature_label.setText(f'{translations[self.language]["qhyccd_capture"]["temperature"]}: {data:.2f} °C')
    
    def update_temperature_setpoint(self, value):
        """更新温度设定点"""
        if self.has_temperature_control:
            self.sdk_input_queue.put({"order":"set_temperature","data":value})
                
    def update_current_humidity(self):
        """更新当前湿度显示"""
        if self.sdk_input_queue is not None:
            self.sdk_input_queue.put({"order":"get_humidity_data",'data':''})
          
    def update_camera_humidity_text(self,data):
        if self.has_humidity_control:
            self.current_humidity_label.setText(f'{translations[self.language]["qhyccd_capture"]["humidity"]}: {data:.2f} %')
        
    def on_CFW_filter_changed(self, index):
        if self.sdk_input_queue is None:
            return
        self.CFW_id = self.CFW_filter_selector.itemText(index)
        if self.CFW_id == "None" or self.CFW_id == "" or self.CFW_id == " ":
            return 
        self.CFW_filter_selector.setEnabled(False)
        self.sdk_input_queue.put({"order":"setCFWFilter","data":self.CFW_id})
    
    def on_set_CFW_filter_success(self,data):
        self.CFW_filter_selector.setEnabled(True)
        
    def swap_elements(self, dictionary, key):
        """
        将指定键的值移到字典的开头，并返回更新后的字典。
        :param dictionary: 要更新的字典
        :param key: 要移动的键
        :return: 更新后的字典
        """
        if key in dictionary:
            value = dictionary.pop(key)  # 移除指定键
            dictionary = {key: value, **dictionary}  # 将其添加到字典的开头
        return dictionary
    
    def toggle_preview_mode(self, state):
        if state == Qt.CheckState.Checked:
            self.preview_status = True
        else:
            self.preview_status = False

    def select_path(self):
        options = QFileDialog.Options()
        directory = QFileDialog.getExistingDirectory(self, translations[self.language]["qhyccd_capture"]["select_save_path"], options=options)
        if directory:
            self.path_selector.setText(directory)
            
    def on_record_mode_changed(self, index):
        self.record_mode = self.record_mode_selector.itemText(index)

        if self.record_mode == translations[self.language]["qhyccd_capture"]["time_mode"]:
            self.record_time_input.setVisible(True)
            self.record_time_input_label.setVisible(True)
            self.frame_count_input.setVisible(False)
            self.frame_count_input_label.setVisible(False)
            self.layout().removeWidget(self.frame_count_input)  # type: ignore
            self.layout().removeWidget(self.frame_count_input_label)  # type: ignore
        elif self.record_mode == translations[self.language]["qhyccd_capture"]["frame_mode"]:
            self.record_time_input.setVisible(False)
            self.record_time_input_label.setVisible(False)
            self.layout().removeWidget(self.record_time_input)  # type: ignore
            self.layout().removeWidget(self.record_time_input_label)  # type: ignore
            self.frame_count_input.setVisible(True)
            self.frame_count_input_label.setVisible(True)

        elif self.record_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"]:
            self.record_time_input.setVisible(False)
            self.record_time_input_label.setVisible(False)
            self.frame_count_input.setVisible(False)
            self.frame_count_input_label.setVisible(False)
            self.layout().removeWidget(self.record_time_input)  # type: ignore
            self.layout().removeWidget(self.record_time_input_label)  # type: ignore
            self.layout().removeWidget(self.frame_count_input)  # type: ignore
            self.layout().removeWidget(self.frame_count_input_label)  # type: ignore

    def start_recording(self):
        self.append_text(translations[self.language]["qhyccd_capture"]["start_recording"])
        self.record_button.setEnabled(False)
        self.is_recording = True
        self.save_progress_indicator.setVisible(True)
        self.save_progress_indicator.setText(translations[self.language]["qhyccd_capture"]["saving"])
        
        record_time_mode = False
        record_frame_mode = False
        continuous_mode = False
        record_time = 0
        total_frames = 0
        
        # 传输数据到保存
        if self.is_recording:
            if self.file_format == "fits":
                if (self.is_color_camera and self.Debayer_mode) or self.bayer_conversion != "None":
                    self.image_c = 3
                else:
                    self.image_c = 1
                dict_value = {
                    "SIMPLE": "T",
                    "BITPIX": self.camera_bit,
                    "NAXIS": self.image_c,
                    "NAXIS1": self.image_w,
                    "NAXIS2": self.image_h,
                    "DATE-OBS": datetime.now().strftime("%Y-%m-%d_%H-%M-%S"),
                    "EXPTIME": f"{self.exposure_time.value():.3f} ms",
                    "TELESCOP": self.camera_name,     
                }
                self.fits_header_dialog.update_table_with_dict(dict_value)
    
            self.record_time_input.setEnabled(False)
            self.frame_count_input.setEnabled(False)
            self.record_mode_selector.setEnabled(False)
            # 重置进度条
            self.progress_bar.setValue(0)
            self.progress_bar.setTextVisible(True)
            self.progress_bar.setStyleSheet("")  # 还原颜色
            
            if self.record_mode == translations[self.language]["qhyccd_capture"]["time_mode"]:
                record_time_mode = True
                record_time = self.record_time_input.value()
                self.progress_bar.setRange(0, 100)
            elif self.record_mode == translations[self.language]["qhyccd_capture"]["frame_mode"]:
                total_frames = self.frame_count_input.value()  # 获取总帧数
                record_frame_mode = True
                self.progress_bar.setRange(0, 100)
            elif self.record_mode == translations[self.language]["qhyccd_capture"]["continuous_mode"]:
                continuous_mode = True
                self.progress_bar.setRange(0, 0)
        
            self.sdk_input_queue.put({"order":"start_save_video",'data':{
                "record_time_mode":record_time_mode,
                "record_frame_mode":record_frame_mode,
                "continuous_mode":continuous_mode,
                "record_time":record_time,
                "total_frames":total_frames,
                "path":self.path_selector.text(),
                "file_name":self.record_file_name.text(),
                "save_format":self.save_format_selector.currentText(),
                "save_mode":self.save_mode,
                "jpeg_quality":self.jpeg_quality.value(),
                "tiff_compression":self.tiff_compression.currentText(),
                "fits_header":self.fits_header_dialog.get_table_data()
            }})
        
    def on_save_thread_finished(self):
        self.save_progress_indicator.setText(translations[self.language]["qhyccd_capture"]["save_completed"])
        self.append_text(translations[self.language]["qhyccd_capture"]["recording_completed"])

    def stop_recording(self):
        if self.sdk_input_queue is not None:
            self.sdk_input_queue.put({"order":"stop_save_video",'data':''})
        
    def stop_recording_success(self,data):
        self.is_recording = False
        self.record_button.setEnabled(True)
        self.save_thread = None
        self.record_time_input.setEnabled(True)
        self.frame_count_input.setEnabled(True)
        self.record_mode_selector.setEnabled(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)  # 重置进度条
        self.append_text(translations[self.language]["qhyccd_capture"]["stop_recording"])

    def start_preview(self):
        if not self.preview_state :
            if self.is_color_camera and self.Debayer_mode:
                channels = 3
            else:
                channels = 1
            self.sdk_input_queue.put({"order":"start_preview",'data':(self.image_w,self.image_h,channels,self.camera_bit,self.exposure_time.value(),self.gain.value(),self.offset.value(),self.Debayer_mode)})
    
    def start_preview_success(self,data):
        self.shared_image_data = data
        self.preview_state  = True
        self.preview_checkbox.setChecked(True)
        self.append_text(translations[self.language]["qhyccd_capture"]["start_preview"])
        
    def update_shared_image(self):
        if self.preview_state :
            if self.is_color_camera and self.Debayer_mode:
                channels = 3
            else:
                channels = 1
            self.sdk_input_queue.put({"order":"update_shared_image_data",'data':(self.image_w,self.image_h,channels,self.camera_bit)})
    
    def update_shared_image_success(self,data):
        self.shared_image_data = data
            
    def stop_preview(self):
        if self.preview_state :
            self.sdk_input_queue.put({"order":"stop_preview",'data':''})
    
    def stop_preview_success(self):
        self.preview_checkbox.setChecked(False)
        self.preview_state = False
        self.append_text(translations[self.language]["qhyccd_capture"]["stop_preview"])
        if 'QHY-Preview' in self.viewer.layers:
            self.viewer.layers.remove('QHY-Preview')
        
    def data_received(self, data):
        fps = data["fps"]
        image_size = data["image_size"]
        image_w, image_h, image_c, image_b = data["shape"]
        shm_status = data["shm_status"]
        gps_data = data["gps_data"]
        try:
            if shm_status:
                with self.lock:
                    if self.shm1 is not None:
                        imgdata_np = self.shm1.buf[:image_size]  # 尝试从共享内存中获取数据
            else:
                with self.lock:
                    if self.shm2 is not None:
                        imgdata_np = self.shm2.buf[:image_size]  # 尝试从共享内存中获取数据
                        expect_size = image_w * image_h * image_c * (image_b // 8)
                        if len(imgdata_np) != expect_size:
                            self.append_text(translations[self.language]['debug']['shm_data_size_error'],True)
                            return
            imgdata_np = np.frombuffer(imgdata_np, dtype=np.uint8 if image_b == 8 else np.uint16).reshape(image_w, image_h) if image_c == 1 else np.frombuffer(imgdata_np, dtype=np.uint8 if image_b == 8 else np.uint16).reshape(image_w, image_h, image_c)
        except ValueError:
            return  # 退出函数
        except queue.Empty:
            return  # 退出函数
        if imgdata_np is None:
            return
        self.update_GPS_data(gps_data)
                
        # 获取当前时间
        current_time = time.time()
        # 传输数据到画布显示，限制最高帧率为30fps   
        if self.last_update_time is not None and current_time - self.last_update_time < 1/30:
            return
        else:   
            if self.is_color_camera and self.bayer_conversion != "None":
                imgdata_np = self.convert_bayer(imgdata_np, self.bayer_conversion)
            self.update_viewer(imgdata_np, fps)
            self.last_update_time = current_time
            
        if (self.last_histogram_update_time is None or current_time - self.last_histogram_update_time > 0.1) and self.histogram_layer_name == "QHY-Preview":
            self.img_buffer.put(imgdata_np)
            self.last_histogram_update_time = current_time
            if self.contrast_limits_name != 'QHY-Preview':
                self.contrast_limits_name = 'QHY-Preview'
                self.bind_contrast_limits_event()
                contrast_limits = self.viewer.layers[self.contrast_limits_name].contrast_limits
                self.histogram_widget.update_min_max_lines(contrast_limits[0], contrast_limits[1])
                
    def update_viewer(self, imgdata_np, fps):
        layer_name = 'QHY-Preview'
        
        self.preview_image = imgdata_np
        if not self.preview_status:
            if layer_name in self.viewer.layers:
                self.viewer.layers.remove(layer_name)
            return

        self.fps_label.setText(f'FPS: {fps:.2f}')
        
        if layer_name in self.viewer.layers:
            if self.viewer.layers[layer_name].data.shape == imgdata_np.shape:
                self.viewer.layers[layer_name].data = imgdata_np
            else:
                self.viewer.layers.remove(layer_name)
                self.viewer.add_image(imgdata_np, name=layer_name)
                if self.camera_bit == 16:
                    self.viewer.layers[layer_name].contrast_limits = (0, 65535)
                else:
                    self.viewer.layers[layer_name].contrast_limits = (0, 255)
        else:
            self.viewer.add_image(imgdata_np, name=layer_name)
            if self.camera_bit == 16:
                self.viewer.layers[layer_name].contrast_limits = (0, 65535)
            else:
                self.viewer.layers[layer_name].contrast_limits = (0, 255)

        # 如果需要将图层移动到顶部，可以使用以下方法
        if self.top_checkbox.isChecked():  # 检查复选框状态
            layer_index = self.viewer.layers.index(layer_name)  # 获取图层的索引
            self.viewer.layers.move(layer_index, -1)  # 将图层移动到索引-1的位置
            self.bind_contrast_limits_event()

    def bind_contrast_limits_event(self):
        if self.contrast_limits_name is None or self.contrast_limits_name not in self.viewer.layers:
            return
        # 绑定当前图层的对比度限制变化事件
        current_layer = self.viewer.layers[self.contrast_limits_name]
        try:
            self.contrast_limits_connection = current_layer.events.contrast_limits.connect(self.on_contrast_limits_change)
        except Exception as e:
            self.append_text(f"Error connecting contrast limits event: {e}",True)

    def on_contrast_limits_change(self, event):
        if self.contrast_limits_name in self.viewer.layers:
            try:    
                # 当对比度限制变化时触发的函数
                contrast_limits = self.viewer.layers[self.contrast_limits_name].contrast_limits
                self.histogram_widget.update_min_max_lines(contrast_limits[0], contrast_limits[1])
            except Exception as e:
                self.append_text(f"Error on_contrast_limits_change: {e}",True)
                return

    def toggle_auto_exposure(self):
        self.auto_exposure_dialog.exec_() # type: ignore

    def toggle_auto_white_balance(self):
        if self.auto_white_balance_button.text() == translations[self.language]['qhyccd_capture']['auto_white_balance_stop']:
            self.auto_white_balance_dialog.stop() # type: ignore
            self.auto_white_balance_button.setText(translations[self.language]['qhyccd_capture']['auto_white_balance'])
        else:
            self.auto_white_balance_dialog.start() # type: ignore
            self.auto_white_balance_button.setText(translations[self.language]['qhyccd_capture']['auto_white_balance_stop'])
            
    def on_auto_exposure_changed(self, mode):
        if mode == 0:
            self.exposure_time.setEnabled(True)
        else:
            self.exposure_time.setEnabled(False)
     
    def on_auto_exposure_value_changed(self, exposure_time):
        self.exposure_time.setValue(exposure_time)
        
    def on_auto_white_balance_complete(self, data):
        wb_red, wb_green, wb_blue, auto_white_balance_is_running = data
        self.auto_white_balance_button.setEnabled(True)
        self.wb_red.setValue(wb_red)
        self.wb_green.setValue(wb_green)
        self.wb_blue.setValue(wb_blue)
        if not auto_white_balance_is_running:
            self.auto_white_balance_dialog.stop() # type: ignore
            self.auto_white_balance_button.setText(translations[self.language]['qhyccd_capture']['auto_white_balance'])
    
    def get_image_layer(self):
        # 从最后一个图层开始向前检查，找到第一个图像图层
        for layer in reversed(self.viewer.layers):
            if isinstance(layer, napari.layers.Image):  # type: ignore
                return layer.data
        return None
           
    def star_analysis(self):
        image = self.get_image_layer()
        if image is None:
            self.append_text(translations[self.language]['qhyccd_capture']['no_image_layer'])
            return
        self.append_text(translations[self.language]['qhyccd_capture']['prepare_to_star_analysis'])
        # 如果图像是彩色的，转换为灰度图
        if image.ndim == 3 and image.shape[2] == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        elif image.ndim == 4 and image.shape[-1] == 3:
            image = image[0]
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        if self.star_analysis_method_selector.currentText() == 'photutils':
            self.star_progress_bar.setRange(0, 0)
            from photutils import DAOStarFinder
            # 计算背景统计数据
            mean, median, std = sigma_clipped_stats(image, sigma=3.0)

            fwhm = self.star_fwhm.value()
            # 使用 DAOStarFinder 进行星点检测
            daofinder = DAOStarFinder(fwhm=fwhm, threshold=5.*std)
            sources = daofinder(image - median)
            if sources is None or len(sources) == 0:
                self.append_text(translations[self.language]['qhyccd_capture']['no_detected_stars'])
                self.star_progress_bar.setRange(0, 100)
                return

            # 创建新的星点信息表格
            self.star_table = QTableWidget()
            self.star_table.setWindowTitle("Detected Stars")
            self.star_table.setColumnCount(len(sources.colnames))  # 设置列数为 sources 表的列数
            self.star_table.setHorizontalHeaderLabels(sources.colnames)  # 设置表头为 sources 表的列名
            self.star_table.setRowCount(len(sources))  # 设置行数为检测到的星点数

            # 创建点的列表
            points = []
            # 填充表格并添加点
            for i, star in enumerate(sources):
                for j, col_name in enumerate(sources.colnames):
                    value = star[col_name]
                    if isinstance(value, float):
                        value = f"{value:.2f}"  # 格式化浮点数
                    self.star_table.setItem(i, j, QTableWidgetItem(str(value)))

                # 添加点到列表
                point = [star['ycentroid'], star['xcentroid']]
                points.append(point)
            # 显示表格
            self.star_table.show()
            # 在 napari 查看器中添加点图层
            if 'Star Points' in self.viewer.layers:
                self.viewer.layers['Star Points'].data = points
                layer_index = self.viewer.layers.index('Star Points')
                self.viewer.layers.move(layer_index, -1)
            else:
                self.viewer.add_points(points, size=fwhm, face_color='red', edge_color='white', name='Star Points')
            self.star_progress_bar.setRange(0, 100)
            self.append_text(translations[self.language]['qhyccd_capture']['star_analysis_completed'])
        elif self.star_analysis_method_selector.currentText() == 'Astrometry' and self.astrometrySolver is not None:
            dialog = AstrometryDialog(self, self.astrometrySolver,self.language)
            if dialog.exec_() == QDialog.Accepted:  # 检查对话框是否被接受
                self.star_progress_bar.setRange(0, 0)
                params = dialog.get_parameters()
                self.astrometrySolver.start_solving(image_input=image, params=params)
                self.star_analysis_button.setEnabled(False)
            else:
                self.append_text(translations[self.language]['qhyccd_capture']['cancel_solving'])  # 可以根据需要处理用户取消的情况
            
    def parse_star_data(self,data):
        # 分割数据为行
        lines = data.strip().split('\n')
        title_line = lines[0].split()
        title_line.insert(0, "ID")
        star_dict = {}
        for i,title in enumerate(title_line):
            star_dict[title] = []
        for line in lines[1:]:
            parts = line.split()
            for i,title in enumerate(title_line):
                star_dict[title].append(float(parts[i]))
        return star_dict    
            
    def on_astrometry_finished(self, result):
        self.append_text(f"Astrometry result: {result}")
        self.star_analysis_button.setEnabled(True)
        self.star_progress_bar.setRange(0, 100)
        self.append_text(translations[self.language]['qhyccd_capture']['star_analysis_completed'])
             
    def on_astrometry_error(self, error):
        warnings.warn(f"Astrometry error: {error}")
        self.append_text(f"Astrometry error: {error}")
        self.star_analysis_button.setEnabled(True)
        self.star_progress_bar.setRange(0, 100)
        
    def on_astrometry_star_info(self, data, wcs,wcs_tip):
        self.star_dict = self.parse_star_data(data)
        self.star_table = QTableWidget()
        self.star_table.setWindowTitle("Detected Stars")
        if wcs_tip:
            self.star_table.setColumnCount(len(self.star_dict) + 2)  
            headers = list(self.star_dict.keys()) + ['RA', 'Dec']
        else:
            self.star_table.setColumnCount(len(self.star_dict))
            headers = list(self.star_dict.keys())
        self.star_table.setHorizontalHeaderLabels(headers)
        self.star_table.setRowCount(len(self.star_dict[list(self.star_dict.keys())[0]]))

        points = []
        properties = {'info': []}  # 创建一个字典来存储每个点的信息
        
        for i in range(len(self.star_dict[list(self.star_dict.keys())[0]])):
            x = self.star_dict['X'][i]
            y = self.star_dict['Y'][i]
            if wcs_tip:
                ra, dec = wcs.all_pix2world(x, y, 0)
                info = f"RA: {ra:.6f}, Dec: {dec:.6f}"
                properties['info'].append(info)  # 将信息添加到 properties 字典中

            for j, key in enumerate(self.star_dict.keys()):
                value = self.star_dict[key][i]
                if isinstance(value, float):
                    value = f"{value:.2f}"
                self.star_table.setItem(i, j, QTableWidgetItem(str(value)))
            if wcs_tip:
                self.star_table.setItem(i, len(self.star_dict), QTableWidgetItem(f"{ra:.6f}"))
                self.star_table.setItem(i, len(self.star_dict) + 1, QTableWidgetItem(f"{dec:.6f}"))
            point = [y, x]  # 注意 napari 使用 (y, x) 格式
            points.append(point)

        self.star_table.show()

        # 在 napari 查看器中添加点图层并绑定鼠标悬停事件
        if 'Star Points' in self.viewer.layers:
            self.viewer.layers.remove('Star Points')
        if wcs_tip:
            points_layer = self.viewer.add_points(points, size=self.star_fwhm.value(), face_color='red', border_color='white', name='Star Points', properties=properties)
            points_layer.mouse_move_callbacks.append(self.display_point_info)
        else:
            points_layer = self.viewer.add_points(points, size=self.star_fwhm.value(), face_color='red', border_color='white', name='Star Points')
    
        self.star_progress_bar.setRange(0, 100)

    def display_point_info(self, layer, event):
        """显示鼠标悬停点的信息"""
        hovered_point_index = layer.get_value(event.position, world=True)
        if hovered_point_index is None:
            self.viewer.status = ""
            return
        info = layer.properties['info'][hovered_point_index]
        self.viewer.status = info  # 将信息显示在 napari 
        
    def save_star_table(self):
        if self.star_table is None:
            self.append_text(translations[self.language]['qhyccd_capture']['no_detected_stars'])
            return
        
        file_path, _ = QFileDialog.getSaveFileName(self, translations[self.language]['qhyccd_capture']['save_star_table'], "", "CSV Files (*.csv)")
        if file_path:
            with open(file_path, 'w', newline='') as file:
                writer = csv.writer(file)
                # 获取所有列的标题
                headers = [self.star_table.horizontalHeaderItem(i).text() for i in range(self.star_table.columnCount())]    # type: ignore
                writer.writerow(headers)
                # 写入每一行的数据
                for row in range(self.star_table.rowCount()):
                    row_data = [self.star_table.item(row, col).text() for col in range(self.star_table.columnCount())]    # type: ignore
                    writer.writerow(row_data)
            self.append_text(f"{translations[self.language]['qhyccd_capture']['star_table_saved']}: {file_path}")
        
    def on_selection_changed(self,event):
        selected_layers = self.viewer.layers.selection
        if len(selected_layers) == 1:
            for layer in selected_layers:
                if isinstance(layer, napari.layers.Image):  # type: ignore
                    self.histogram_layer_name = layer.name
        elif len(selected_layers) > 1:
            for layer in selected_layers:
                if layer.name == 'QHY-Preview':
                    self.histogram_layer_name = layer.name
        if self.histogram_layer_name is not None and self.histogram_layer_name != 'QHY-Preview':
            imgdata_np = self.viewer.layers[self.histogram_layer_name].data
            
            # 确保 imgdata_np 是一个 NumPy 数组
            if not isinstance(imgdata_np, np.ndarray):
                imgdata_np = np.array(imgdata_np)
            
            if imgdata_np.ndim == 4 :
                current_time_index = self.viewer.dims.current_step[0] 
                self.viewer.dims.events.current_step.connect(self.on_time_index_change)
                imgdata_np = imgdata_np[current_time_index]
                if np.array_equal(imgdata_np[:,:,0], imgdata_np[:,:,1]) and np.array_equal(imgdata_np[:,:,1], imgdata_np[:,:,2]):
                    imgdata_np = cv2.cvtColor(imgdata_np, cv2.COLOR_BGR2GRAY)
            else:
                self.viewer.dims.events.current_step.disconnect(self.on_time_index_change)
            self.contrast_limits_name = self.histogram_layer_name
            self.bind_contrast_limits_event()
            contrast_limits = self.viewer.layers[self.contrast_limits_name].contrast_limits
            self.img_buffer.put(imgdata_np)
            # self.histogram_widget.update_histogram()
            self.histogram_widget.update_min_max_lines(contrast_limits[0], contrast_limits[1])
            
    def on_time_index_change(self,event):
        current_time_index = self.viewer.dims.current_step[0] 
        imgdata_np = self.viewer.layers[self.histogram_layer_name].data[current_time_index]
        if imgdata_np.ndim == 3 :
            if np.array_equal(imgdata_np[:,:,0], imgdata_np[:,:,1]) and np.array_equal(imgdata_np[:,:,1], imgdata_np[:,:,2]):
                imgdata_np = cv2.cvtColor(imgdata_np, cv2.COLOR_BGR2GRAY)
        self.img_buffer.put(imgdata_np)
        # self.histogram_widget.update_histogram()
        
    def show_planned_shooting_dialog(self):
        self.planned_shooting_dialog.show()
        
    def update_planned_shooting_data(self,data):
        self.planned_shooting_data = data
        self.planned_shooting_dialog.updateTableOptions(data)

    def on_plan_running(self,data):
        if self.sdk_input_queue is not None:
            self.sdk_input_queue.put({'order':'run_plan', 'data':data})
        
    def on_plan_success(self,image_data):
        self.planned_shooting_dialog.update_row_state()
        self.viewer.add_image(image_data, name='Plan Shooting')
        
    def toggle_external_trigger_enabled(self,state):
        if self.sdk_input_queue is not None:
            self.trigger_interface_selector.setEnabled(not state)
            self.use_trigger_output_checkbox.setEnabled(not state)
            self.settings_box.setEnabled(not state)
            self.video_control_box.setEnabled(not state)
            if state:
                self.temperature_update_timer.stop()
                self.sdk_input_queue.put({'order':'set_external_trigger', 'data':(self.trigger_interface_selector.currentText(), self.use_trigger_output_checkbox.isChecked(), (self.image_w, self.image_h, self.image_c, self.camera_bit))})
            else:
                self.sdk_input_queue.put({'order':'stop_external_trigger', 'data':''})
                self.temperature_update_timer.start(5000)

    def stop_external_trigger_success(self,data):
        self.on_set_resolution_clicked()
        
    def on_burst_mode_min_value_changed(self,value):
        if self.burst_mode_min_value_selector.value() > self.burst_mode_max_value_selector.value():
            self.burst_mode_min_value_selector.setValue(self.burst_mode_max_value_selector.value()-2)

    def on_burst_mode_max_value_changed(self,value):
        if self.burst_mode_min_value_selector.value() > self.burst_mode_max_value_selector.value():
            self.burst_mode_max_value_selector.setValue(self.burst_mode_min_value_selector.value()+2)
        
    def on_burst_mode_frame(self,data):
        image_size = data["image_size"]
        image_w, image_h, image_c, image_b = data["shape"]
        shm_status = data["shm_status"]
        gps_data = data["gps_data"]
        try:
            if shm_status:
                with self.lock:
                    if self.shm1 is not None:
                        imgdata_np = self.shm1.buf[:image_size]  # 尝试从共享内存中获取数据
            else:
                with self.lock:
                    if self.shm2 is not None:
                        imgdata_np = self.shm2.buf[:image_size]  # 尝试从共享内存中获取数据
                        expect_size = image_w * image_h * image_c * (image_b // 8)
                        if len(imgdata_np) != expect_size:
                            self.append_text(translations[self.language]['debug']['shm_data_size_error'],True)
                            return
            imgdata_np = np.frombuffer(imgdata_np, dtype=np.uint8 if image_b == 8 else np.uint16).reshape(image_w, image_h) if image_c == 1 else np.frombuffer(imgdata_np, dtype=np.uint8 if image_b == 8 else np.uint16).reshape(image_w, image_h, image_c)
        except ValueError:
            return  # 退出函数
        except queue.Empty:
            return  # 退出函数
        if imgdata_np is None:
            return
        self.update_GPS_data(gps_data)
        if self.is_color_camera and self.bayer_conversion != "None":
            imgdata_np = self.convert_bayer(imgdata_np, self.bayer_conversion)
        self.viewer.add_image(imgdata_np, name='Burst Mode')
    
    def toggle_GPS_control(self,state):
        if self.sdk_input_queue is not None:
            self.sdk_input_queue.put({'order':'set_GPS_control', 'data':state})
    
    def on_GPS_control_success(self,data):
        self.GPS_control = data
    
    def is_leap_year(self,year):
        """判断是否为闰年"""
        return year % 400 == 0 or (year % 4 == 0 and year % 100 != 0)

    def seconds_to_time(self, sec, usec, timezone='US/Eastern'):
        """将秒和微秒转换为从1995年10月10日开始的完整日期时间对象，精确到秒，并考虑闰年"""
        # 将微秒转换为秒
        full_seconds = sec + usec / 1_000_000
        # 创建基准日期1995年10月10日
        base_date = datetime(1995, 10, 10)
        # 计算最终日期时间
        final_date = base_date + timedelta(seconds=full_seconds)

        # 处理日期，考虑闰年
        year = final_date.year
        month = final_date.month
        day = final_date.day
        hour = final_date.hour
        minute = final_date.minute
        second = final_date.second

        # 调整日期和时间
        days_in_month = [31, 28 + self.is_leap_year(year), 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        while day > days_in_month[month - 1]:
            day -= days_in_month[month - 1]
            month += 1
            if month > 12:
                month = 1
                year += 1

        # 重新构建日期时间对象
        adjusted_date = datetime(year, month, day, hour, minute, second)
        # 转换为指定时区
        tz = pytz.timezone(timezone)
        local_time = adjusted_date.replace(tzinfo=pytz.utc).astimezone(tz)
        # 格式化时间为年月日，时分秒
        return local_time.strftime('%Y-%m-%d %H:%M:%S')
    
    def parse_gps_data(self,gps_data):
        gps = np.array(gps_data, dtype=np.uint8)  # 假设gps_data已经是一个字节数组

        # 在进行计算之前，将需要用于计算的元素转换为 int 类型
        # GPS状态
        now_flag = (int(gps[33]) // 16) % 4
        # PPS计数值
        pps = 256*256*int(gps[41]) + 256*int(gps[42]) + int(gps[43])
        # 帧序号
        seqNumber = 256*256*256*int(gps[0]) + 256*256*int(gps[1]) + 256*int(gps[2]) + int(gps[3])
        # 图像宽度
        width = 256*int(gps[5]) + int(gps[6])
        # 图像高度
        height = 256*int(gps[7]) + int(gps[8])

        # 纬度解析
        temp = 256*256*256*int(gps[9]) + 256*256*int(gps[10]) + 256*int(gps[11]) + int(gps[12])
        south = temp > 1000000000
        deg = (temp % 1000000000) // 10000000
        min = (temp % 10000000) // 100000
        fractMin = (temp % 100000) / 100000.0
        latitude = (deg + (min + fractMin) / 60.0) * (1 if not south else -1)

        # 经度解析
        temp = 256*256*256*int(gps[13]) + 256*256*int(gps[14]) + 256*int(gps[15]) + int(gps[16])
        west = temp > 1000000000
        deg = (temp % 1000000000) // 1000000
        min = (temp % 1000000) // 10000
        fractMin = (temp % 10000) / 10000.0
        longitude = (deg + (min + fractMin) / 60.0) * (1 if not west else -1)

        # 时间解析
        start_sec = 256*256*256*int(gps[18]) + 256*256*int(gps[19]) + 256*int(gps[20]) + int(gps[21])
        start_us = (256*256*int(gps[22]) + 256*int(gps[23]) + int(gps[24])) // 10
        end_sec = 256*256*256*int(gps[26]) + 256*256*int(gps[27]) + 256*int(gps[28]) + int(gps[29])
        end_us = (256*256*int(gps[30]) + 256*int(gps[31]) + int(gps[32])) // 10
        now_sec = 256*256*256*int(gps[34]) + 256*256*int(gps[35]) + 256*int(gps[36]) + int(gps[37])
        now_us = (256*256*int(gps[38]) + 256*int(gps[39]) + int(gps[40])) // 10

        start_time = self.seconds_to_time(start_sec, start_us)
        end_time = self.seconds_to_time(end_sec, end_us)
        current_time = self.seconds_to_time(now_sec, now_us)

        # 曝光时间计算
        exposure = (end_sec - start_sec) * 1000000 + (end_us - start_us)

        return {
            "now_flag": now_flag,
            "pps": pps,
            "seqNumber": seqNumber,
            "width": width,
            "height": height,
            "latitude": latitude,
            "longitude": longitude,
            "start_time": start_time,
            "end_time": end_time,
            "current_time": current_time,
            "exposure": exposure
        }
    
    def update_GPS_data(self, data):
        if data is None or len(data) == 0:
            return
        data_dict = self.parse_gps_data(data)
        # 将数据字典转换为键值对字符串，确保冒号对齐
        max_key_length = max(len(key) for key in data_dict.keys())  # 获取最长键的长度
        html = "<pre>"
        for key, value in data_dict.items():
            # 使用ljust确保键对齐
            html += f"{key.ljust(max_key_length)} : {value}\n"
        html += "</pre>"

        # 设置QLabel的文本
        self.GPS_data_label.setText(html)
    
@napari_hook_implementation
def napari_experimental_provide_dock_widget():
    """注册插件窗口部件"""
    return CameraControlWidget

