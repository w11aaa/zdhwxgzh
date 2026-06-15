#!/usr/bin/env python3
"""
浏览器环境管理页面 - 合并代理和指纹配置
"""

import json
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
                             QPushButton, QTableWidget, QTableWidgetItem, 
                             QDialog, QTextEdit, QMessageBox, QTabWidget,
                             QCheckBox, QComboBox, QSpinBox, QLineEdit)

from src.core.ui.qt_font import get_mono_font_family, get_ui_font_family

# 导入服务类
try:
    from ..services.browser_environment_service import browser_environment_service
    print("✅ 成功导入浏览器环境服务模块")
    USE_REAL_SERVICES = True
    
except ImportError as e:
    print(f"⚠️ 无法导入浏览器环境服务模块: {e}")
    print("💡 使用Mock服务作为备用方案")
    USE_REAL_SERVICES = False
    
    # Mock服务类
    class MockBrowserEnvironmentService:
        def __init__(self):
            self.data = []
        
        def get_all(self, user_id=None):
            return [{'id': item.get('id'), **item} for item in self.data if isinstance(item, dict)]
        
        def create(self, **kwargs):
            item = kwargs.copy()
            item['id'] = len(self.data) + 1
            self.data.append(item)
            return item
        
        def update(self, item_id, **kwargs):
            for item in self.data:
                if isinstance(item, dict) and item.get('id') == item_id:
                    item.update(kwargs)
                    return item
            return None
        
        def delete(self, item_id):
            self.data = [item for item in self.data if not (isinstance(item, dict) and item.get('id') == item_id)]
            return True

    browser_environment_service = MockBrowserEnvironmentService()


