# qhyccd-capture

## 项目简介

`qhyccd-capture` 是一个用于操作 QHYCCD 系列相机的基本操作库。该库提供了与 QHYCCD 相机进行交互的功能，包括相机连接、参数设置、图像捕获和显示等。该项目是一个 [napari] 插件，旨在通过图形用户界面简化相机的使用。

## 功能

- **相机连接**：支持在不同操作系统（如 Windows、Linux、macOS）上加载相应的 QHYCCD 动态链接库，并初始化相机资源。
- **参数设置**：提供了设置相机参数的功能，如曝光时间、增益、偏移量、USB 带宽等。
- **图像捕获**：支持单帧模式曝光，并获取图像数据。
- **图像显示**：通过 napari 显示捕获的图像，支持分布式显示、单一显示和序列显示模式。
- **直方图和白平衡**：提供直方图均衡化和白平衡调整功能。
- **ROI（感兴趣区域）**：支持创建和应用 ROI，以便对特定区域进行操作。

## 安装
您可以通过pip安装:qhyccd-capture

    pip install qhyccd-capture

如果要安装最新的开发版本:

    pip install git+https://github.com/nightliar-L/qhyccd-capture.git



## 使用

    napari  

## 依赖安装
#### Astrometry.net

    sudo apt-get install astrometry.net
    sudo apt-get install astrometry-data-tycho2
    sudo vim ~/.bashrc
    # 添加以下内容
    export PATH=$PATH:/usr/local/astrometry/bin

# 版本变化

- 2024-10-23 版本 0.0.1 初始版本 实现了QHYCCD相机的基本操作

## Contributing

Contributions are very welcome. Tests can be run with [tox], please ensure
the coverage at least stays the same before you submit a pull request.

## License

Distributed under the terms of the [BSD-3] license,
"qhyccd-sdk" is free and open source software