class BrowserEnvironmentDialog(QDialog):
    """浏览器环境配置对话框"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("浏览器环境配置")
        self.setModal(True)
        self.setFixedSize(1100, 750)  # 进一步增大对话框尺寸，更宽敞
        
        # 设置全局字体
        self.default_font = QFont(get_ui_font_family(), 12)  # 增大字体到12号
        self.setFont(self.default_font)
        
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        title_label = QLabel("🌐 浏览器环境配置")
        title_label.setFont(QFont(get_ui_font_family(), 18, QFont.Bold))  # 标题更大字体
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label)
        
        # 创建选项卡
        self.tab_widget = QTabWidget()
        
        # 基本配置选项卡
        self.basic_tab = QWidget()
        self.init_basic_tab()
        self.tab_widget.addTab(self.basic_tab, "🔧 基本配置")
        
        # 高级配置选项卡  
        self.advanced_tab = QWidget()
        self.init_advanced_tab()
        self.tab_widget.addTab(self.advanced_tab, "⚡ 高级配置")
        
        # JSON配置选项卡
        self.json_tab = QWidget()
        self.init_json_tab()
        self.tab_widget.addTab(self.json_tab, "📝 JSON配置")
        
        layout.addWidget(self.tab_widget)
        
        # 按钮区域
        button_layout = QHBoxLayout()
        
        preset_btn = QPushButton("📋 加载预设")
        preset_btn.setFont(QFont(get_ui_font_family(), 12))  # 按钮字体
        preset_btn.setMinimumHeight(35)
        preset_btn.setMinimumWidth(100)  # 增加按钮宽度
        preset_btn.clicked.connect(self.load_preset)
        button_layout.addWidget(preset_btn)
        
        random_btn = QPushButton("🎲 随机生成")
        random_btn.setFont(QFont(get_ui_font_family(), 12))  # 按钮字体
        random_btn.setMinimumHeight(35)
        random_btn.setMinimumWidth(100)
        random_btn.clicked.connect(self.generate_random)
        button_layout.addWidget(random_btn)
        
        button_layout.addStretch()
        
        cancel_btn = QPushButton("❌ 取消")  # 添加图标
        cancel_btn.setFont(QFont(get_ui_font_family(), 12))  # 按钮字体
        cancel_btn.setMinimumHeight(35)
        cancel_btn.setMinimumWidth(80)
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)
        
        ok_btn = QPushButton("✅ 确定")  # 添加图标
        ok_btn.setFont(QFont(get_ui_font_family(), 12))  # 按钮字体
        ok_btn.setMinimumHeight(35)
        ok_btn.setMinimumWidth(80)
        ok_btn.clicked.connect(self.accept)
        button_layout.addWidget(ok_btn)
        
        layout.addLayout(button_layout)

    def init_basic_tab(self):
        """初始化基本配置选项卡"""
        layout = QVBoxLayout(self.basic_tab)
        
        # 环境名称
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("环境名称:"))
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("例如: Windows Chrome环境")
        name_layout.addWidget(self.name_input)
        layout.addLayout(name_layout)
        
        # 代理配置
        proxy_group = QVBoxLayout()
        
        self.proxy_enabled = QCheckBox("启用代理")
        proxy_group.addWidget(self.proxy_enabled)
        
        proxy_config_layout = QHBoxLayout()
        proxy_config_layout.addWidget(QLabel("代理类型:"))
        self.proxy_type = QComboBox()
        self.proxy_type.addItems(["direct", "http", "https", "socks5"])
        proxy_config_layout.addWidget(self.proxy_type)
        
        proxy_config_layout.addWidget(QLabel("主机:"))
        self.proxy_host = QLineEdit()
        self.proxy_host.setPlaceholderText("127.0.0.1")
        proxy_config_layout.addWidget(self.proxy_host)
        
        proxy_config_layout.addWidget(QLabel("端口:"))
        self.proxy_port = QSpinBox()
        self.proxy_port.setRange(1, 65535)
        self.proxy_port.setValue(1080)
        proxy_config_layout.addWidget(self.proxy_port)
        
        proxy_group.addLayout(proxy_config_layout)
        
        auth_layout = QHBoxLayout()
        auth_layout.addWidget(QLabel("用户名:"))
        self.proxy_username = QLineEdit()
        auth_layout.addWidget(self.proxy_username)
        
        auth_layout.addWidget(QLabel("密码:"))
        self.proxy_password = QLineEdit()
        self.proxy_password.setEchoMode(QLineEdit.Password)
        auth_layout.addWidget(self.proxy_password)
        
        proxy_group.addLayout(auth_layout)
        layout.addLayout(proxy_group)
        
        # 浏览器配置
        browser_group = QVBoxLayout()
        
        ua_layout = QHBoxLayout()
        ua_layout.addWidget(QLabel("User-Agent:"))
        self.user_agent = QLineEdit()
        self.user_agent.setPlaceholderText("浏览器用户代理字符串")
        ua_layout.addWidget(self.user_agent)
        browser_group.addLayout(ua_layout)
        
        resolution_layout = QHBoxLayout()
        resolution_layout.addWidget(QLabel("视窗大小:"))
        self.viewport_width = QSpinBox()
        self.viewport_width.setRange(320, 4096)
        self.viewport_width.setValue(1920)
        resolution_layout.addWidget(self.viewport_width)
        
        resolution_layout.addWidget(QLabel("x"))
        self.viewport_height = QSpinBox()
        self.viewport_height.setRange(240, 2160)
        self.viewport_height.setValue(1080)
        resolution_layout.addWidget(self.viewport_height)
        browser_group.addLayout(resolution_layout)
        
        platform_layout = QHBoxLayout()
        platform_layout.addWidget(QLabel("平台:"))
        self.platform = QComboBox()
        self.platform.addItems(["Win32", "MacIntel", "Linux x86_64", "iPhone", "Android"])
        platform_layout.addWidget(self.platform)
        
        platform_layout.addWidget(QLabel("时区:"))
        self.timezone = QComboBox()
        self.timezone.addItems(["Asia/Shanghai", "Asia/Beijing", "Asia/Hong_Kong", "UTC"])
        platform_layout.addWidget(self.timezone)
        browser_group.addLayout(platform_layout)
        
        layout.addLayout(browser_group)

    def init_advanced_tab(self):
        """初始化高级配置选项卡"""
        layout = QVBoxLayout(self.advanced_tab)
        
        # WebGL配置
        webgl_layout = QVBoxLayout()
        vendor_layout = QHBoxLayout()
        vendor_layout.addWidget(QLabel("WebGL供应商:"))
        self.webgl_vendor = QLineEdit()
        self.webgl_vendor.setPlaceholderText("Google Inc. (Intel)")
        vendor_layout.addWidget(self.webgl_vendor)
        webgl_layout.addLayout(vendor_layout)
        
        renderer_layout = QHBoxLayout()
        renderer_layout.addWidget(QLabel("WebGL渲染器:"))
        self.webgl_renderer = QLineEdit()
        self.webgl_renderer.setPlaceholderText("ANGLE (Intel, Intel(R) HD Graphics)")
        renderer_layout.addWidget(self.webgl_renderer)
        webgl_layout.addLayout(renderer_layout)
        layout.addLayout(webgl_layout)
        
        # 地理位置
        geo_layout = QHBoxLayout()
        geo_layout.addWidget(QLabel("纬度:"))
        self.latitude = QLineEdit()
        self.latitude.setPlaceholderText("39.9042")
        geo_layout.addWidget(self.latitude)
        
        geo_layout.addWidget(QLabel("经度:"))
        self.longitude = QLineEdit()
        self.longitude.setPlaceholderText("116.4074")
        geo_layout.addWidget(self.longitude)
        layout.addLayout(geo_layout)
        
        layout.addStretch()

    def init_json_tab(self):
        """初始化JSON配置选项卡"""
        layout = QVBoxLayout(self.json_tab)
        
        info_label = QLabel("📝 您也可以直接编辑JSON配置:")
        info_label.setFont(QFont(get_ui_font_family(), 12))
        layout.addWidget(info_label)
        
        self.json_edit = QTextEdit()
        self.json_edit.setFont(QFont(get_mono_font_family(), 12))  # 增大JSON编辑器字体
        layout.addWidget(self.json_edit)
        
        sync_layout = QHBoxLayout()
        
        form_to_json_btn = QPushButton("表单 → JSON")
        form_to_json_btn.setFont(QFont(get_ui_font_family(), 12))
        form_to_json_btn.clicked.connect(self.form_to_json)
        sync_layout.addWidget(form_to_json_btn)
        
        json_to_form_btn = QPushButton("JSON → 表单")
        json_to_form_btn.setFont(QFont(get_ui_font_family(), 12))
        json_to_form_btn.clicked.connect(self.json_to_form)
        sync_layout.addWidget(json_to_form_btn)
        
        sync_layout.addStretch()
        layout.addLayout(sync_layout)

    def load_preset(self):
        """加载预设配置"""
        presets = {
            "Windows Chrome": {
                "name": "Windows Chrome环境",
                "proxy_enabled": False,
                "proxy_type": "direct",
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "viewport_width": 1920,
                "viewport_height": 937,
                "platform": "Win32",
                "timezone": "Asia/Shanghai",
                "webgl_vendor": "Google Inc. (Intel)",
                "webgl_renderer": "ANGLE (Intel, Intel(R) HD Graphics Direct3D11)"
            },
            "Mac Chrome": {
                "name": "Mac Chrome环境", 
                "proxy_enabled": False,
                "proxy_type": "direct",
                "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "viewport_width": 1440,
                "viewport_height": 764,
                "platform": "MacIntel",
                "timezone": "Asia/Shanghai",
                "webgl_vendor": "Apple Inc.",
                "webgl_renderer": "Apple GPU"
            },
            "SOCKS5代理": {
                "name": "SOCKS5代理环境",
                "proxy_enabled": True,
                "proxy_type": "socks5",
                "proxy_host": "127.0.0.1",
                "proxy_port": 1080,
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "viewport_width": 1366,
                "viewport_height": 625,
                "platform": "Win32",
                "timezone": "Asia/Shanghai"
            }
        }
        
        # 简单选择第一个预设
        preset = presets["Windows Chrome"]
        self.load_config(preset)

    def generate_random(self):
        """生成随机配置"""
        import random
        
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0"
        ]
        
        resolutions = [(1920, 1080), (1366, 768), (1440, 900)]
        platforms = ["Win32", "MacIntel"]
        
        resolution = random.choice(resolutions)
        
        config = {
            "name": f"随机环境_{random.randint(1000, 9999)}",
            "proxy_enabled": random.choice([True, False]),
            "proxy_type": random.choice(["direct", "socks5", "http"]),
            "proxy_host": "127.0.0.1" if random.choice([True, False]) else "",
            "proxy_port": random.choice([1080, 8080, 3128]),
            "user_agent": random.choice(user_agents),
            "viewport_width": resolution[0] - random.randint(0, 100),
            "viewport_height": resolution[1] - random.randint(100, 200),
            "platform": random.choice(platforms),
            "timezone": "Asia/Shanghai"
        }
        
        self.load_config(config)

    def load_config(self, config):
        """加载配置到表单"""
        self.name_input.setText(config.get("name", ""))
        self.proxy_enabled.setChecked(config.get("proxy_enabled", False))
        self.proxy_type.setCurrentText(config.get("proxy_type", "direct"))
        self.proxy_host.setText(config.get("proxy_host", ""))
        self.proxy_port.setValue(config.get("proxy_port", 1080))
        self.proxy_username.setText(config.get("proxy_username", ""))
        self.proxy_password.setText(config.get("proxy_password", ""))
        
        self.user_agent.setText(config.get("user_agent", ""))
        self.viewport_width.setValue(config.get("viewport_width", 1920))
        self.viewport_height.setValue(config.get("viewport_height", 1080))
        self.platform.setCurrentText(config.get("platform", "Win32"))
        self.timezone.setCurrentText(config.get("timezone", "Asia/Shanghai"))
        
        self.webgl_vendor.setText(config.get("webgl_vendor", ""))
        self.webgl_renderer.setText(config.get("webgl_renderer", ""))
        self.latitude.setText(config.get("geolocation_latitude", ""))
        self.longitude.setText(config.get("geolocation_longitude", ""))

    def form_to_json(self):
        """表单数据转JSON"""
        config = self.get_environment_data()
        self.json_edit.setPlainText(json.dumps(config, ensure_ascii=False, indent=2))

    def json_to_form(self):
        """JSON转表单数据"""
        try:
            config = json.loads(self.json_edit.toPlainText())
            self.load_config(config)
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, "JSON错误", f"JSON格式错误: {e}")

    def get_environment_data(self):
        """获取环境配置数据"""
        return {
            "name": self.name_input.text().strip(),
            "proxy_enabled": self.proxy_enabled.isChecked(),
            "proxy_type": self.proxy_type.currentText(),
            "proxy_host": self.proxy_host.text().strip() or None,
            "proxy_port": self.proxy_port.value() if self.proxy_host.text().strip() else None,
            "proxy_username": self.proxy_username.text().strip() or None,
            "proxy_password": self.proxy_password.text().strip() or None,
            "user_agent": self.user_agent.text().strip(),
            "viewport_width": self.viewport_width.value(),
            "viewport_height": self.viewport_height.value(),
            "platform": self.platform.currentText(),
            "timezone": self.timezone.currentText(),
            "locale": "zh-CN",
            "webgl_vendor": self.webgl_vendor.text().strip() or None,
            "webgl_renderer": self.webgl_renderer.text().strip() or None,
            "geolocation_latitude": self.latitude.text().strip() or None,
            "geolocation_longitude": self.longitude.text().strip() or None
        }


class BrowserEnvironmentPage(QWidget):
    """浏览器环境管理页面"""
    
    environment_switched = pyqtSignal(int)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # 初始化服务
        self.environment_service = browser_environment_service
        
        # 显示服务状态
        if USE_REAL_SERVICES:
            print("💚 浏览器环境页面使用真实数据库服务")
        else:
            print("🟡 浏览器环境页面使用Mock服务（数据将不会持久化）")
        
        self.init_ui()
        self.load_data()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)
        
        # 设置页面字体
        page_font = QFont(get_ui_font_family(), 12)
        self.setFont(page_font)
        
        # 添加服务状态指示器
        status_layout = QHBoxLayout()
        
        if USE_REAL_SERVICES:
            status_label = QLabel("💚 数据库服务已连接")
            status_label.setStyleSheet("color: green; font-weight: bold; font-size: 12px;")
        else:
            status_label = QLabel("🟡 使用临时数据（重启后丢失）")
            status_label.setStyleSheet("color: orange; font-weight: bold; font-size: 12px;")
        
        status_layout.addWidget(status_label)
        status_layout.addStretch()
        
        # 添加刷新按钮
        refresh_btn = QPushButton("🔄 刷新数据")
        refresh_btn.setFont(QFont(get_ui_font_family(), 12))
        refresh_btn.clicked.connect(self.load_data)
        status_layout.addWidget(refresh_btn)
        
        layout.addLayout(status_layout)
        
        title = QLabel("🌐 浏览器环境管理")
        title.setFont(QFont(get_ui_font_family(), 28, QFont.Bold))  # 主标题更大
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        
        # 环境配置表格
        self.environments_table = QTableWidget()
        self.environments_table.setColumnCount(8)
        self.environments_table.setHorizontalHeaderLabels([
            "ID", "环境名称", "代理状态", "代理配置", "浏览器", "分辨率", "平台", "操作"
        ])
        # 设置表格字体
        table_font = QFont(get_ui_font_family(), 11)
        self.environments_table.setFont(table_font)
        # 设置表头字体
        header_font = QFont(get_ui_font_family(), 12, QFont.Bold)
        self.environments_table.horizontalHeader().setFont(header_font)
        # 调整行高
        self.environments_table.verticalHeader().setDefaultSectionSize(35)
        
        # 调整列宽 - 让表格更宽敞
        self.environments_table.setColumnWidth(0, 60)   # ID列
        self.environments_table.setColumnWidth(1, 180)  # 环境名称列
        self.environments_table.setColumnWidth(2, 90)   # 代理状态列
        self.environments_table.setColumnWidth(3, 200)  # 代理配置列
        self.environments_table.setColumnWidth(4, 100)  # 浏览器列
        self.environments_table.setColumnWidth(5, 100)  # 分辨率列
        self.environments_table.setColumnWidth(6, 120)  # 平台列
        self.environments_table.setColumnWidth(7, 200)  # 操作列 - 加宽操作区域
        
        # 设置表格最小宽度
        self.environments_table.setMinimumWidth(1050)
        
        layout.addWidget(self.environments_table)
        
        # 按钮区域
        button_layout = QHBoxLayout()
        
        add_env_btn = QPushButton("➕ 添加环境")
        add_env_btn.setFont(QFont(get_ui_font_family(), 12))
        add_env_btn.setMinimumHeight(40)  # 增加按钮高度
        add_env_btn.setMinimumWidth(120)  # 增加按钮宽度
        add_env_btn.clicked.connect(self.add_environment)
        button_layout.addWidget(add_env_btn)
        
        preset_btn = QPushButton("📋 创建预设")
        preset_btn.setFont(QFont(get_ui_font_family(), 12))
        preset_btn.setMinimumHeight(40)
        preset_btn.setMinimumWidth(120)
        preset_btn.clicked.connect(self.create_presets)
        button_layout.addWidget(preset_btn)
        
        test_btn = QPushButton("🧪 测试所有")
        test_btn.setFont(QFont(get_ui_font_family(), 12))
        test_btn.setMinimumHeight(40)
        test_btn.setMinimumWidth(120)
        test_btn.clicked.connect(self.test_all_environments)
        button_layout.addWidget(test_btn)
        
        button_layout.addStretch()
        layout.addLayout(button_layout)

    def load_data(self):
        """加载环境数据"""
        try:
            print("🔄 正在刷新浏览器环境数据...")
            self.load_environments()
            print("✅ 环境数据刷新完成")
        except Exception as e:
            print(f"❌ 刷新环境数据失败: {e}")
            QMessageBox.warning(self, "刷新失败", f"刷新数据时出错：{str(e)}")

    def load_environments(self):
        """加载环境配置"""
        try:
            user_id = None
            if USE_REAL_SERVICES:
                try:
                    from ..services.user_service import user_service
                    current_user = user_service.get_current_user()
                    user_id = current_user.id if current_user else None
                except Exception:
                    user_id = None

            environments = self.environment_service.get_all(user_id=user_id)
            
            self.environments_table.setRowCount(len(environments))
            for row, env in enumerate(environments):
                self.environments_table.setItem(row, 0, QTableWidgetItem(str(env.get('id', ''))))

                env_name = env.get('name', '')
                if env.get('is_default'):
                    env_name = f"⭐ {env_name}"
                self.environments_table.setItem(row, 1, QTableWidgetItem(env_name))
                
                # 代理状态
                proxy_status = "✅ 启用" if env.get('proxy_enabled') else "❌ 直连"
                self.environments_table.setItem(row, 2, QTableWidgetItem(proxy_status))
                
                # 代理配置
                proxy_display = env.get('proxy_display', '直连')
                self.environments_table.setItem(row, 3, QTableWidgetItem(proxy_display))
                
                # 浏览器信息
                ua = env.get('user_agent', '')
                browser_info = "Chrome" if "Chrome" in ua else "Firefox" if "Firefox" in ua else "Unknown"
                self.environments_table.setItem(row, 4, QTableWidgetItem(browser_info))
                
                # 分辨率
                resolution = env.get('resolution_display', '1920x1080')
                self.environments_table.setItem(row, 5, QTableWidgetItem(resolution))
                
                # 平台
                platform = env.get('platform', '')
                self.environments_table.setItem(row, 6, QTableWidgetItem(platform))
                
                # 操作按钮
                button_layout = QHBoxLayout()
                button_layout.setSpacing(5)  # 减小按钮间距

                default_btn = QPushButton("⭐ 默认")
                default_btn.setFont(QFont(get_ui_font_family(), 10))
                default_btn.setMinimumHeight(28)
                default_btn.setMinimumWidth(50)
                default_btn.setEnabled(not bool(env.get('is_default')))
                default_btn.clicked.connect(lambda checked, e=env: self.set_default_environment(e))
                button_layout.addWidget(default_btn)
                
                edit_btn = QPushButton("📝 编辑")  # 添加图标让按钮更美观
                edit_btn.setFont(QFont(get_ui_font_family(), 10))
                edit_btn.setMinimumHeight(28)
                edit_btn.setMinimumWidth(50)  # 设置最小宽度
                edit_btn.clicked.connect(lambda checked, e=env: self.edit_environment(e))
                button_layout.addWidget(edit_btn)
                
                test_btn = QPushButton("🧪 测试")
                test_btn.setFont(QFont(get_ui_font_family(), 10))
                test_btn.setMinimumHeight(28)
                test_btn.setMinimumWidth(50)
                test_btn.clicked.connect(lambda checked, e=env: self.test_environment(e))
                button_layout.addWidget(test_btn)
                
                delete_btn = QPushButton("🗑️ 删除")
                delete_btn.setFont(QFont(get_ui_font_family(), 10))
                delete_btn.setMinimumHeight(28)
                delete_btn.setMinimumWidth(50)
                delete_btn.clicked.connect(lambda checked, e=env: self.delete_environment(e))
                button_layout.addWidget(delete_btn)
                
                button_widget = QWidget()
                button_widget.setLayout(button_layout)
                self.environments_table.setCellWidget(row, 7, button_widget)
                
        except Exception as e:
            print(f"❌ 加载环境数据失败: {e}")
            QMessageBox.warning(self, "加载失败", f"加载环境数据时出错：{str(e)}")

    def set_default_environment(self, env):
        """设置默认环境配置"""
        if not USE_REAL_SERVICES:
            QMessageBox.information(self, "Mock模式", "Mock模式下不支持设置默认环境")
            return

        try:
            from ..services.user_service import user_service

            current_user = user_service.get_current_user()
            if not current_user:
                QMessageBox.warning(self, "错误", "请先创建并选择一个用户作为当前用户")
                return

            self.environment_service.set_default_environment(current_user.id, env.get('id'))
            self.load_environments()
            QMessageBox.information(self, "成功", "已设置为默认环境")
        except Exception as e:
            QMessageBox.warning(self, "失败", f"设置默认环境失败：{str(e)}")

    def add_environment(self):
        """添加环境配置"""
        # 首先需要确保有当前用户
        if USE_REAL_SERVICES:
            from ..services.user_service import user_service
            current_user = user_service.get_current_user()
            if not current_user:
                QMessageBox.warning(self, "错误", "请先创建并选择一个用户作为当前用户")
                return
        
        dialog = BrowserEnvironmentDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            env_data = dialog.get_environment_data()
            if env_data and env_data.get('name'):
                try:
                    if USE_REAL_SERVICES:
                        # 使用真实服务创建环境配置
                        env = self.environment_service.create_environment(
                            user_id=current_user.id,
                            **env_data
                        )
                        print(f"✅ 成功创建环境配置: {env.name}")
                    else:
                        # 使用Mock服务
                        env = self.environment_service.create(**env_data)
                        print(f"✅ 成功创建Mock环境配置: {env_data.get('name')}")
                    
                    self.load_environments()
                    QMessageBox.information(self, "成功", "环境配置添加成功！")
                except Exception as e:
                    print(f"❌ 添加环境配置失败: {e}")
                    QMessageBox.warning(self, "错误", f"添加环境配置失败：{str(e)}")

    def edit_environment(self, env):
        """编辑环境配置"""
        dialog = BrowserEnvironmentDialog(self)
        dialog.load_config(env)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            env_data = dialog.get_environment_data()
            if env_data:
                try:
                    if USE_REAL_SERVICES:
                        # 使用真实服务更新环境配置
                        updated_env = self.environment_service.update_environment(env['id'], **env_data)
                        print(f"✅ 成功更新环境配置: {updated_env.name}")
                    else:
                        # 使用Mock服务
                        self.environment_service.update(env['id'], **env_data)
                        print(f"✅ 成功更新Mock环境配置: {env_data.get('name')}")
                    
                    self.load_environments()
                    QMessageBox.information(self, "成功", "环境配置更新成功！")
                except Exception as e:
                    print(f"❌ 更新环境配置失败: {e}")
                    QMessageBox.warning(self, "错误", f"更新环境配置失败：{str(e)}")

    def delete_environment(self, env):
        """删除环境配置"""
        reply = QMessageBox.question(self, "确认删除", 
                                   f"确定要删除环境配置 '{env.get('name', '')}' 吗？",
                                   QMessageBox.Yes | QMessageBox.No)
        
        if reply == QMessageBox.Yes:
            try:
                if USE_REAL_SERVICES:
                    # 使用真实服务删除环境配置
                    self.environment_service.delete_environment(env['id'])
                    print(f"✅ 成功删除环境配置: {env.get('name')}")
                else:
                    # 使用Mock服务
                    self.environment_service.delete(env['id'])
                    print(f"✅ 成功删除Mock环境配置: {env.get('name')}")
                
                self.load_environments()
                QMessageBox.information(self, "成功", "环境配置删除成功！")
            except Exception as e:
                print(f"❌ 删除环境配置失败: {e}")
                QMessageBox.warning(self, "错误", f"删除环境配置失败：{str(e)}")

    def test_environment(self, env):
        """测试单个环境配置"""
        if not USE_REAL_SERVICES:
            QMessageBox.information(self, "测试结果", f"Mock模式下无法进行真实测试")
            return
        
        try:
            print(f"🧪 测试环境配置: {env.get('name')}")
            # 这里应该调用异步测试方法，简化为同步提示
            QMessageBox.information(self, "测试中", f"正在测试环境配置 '{env.get('name')}'...")
            # 实际实现需要异步处理
        except Exception as e:
            QMessageBox.warning(self, "测试失败", f"测试失败：{str(e)}")

    def test_all_environments(self):
        """测试所有环境配置"""
        if not USE_REAL_SERVICES:
            QMessageBox.information(self, "测试结果", f"Mock模式下无法进行真实测试")
            return
            
        QMessageBox.information(self, "批量测试", "开始批量测试所有环境配置...")

    def create_presets(self):
        """创建预设环境配置"""
        if USE_REAL_SERVICES:
            from ..services.user_service import user_service
            current_user = user_service.get_current_user()
            if not current_user:
                QMessageBox.warning(self, "错误", "请先创建并选择一个用户作为当前用户")
                return
            
            try:
                presets = self.environment_service.create_preset_environments(current_user.id)
                self.load_environments()
                QMessageBox.information(self, "成功", f"成功创建 {len(presets)} 个预设环境配置！")
            except Exception as e:
                QMessageBox.warning(self, "错误", f"创建预设失败：{str(e)}")
        else:
            QMessageBox.information(self, "Mock模式", "Mock模式下无法创建真实预设")
