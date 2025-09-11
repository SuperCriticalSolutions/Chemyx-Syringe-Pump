# -*- coding: utf-8 -*-
"""
Custom Chemyx Pump GUI with Step-Based Control
Provides sequential step execution with programmable pump operations
"""

import sys
import time
import logging
import json
import os
from threading import Thread, Event
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QTableWidget, QTableWidgetItem, QPushButton, QComboBox,
    QLineEdit, QLabel, QSpinBox, QDoubleSpinBox, QGroupBox, QMessageBox,
    QHeaderView, QProgressBar, QDialog, QFileDialog
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QDoubleValidator, QColor

from python_dist.CHEMYX_python.core import connect

# Logging Setup
logging.basicConfig()
logger = logging.getLogger('MyChemyxGUI')
logger.setLevel(logging.INFO)

class CachedConnection:
    """
    Wrapper for Connection that caches parameter-setting methods to avoid redundant calls.
    """
    def __init__(self, connection):
        self.connection = connection
        self.cache = {}
        # Methods that should be cached (parameter-setting methods)
        self.cacheable_methods = {
            'setUnits', 'setDiameter', 'setVolume', 'setMode', 'setRate', 
            'setDelay', 'setTime', 'setPump'
        }
    
    def reset_cache(self):
        """Reset the parameter cache (call when connection is reset)"""
        self.cache.clear()
        logger.debug("Connection cache reset")
    
    def _make_cache_key(self, method_name):
        """Create a cache key for tracking parameter state"""
        # Use just method name, optionally with pump number for multipump setups
        cache_key = method_name
        if hasattr(self.connection, 'multipump') and self.connection.multipump:
            cache_key = f"{method_name}_pump_{getattr(self.connection, 'currentPump', 1)}"
        
        return cache_key
    
    def _make_param_value(self, args, kwargs):
        """Create a hashable representation of the parameter values being set"""
        # Convert lists to tuples for hashing
        def make_hashable(item):
            if isinstance(item, list):
                return tuple(item)
            elif isinstance(item, dict):
                return tuple(sorted(item.items()))
            return item
        
        hashable_args = tuple(make_hashable(arg) for arg in args)
        hashable_kwargs = tuple(sorted((k, make_hashable(v)) for k, v in kwargs.items()))
        
        return (hashable_args, hashable_kwargs)
    
    def __getattr__(self, name):
        """Intercept method calls and cache parameter-setting methods"""
        if not hasattr(self.connection, name):
            raise AttributeError(f"'{type(self.connection).__name__}' object has no attribute '{name}'")
        
        original_method = getattr(self.connection, name)
        
        # If it's not a cacheable method, just return the original
        if name not in self.cacheable_methods or not callable(original_method):
            return original_method
        
        def cached_method(*args, **kwargs):
            cache_key = self._make_cache_key(name)
            param_value = self._make_param_value(args, kwargs)
            
            # Check if this parameter is already set to these exact values
            if cache_key in self.cache and self.cache[cache_key] == param_value:
                logger.debug(f"Cache hit for {name}{args}: parameter unchanged")
                return None  # Parameter already set, no need to call hardware
            
            # Parameter value changed or first call - send command to hardware
            logger.debug(f"Cache miss for {name}{args}: parameter changed, calling hardware")
            result = original_method(*args, **kwargs)
            
            # Store the new parameter values (not the return value)
            self.cache[cache_key] = param_value
            return result
        
        return cached_method
    
    def __setattr__(self, name, value):
        """Handle attribute setting - pass through to wrapped connection for non-wrapper attributes"""
        if name in ('connection', 'cache', 'cacheable_methods'):
            super().__setattr__(name, value)
        else:
            setattr(self.connection, name, value)
    
    def __getattribute__(self, name):
        """Handle attribute access - pass through to wrapped connection for non-wrapper attributes"""
        if name in ('connection', 'cache', 'cacheable_methods', 'reset_cache', '_make_cache_key', '_make_param_value'):
            return super().__getattribute__(name)
        elif hasattr(super().__getattribute__('connection'), name):
            return self.__getattr__(name)
        else:
            return super().__getattribute__(name)

class StepExecutor(QObject):
    """Handles step execution in a separate thread"""
    step_changed = pyqtSignal(int)  # Current step index
    execution_finished = pyqtSignal()
    error_occurred = pyqtSignal(str)
    
    def __init__(self, steps, connection, config):
        super().__init__()
        self.steps = steps
        self.connection = connection
        self.config = config
        self.current_step = 0
        self.stop_event = Event()
        self.pause_event = Event()
        self.loop_stack = []
        
    def execute_steps(self):
        """Execute all steps sequentially"""
        try:
            # Initialize pump state at start of execution
            self._initialize_pump_state()
            
            self.current_step = 0
            while self.current_step < len(self.steps) and not self.stop_event.is_set():
                if self.pause_event.is_set():
                    self.pause_event.wait()
                    continue
                    
                step = self.steps[self.current_step]
                self.step_changed.emit(self.current_step)
                
                if not self.execute_single_step(step):
                    break
                    
                self.current_step += 1
                
        except Exception as e:
            self.error_occurred.emit(str(e))
        finally:
            self.execution_finished.emit()
    
    def execute_single_step(self, step):
        """Execute a single step and return True if successful"""
        function = step['function']
        params = step['params']
        
        try:
            if function == 'pump_volume':
                volume = float(params.get('volume', 0))
                rate = float(params.get('rate', 1))
                self._pump_volume(volume, rate)
                
            elif function == 'pump_time':
                duration = float(params.get('time', 1))
                rate = float(params.get('rate', 1))
                self._pump_time(duration, rate)
                
            elif function == 'wait':
                duration = float(params.get('time', 1))
                self._wait(duration)
                
            elif function == 'start_loop':
                iterations = int(params.get('iterations', 1))
                self.loop_stack.append({
                    'start_index': self.current_step,
                    'iterations': iterations,
                    'current_iteration': 0
                })
                
            elif function == 'end_loop':
                if self.loop_stack:
                    loop = self.loop_stack[-1]
                    loop['current_iteration'] += 1
                    if loop['current_iteration'] < loop['iterations']:
                        self.current_step = loop['start_index']
                    else:
                        self.loop_stack.pop()
                        
            return True
            
        except Exception as e:
            self.error_occurred.emit(f"Step {self.current_step + 1}: {str(e)}")
            return False
    
    def _execute_pump_operation(self, volume, rate, wait_for_completion=True):
        """
        Unified method for all pump operations
        
        Args:
            volume: Volume to pump (mL). Positive for withdraw, negative for infuse
            rate: Rate in mL/min. Sign determines direction if volume is unsigned
            wait_for_completion: If True, wait for operation to complete
        """
        self.connection.setUnits('mL/min')
        self.connection.setDiameter(self.config['diameter'])
        
        # Determine mode and volume based on signs
        if volume < 0:
            self.connection.setMode(1)  # Infuse mode
        else:
            self.connection.setMode(0)  # Withdraw mode
            
        self.connection.setVolume(actual_volume)
        self.connection.setRate(abs(rate))
        self.connection.startPump()
        
        if wait_for_completion and actual_volume > 0:
            # Wait for completion based on volume and rate
            wait_time = actual_volume / abs(rate) * 60
            time.sleep(wait_time + 1)

    def _pump_volume(self, volume, rate):
        """Pump a specific volume at given rate"""
        self._execute_pump_operation(volume, rate, wait_for_completion=True)
        
    def _pump_time(self, duration, rate):
        """Pump for specific time at given rate"""
        # Calculate volume based on time and rate
        volume = abs(rate) * duration / 60
        
        # Apply rate direction to volume
        if rate < 0:
            volume = -volume
            
        self._execute_pump_operation(volume, abs(rate), wait_for_completion=False)
        time.sleep(duration + 1)
        
    def _wait(self, duration):
        """Wait for specified time"""
        time.sleep(duration)
    
    def _initialize_pump_state(self):
        """Initialize pump state with common settings at start of execution"""
        # Reset cache to ensure fresh parameter setting at start of execution
        if hasattr(self.connection, 'reset_cache'):
            self.connection.reset_cache()
        
        # Set the common parameters once at the beginning
        self.connection.setUnits('mL/min')
        self.connection.setDiameter(self.config['diameter'])
    
    def stop(self):
        """Stop execution"""
        self.stop_event.set()
        if hasattr(self.connection, 'stopPump'):
            self.connection.stopPump()
    
    def pause(self):
        """Pause execution"""
        self.pause_event.set()
        if hasattr(self.connection, 'pausePump'):
            self.connection.pausePump()
    
    def resume(self):
        """Resume execution"""
        self.pause_event.clear()

class MyChemyxGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('My Chemyx Pump Controller')
        self.setGeometry(100, 100, 900, 700)
        
        # Apply Sunset Glow theme
        self.apply_sunset_glow_theme()
        
        # Initialize connection
        self.connection = None
        self.connected = False
        self.config = {
            'port': 'COM4',
            'baudrate': 38400,
            'diameter': 28.6,
            'max_volume': 20,
            'max_rate': 100
        }
        
        # Step execution
        self.steps = []
        self.executor = None
        self.execution_thread = None
        self.current_step_index = -1
        self.is_running = False
        self.is_paused = False
        self.completed_steps = set()  # Track completed step indices
        
        # Settings files
        self.config_file = "chemyx_config.json"
        self.steps_file = "chemyx_steps.json"
        
        self.init_ui()
        self.setup_connections()
        self.load_settings()
        
    def apply_sunset_glow_theme(self):
        """Apply the Sunset Glow theme styling"""
        style = """
        /* Main Application Styling */
        QMainWindow {
            background-color: #1e1b4b;
            color: #e0e7ff;
            font-family: 'Inter', 'Segoe UI', 'Arial', sans-serif;
            font-size: 9pt;
        }
        
        /* Central Widget and General Widgets */
        QWidget {
            background-color: #1e1b4b;
            color: #e0e7ff;
            font-family: 'Inter', 'Segoe UI', 'Arial', sans-serif;
        }
        
        /* Group Boxes */
        QGroupBox {
            background-color: #312e81;
            border: 1px solid #4338ca;
            border-radius: 8px;
            font-weight: 500;
            font-size: 10pt;
            padding: 8px;
            margin-top: 10px;
        }
        
        QGroupBox::title {
            color: #e0e7ff;
            subcontrol-origin: margin;
            subcontrol-position: top left;
            padding: 0 8px;
            background-color: #312e81;
        }
        
        /* Tab Widget */
        QTabWidget::pane {
            border: 1px solid #4338ca;
            border-radius: 8px;
            background-color: #312e81;
        }
        
        QTabBar::tab {
            background-color: #312e81;
            color: #a5b4fc;
            border: 1px solid #4338ca;
            padding: 8px 16px;
            margin-right: 2px;
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
            font-weight: 500;
        }
        
        QTabBar::tab:selected {
            background-color: #f97316;
            color: #ffffff;
            border-bottom: 1px solid #f97316;
        }
        
        QTabBar::tab:hover:!selected {
            background-color: #4338ca;
            color: #e0e7ff;
        }
        
        /* Primary Buttons */
        QPushButton {
            background-color: #f97316;
            color: #ffffff;
            border: none;
            border-radius: 6px;
            padding: 8px 16px;
            font-weight: 500;
            font-size: 9pt;
            min-height: 20px;
        }
        
        QPushButton:hover {
            background-color: #ea580c;
        }
        
        QPushButton:pressed {
            background-color: #c2410c;
        }
        
        QPushButton:disabled {
            background-color: #4338ca;
            color: #a5b4fc;
        }
        
        /* Secondary Buttons - for Edit, Move, Save/Load */
        QPushButton[class="secondary"] {
            background-color: #1e1b4b;
            color: #e0e7ff;
            border: 1px solid #4338ca;
            padding: 4px 12px;
            max-height: 28px;
        }
        
        QPushButton[class="secondary"]:hover {
            background-color: #4338ca;
            border-color: #f97316;
        }
        
        /* Small buttons - for up/down arrows */
        QPushButton[class="small"] {
            background-color: #1e1b4b;
            color: #e0e7ff;
            border: 1px solid #4338ca;
            min-width: 50px;
            max-width: 60px;
            max-height: 28px;
            padding: 2px;
            font-size: 12pt;
        }
        
        QPushButton[class="small"]:hover {
            background-color: #4338ca;
        }
        
        /* Connect/Disconnect button special styling */
        QPushButton#connectBtn {
            background-color: #f97316;
            font-weight: 600;
        }
        
        /* Input Fields */
        QLineEdit, QDoubleSpinBox, QSpinBox {
            background-color: #1e1b4b;
            color: #e0e7ff;
            border: 1px solid #4338ca;
            border-radius: 6px;
            padding: 6px 8px;
            selection-background-color: #f97316;
        }
        
        QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus {
            border-color: #f97316;
            outline: none;
        }
        
        /* Combo Boxes */
        QComboBox {
            background-color: #1e1b4b;
            color: #e0e7ff;
            border: 1px solid #4338ca;
            border-radius: 6px;
            padding: 6px 8px;
            min-width: 100px;
        }
        
        QComboBox:focus {
            border-color: #f97316;
        }
        
        QComboBox::drop-down {
            border: none;
            width: 20px;
        }
        
        QComboBox::down-arrow {
            color: #e0e7ff;
            width: 8px;
            height: 8px;
        }
        
        QComboBox QAbstractItemView {
            background-color: #1e1b4b;
            color: #e0e7ff;
            border: 1px solid #4338ca;
            border-radius: 6px;
            selection-background-color: #f97316;
        }
        
        /* Labels */
        QLabel {
            color: #e0e7ff;
            font-weight: 400;
        }
        
        /* Status Labels */
        QLabel#statusConnected {
            color: #10b981;
            font-weight: 700;
        }
        
        QLabel#statusDisconnected {
            color: #f43f5e;
            font-weight: 700;
        }
        
        /* Table Widget */
        QTableWidget {
            background-color: #312e81;
            color: #e0e7ff;
            border: 1px solid #4338ca;
            border-radius: 6px;
            gridline-color: #4338ca;
            selection-background-color: #f97316;
            show-decoration-selected: 1;
        }
        
        QTableWidget::item {
            padding: 0px;
            border: none;
            min-height: 35px;
        }
        
        QTableWidget QWidget {
            text-align: center;
            border: none;
            background-color: transparent;
        }
        
        QTableWidget::item:selected {
            background-color: #f97316;
            color: #ffffff;
        }
        
        /* Selected row highlighting */
        QTableWidget::item:selected:active {
            background-color: #f97316;
            color: #ffffff;
        }
        
        QTableWidget::item:selected:!active {
            background-color: #f97316;
            color: #ffffff;
        }
        
        QHeaderView::section {
            background-color: #1e1b4b;
            color: #e0e7ff;
            border: 1px solid #4338ca;
            padding: 6px 8px;
            font-weight: 600;
        }
        
        /* Progress Bar */
        QProgressBar {
            background-color: #312e81;
            color: #e0e7ff;
            border: 1px solid #4338ca;
            border-radius: 6px;
            text-align: center;
        }
        
        QProgressBar::chunk {
            background-color: #f97316;
            border-radius: 4px;
        }
        
        /* Form Layout Styling */
        QFormLayout QLabel {
            font-weight: 500;
            color: #e0e7ff;
        }
        
        /* Scrollbar */
        QScrollBar:vertical {
            background-color: #312e81;
            width: 12px;
            border-radius: 6px;
        }
        
        QScrollBar::handle:vertical {
            background-color: #4338ca;
            border-radius: 6px;
            min-height: 20px;
        }
        
        QScrollBar::handle:vertical:hover {
            background-color: #f97316;
        }
        """
        
        self.setStyleSheet(style)
        
    def init_ui(self):
        """Initialize the user interface"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        layout = QVBoxLayout(central_widget)
        
        # Create tab widget
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        
        # Create tabs
        self.create_program_tab()
        self.create_config_tab()
        
    def create_program_tab(self):
        """Create the main programming tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        # Connection status
        conn_group = QGroupBox("Connection")
        conn_layout = QHBoxLayout(conn_group)
        
        self.connect_btn = QPushButton("🔌 Connect")
        self.connect_btn.setObjectName("connectBtn")
        self.status_label = QLabel("DISCONNECTED")
        self.status_label.setObjectName("statusDisconnected")
        
        conn_layout.addWidget(QLabel("Status:"))
        conn_layout.addWidget(self.status_label)
        conn_layout.addStretch()
        conn_layout.addWidget(self.connect_btn)
        
        layout.addWidget(conn_group)
        
        # Steps table
        steps_group = QGroupBox("Program Steps")
        steps_layout = QVBoxLayout(steps_group)
        
        # Save/Load buttons
        file_layout = QHBoxLayout()
        self.save_btn = QPushButton("💾 Save Program")
        self.save_btn.setProperty("class", "secondary")
        self.load_btn = QPushButton("📁 Load Program")
        self.load_btn.setProperty("class", "secondary")
        file_layout.addWidget(self.save_btn)
        file_layout.addWidget(self.load_btn)
        file_layout.addStretch()
        steps_layout.addLayout(file_layout)
        
        # Table
        self.steps_table = QTableWidget(0, 6)
        self.steps_table.setHorizontalHeaderLabels(["#", "Function", "Parameters", "Edit", "↑", "↓"])
        
        # Configure selection behavior for row highlighting
        self.steps_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.steps_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.steps_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Keep selection visible even when not focused
        
        header = self.steps_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.steps_table.setColumnWidth(0, 40)
        self.steps_table.setColumnWidth(1, 120)
        self.steps_table.setColumnWidth(3, 80)
        self.steps_table.setColumnWidth(4, 45)
        self.steps_table.setColumnWidth(5, 45)
        
        steps_layout.addWidget(self.steps_table)
        
        # Hide row numbers and set row height
        self.steps_table.verticalHeader().setVisible(False)
        self.steps_table.verticalHeader().setDefaultSectionSize(40)
        
        # Add step controls
        add_layout = QHBoxLayout()
        self.function_combo = QComboBox()
        self.function_combo.addItems([
            "pump_volume", "pump_time", "wait", "start_loop", "end_loop"
        ])
        
        self.add_btn = QPushButton("➕ Add Step")
        self.remove_btn = QPushButton("➖ Remove Step")
        
        add_layout.addWidget(QLabel("Function:"))
        add_layout.addWidget(self.function_combo)
        add_layout.addWidget(self.add_btn)
        add_layout.addWidget(self.remove_btn)
        add_layout.addStretch()
        
        steps_layout.addLayout(add_layout)
        layout.addWidget(steps_group)
        
        # Control buttons - split into two groups
        control_main_group = QGroupBox("Execution Control")
        control_main_layout = QHBoxLayout(control_main_group)
        
        # Left section - Playback controls
        playback_group = QGroupBox("Playback")
        playback_layout = QHBoxLayout(playback_group)
        
        self.play_btn = QPushButton("▶ Play")
        self.pause_btn = QPushButton("⏸ Pause")
        self.stop_btn = QPushButton("⏹ Stop")
        self.step_btn = QPushButton("⏭ Single Step")
        
        playback_layout.addWidget(self.play_btn)
        playback_layout.addWidget(self.pause_btn)
        playback_layout.addWidget(self.stop_btn)
        playback_layout.addWidget(self.step_btn)
        playback_layout.addStretch()
        
        # Right section - Jog controls
        jog_group = QGroupBox("Jog Controls")
        jog_layout = QHBoxLayout(jog_group)
        
        self.jog_fill_btn = QPushButton("🔼 Jog Fill")
        self.jog_empty_btn = QPushButton("🔽 Jog Empty")
        self.jog_rate_spinbox = QDoubleSpinBox()
        self.jog_rate_spinbox.setRange(0.1, 50)
        self.jog_rate_spinbox.setValue(5)
        self.jog_rate_spinbox.setSuffix(" mL/min")
        
        # Set jog buttons to work while pressed
        self.jog_fill_btn.pressed.connect(lambda: self.start_jog(True))
        self.jog_fill_btn.released.connect(self.stop_jog)
        self.jog_empty_btn.pressed.connect(lambda: self.start_jog(False))
        self.jog_empty_btn.released.connect(self.stop_jog)
        
        jog_layout.addStretch()
        jog_layout.addWidget(QLabel("Rate:"))
        jog_layout.addWidget(self.jog_rate_spinbox)
        jog_layout.addWidget(self.jog_fill_btn)
        jog_layout.addWidget(self.jog_empty_btn)
        
        # Add both groups to main control layout
        control_main_layout.addWidget(playback_group)
        control_main_layout.addWidget(jog_group)
        
        # Execution status display
        self.execution_status_label = QLabel("Ready")
        self.execution_status_label.setStyleSheet("color: #e0e7ff; font-weight: bold; padding: 8px;")
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Step %v%")
        self.progress_bar.setVisible(False)
        
        layout.addWidget(control_main_group)
        layout.addWidget(self.execution_status_label)
        layout.addWidget(self.progress_bar)
        
        self.tabs.addTab(tab, "Program")
        
    def create_config_tab(self):
        """Create configuration tab"""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        config_group = QGroupBox("Pump Configuration")
        config_layout = QFormLayout(config_group)
        
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.baudrate_combo = QComboBox()
        self.baudrate_combo.addItems(['9600', '14400', '19200', '38400', '57600', '115200'])
        self.baudrate_combo.setCurrentText('38400')
        
        self.diameter_edit = QDoubleSpinBox()
        self.diameter_edit.setRange(0.1, 100.0)
        self.diameter_edit.setValue(28.6)
        self.diameter_edit.setSuffix(" mm")
        
        self.max_volume_edit = QDoubleSpinBox()
        self.max_volume_edit.setRange(0.1, 1000.0)
        self.max_volume_edit.setValue(20.0)
        self.max_volume_edit.setSuffix(" mL")
        
        self.max_rate_edit = QDoubleSpinBox()
        self.max_rate_edit.setRange(0.1, 1000.0)
        self.max_rate_edit.setValue(100.0)
        self.max_rate_edit.setSuffix(" mL/min")
        
        config_layout.addRow("Serial Port:", self.port_combo)
        config_layout.addRow("Baud Rate:", self.baudrate_combo)
        config_layout.addRow("Syringe Diameter:", self.diameter_edit)
        config_layout.addRow("Max Volume:", self.max_volume_edit)
        config_layout.addRow("Max Rate:", self.max_rate_edit)
        
        scan_btn = QPushButton("🔍 Scan Ports")
        scan_btn.clicked.connect(self.scan_ports)
        config_layout.addRow("", scan_btn)
        
        layout.addWidget(config_group)
        layout.addStretch()
        
        self.tabs.addTab(tab, "Configuration")
        
        
    def setup_connections(self):
        """Setup signal connections"""
        self.connect_btn.clicked.connect(self.toggle_connection)
        self.add_btn.clicked.connect(self.add_step)
        self.remove_btn.clicked.connect(self.remove_step)
        
        self.play_btn.clicked.connect(self.play_program)
        self.pause_btn.clicked.connect(self.pause_program)
        self.stop_btn.clicked.connect(self.stop_program)
        self.step_btn.clicked.connect(self.single_step)
        
        # Save/Load connections
        self.save_btn.clicked.connect(self.save_program)
        self.load_btn.clicked.connect(self.load_program)
        
        # Update config when values change
        self.port_combo.currentTextChanged.connect(self.update_and_save_config)
        self.baudrate_combo.currentTextChanged.connect(self.update_and_save_config)
        self.diameter_edit.valueChanged.connect(self.update_and_save_config)
        self.max_volume_edit.valueChanged.connect(self.update_and_save_config)
        self.max_rate_edit.valueChanged.connect(self.update_and_save_config)
        
        
        # Initialize ports
        self.scan_ports()
        
    def scan_ports(self):
        """Scan for available ports"""
        try:
            ports = connect.getOpenPorts()
            self.port_combo.clear()
            self.port_combo.addItems(ports)
        except Exception as e:
            logger.warning(f"Failed to scan ports: {e}")
            
    def update_and_save_config(self):
        """Update configuration from UI and save to file"""
        self.config.update({
            'port': self.port_combo.currentText(),
            'baudrate': int(self.baudrate_combo.currentText()),
            'diameter': self.diameter_edit.value(),
            'max_volume': self.max_volume_edit.value(),
            'max_rate': self.max_rate_edit.value()
        })
        self.save_config()
        
    def toggle_connection(self):
        """Toggle pump connection"""
        if not self.connected:
            try:
                raw_connection = connect.Connection(
                    port=self.config['port'],
                    baudrate=self.config['baudrate']
                )
                raw_connection.openConnection()
                
                # Wrap with caching functionality
                self.connection = CachedConnection(raw_connection)
                
                self.connected = True
                self.status_label.setText("CONNECTED")
                self.status_label.setObjectName("statusConnected")
                self.status_label.setStyleSheet("")  # Clear to use theme styles
                self.status_label.style().unpolish(self.status_label)
                self.status_label.style().polish(self.status_label)
                self.connect_btn.setText("🔌 Disconnect")
                
                
                logger.info("Connected to pump with caching enabled")
                
            except Exception as e:
                QMessageBox.critical(self, "Connection Error", f"Failed to connect: {str(e)}")
                
        else:
            try:
                if self.connection:
                    # Access the underlying connection for closing
                    if hasattr(self.connection, 'connection'):
                        self.connection.connection.closeConnection()
                    else:
                        self.connection.closeConnection()
                    # Reset cache when disconnecting
                    if hasattr(self.connection, 'reset_cache'):
                        self.connection.reset_cache()
                        
                self.connected = False
                self.status_label.setText("DISCONNECTED")
                self.status_label.setObjectName("statusDisconnected")
                self.status_label.setStyleSheet("")  # Clear to use theme styles
                self.status_label.style().unpolish(self.status_label)
                self.status_label.style().polish(self.status_label)
                self.connect_btn.setText("🔌 Connect")
                
                
                logger.info("Disconnected from pump")
                
            except Exception as e:
                logger.warning(f"Error disconnecting: {e}")
                
    def add_step(self):
        """Add a new step to the program"""
        function = self.function_combo.currentText()
        dialog = StepParameterDialog(function, self)
        
        if dialog.exec() == QMessageBox.DialogCode.Accepted:
            params = dialog.get_parameters()
            step = {'function': function, 'params': params}
            self.steps.append(step)
            self.update_steps_table()
            self.auto_save_steps()
            
    def remove_step(self):
        """Remove selected step"""
        current_row = self.steps_table.currentRow()
        if current_row >= 0 and current_row < len(self.steps):
            self.steps.pop(current_row)
            self.update_steps_table()
            self.auto_save_steps()
            
    def update_steps_table(self):
        """Update the steps table display"""
        self.steps_table.setRowCount(len(self.steps))
        
        for i, step in enumerate(self.steps):
            # Step number
            self.steps_table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            
            # Function name
            self.steps_table.setItem(i, 1, QTableWidgetItem(step['function']))
            
            # Parameters
            param_str = ", ".join([f"{k}={v}" for k, v in step['params'].items()])
            self.steps_table.setItem(i, 2, QTableWidgetItem(param_str))
            
            # Edit button
            edit_btn = QPushButton("✏️ Edit")
            edit_btn.setProperty("class", "secondary")
            edit_btn.clicked.connect(lambda checked, row=i: self.edit_step(row))
            edit_btn.setFixedSize(75, 35)
            edit_widget = QWidget()
            edit_layout = QHBoxLayout(edit_widget)
            edit_layout.addWidget(edit_btn)
            edit_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            edit_layout.setContentsMargins(2, 2, 2, 2)
            edit_layout.setSpacing(0)
            self.steps_table.setCellWidget(i, 3, edit_widget)
            
            # Move up button
            up_btn = QPushButton("🔼")
            up_btn.setProperty("class", "small")
            up_btn.clicked.connect(lambda checked, row=i: self.move_step_up(row))
            up_btn.setEnabled(i > 0)
            up_btn.setFixedSize(32, 35)
            up_widget = QWidget()
            up_layout = QHBoxLayout(up_widget)
            up_layout.addWidget(up_btn)
            up_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            up_layout.setContentsMargins(2, 2, 2, 2)
            up_layout.setSpacing(0)
            self.steps_table.setCellWidget(i, 4, up_widget)
            
            # Move down button
            down_btn = QPushButton("🔽")
            down_btn.setProperty("class", "small")
            down_btn.clicked.connect(lambda checked, row=i: self.move_step_down(row))
            down_btn.setEnabled(i < len(self.steps) - 1)
            down_btn.setFixedSize(32, 35)
            down_widget = QWidget()
            down_layout = QHBoxLayout(down_widget)
            down_layout.addWidget(down_btn)
            down_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            down_layout.setContentsMargins(2, 2, 2, 2)
            down_layout.setSpacing(0)
            self.steps_table.setCellWidget(i, 5, down_widget)
            
    def highlight_current_step(self, step_index):
        """Highlight the currently executing step using row selection"""
        # Mark previous step as completed if we're moving forward
        if hasattr(self, 'current_step_index') and self.current_step_index >= 0:
            if step_index > self.current_step_index:
                self.completed_steps.add(self.current_step_index)
        
        self.current_step_index = step_index
        
        # Clear selection and update row colors
        self.steps_table.clearSelection()
        self._update_row_colors()
        
        # Select current step row
        if 0 <= step_index < self.steps_table.rowCount():
            self.steps_table.selectRow(step_index)
            
            # Update status text
            step = self.steps[step_index]
            function_name = step['function']
            params = step['params']
            total_steps = len(self.steps)
            
            # Format parameter display
            param_text = ", ".join([f"{k}={v}" for k, v in params.items()])
            
            status_text = f"Executing Step {step_index + 1} of {total_steps}: {function_name}"
            if param_text:
                status_text += f" ({param_text})"
            
            self.execution_status_label.setText(status_text)
            
            # Update progress bar
            progress_percentage = int((step_index + 1) / total_steps * 100)
            self.progress_bar.setValue(progress_percentage)
            self.progress_bar.setFormat(f"Step {step_index + 1} of {total_steps} ({progress_percentage}%)")
            
        else:
            # Clear status when no step is active
            if step_index == -1:
                self.execution_status_label.setText("Ready")
                self.progress_bar.setValue(0)
                self.progress_bar.setFormat("Step %v%")
                self.completed_steps.clear()  # Clear completed steps when resetting
            else:
                total_steps = len(self.steps)
                self.execution_status_label.setText("Execution Complete")
                self.progress_bar.setValue(100)
                self.progress_bar.setFormat(f"Complete: {total_steps} steps (100%)")
                # Mark all steps as completed
                for i in range(total_steps):
                    self.completed_steps.add(i)
                self._update_row_colors()
    
    def _update_row_colors(self):
        """Update row background colors for completed steps"""
        for i in range(self.steps_table.rowCount()):
            if i in self.completed_steps:
                # Set completed step color for items only (not widgets)
                for j in [0, 1, 2]:  # Only columns with QTableWidgetItems
                    item = self.steps_table.item(i, j)
                    if item:
                        item.setBackground(QColor(34, 197, 94, 80))  # Semi-transparent green
            else:
                # Clear to default theme color
                for j in [0, 1, 2]:  # Only columns with QTableWidgetItems
                    item = self.steps_table.item(i, j)
                    if item:
                        item.setBackground(QColor(49, 46, 129))  # Theme table background
                    
    def play_program(self):
        """Start program execution"""
        if not self.connected:
            QMessageBox.warning(self, "Not Connected", "Please connect to pump first")
            return
            
        if not self.steps:
            QMessageBox.warning(self, "No Steps", "Please add steps to the program")
            return
            
        if not self.is_running:
            self.is_running = True
            self.is_paused = False
            
            self.executor = StepExecutor(self.steps, self.connection, self.config)
            self.executor.step_changed.connect(self.highlight_current_step)
            self.executor.execution_finished.connect(self.execution_finished)
            self.executor.error_occurred.connect(self.execution_error)
            
            self.execution_thread = Thread(target=self.executor.execute_steps)
            self.execution_thread.start()
            
            self.play_btn.setText("▶ Resume")
            self.progress_bar.setVisible(True)
            
    def pause_program(self):
        """Pause program execution"""
        if self.is_running and self.executor:
            if not self.is_paused:
                self.executor.pause()
                self.is_paused = True
                self.pause_btn.setText("▶ Resume")
            else:
                self.executor.resume()
                self.is_paused = False
                self.pause_btn.setText("⏸ Pause")
                
    def stop_program(self):
        """Stop program execution"""
        if self.is_running and self.executor:
            self.executor.stop()
            
    def single_step(self):
        """Execute a single step"""
        if not self.connected:
            QMessageBox.warning(self, "Not Connected", "Please connect to pump first")
            return
            
        current_row = self.steps_table.currentRow()
        if 0 <= current_row < len(self.steps):
            step = self.steps[current_row]
            self.highlight_current_step(current_row)
            
            executor = StepExecutor([step], self.connection, self.config)
            try:
                executor.execute_single_step(step)
            except Exception as e:
                QMessageBox.critical(self, "Execution Error", str(e))
                
    def execution_finished(self):
        """Handle execution completion"""
        self.is_running = False
        self.is_paused = False
        self.play_btn.setText("▶ Play")
        self.pause_btn.setText("⏸ Pause")
        self.progress_bar.setVisible(False)
        self.highlight_current_step(-1)  # Clear highlighting
        
    def execution_error(self, error_message):
        """Handle execution error"""
        QMessageBox.critical(self, "Execution Error", error_message)
        self.stop_program()
        
    def start_jog(self, fill_direction):
        """Start jogging the pump"""
        if not self.connected:
            return
            
        try:
            rate = self.jog_rate_spinbox.value()
            # Use large volume for continuous jogging
            # Positive volume for withdraw (fill direction = False)
            # Negative volume for infuse (fill direction = True) 
            volume = -50 if fill_direction else 50
            
            # Create a temporary executor to use the unified method
            executor = StepExecutor([], self.connection, self.config)
            executor._execute_pump_operation(volume, rate, wait_for_completion=False)
            
        except Exception as e:
            logger.error(f"Jog error: {e}")
            
    def stop_jog(self):
        """Stop jogging the pump"""
        if self.connected and self.connection:
            try:
                self.connection.stopPump()
            except Exception as e:
                logger.error(f"Stop jog error: {e}")
                
    def edit_step(self, row):
        """Edit a step's parameters"""
        if 0 <= row < len(self.steps):
            step = self.steps[row]
            dialog = StepParameterDialog(step['function'], self, step['params'])
            
            if dialog.exec() == QMessageBox.DialogCode.Accepted:
                step['params'] = dialog.get_parameters()
                self.update_steps_table()
                self.auto_save_steps()
                
    def move_step_up(self, row):
        """Move step up in the list"""
        if row > 0 and row < len(self.steps):
            self.steps[row], self.steps[row-1] = self.steps[row-1], self.steps[row]
            self.update_steps_table()
            self.auto_save_steps()
            
    def move_step_down(self, row):
        """Move step down in the list"""
        if row >= 0 and row < len(self.steps) - 1:
            self.steps[row], self.steps[row+1] = self.steps[row+1], self.steps[row]
            self.update_steps_table()
            self.auto_save_steps()
            
    def save_program(self):
        """Save program steps to file"""
        try:
            # Get directory of steps file for initial directory
            initial_dir = os.path.dirname(os.path.abspath(self.steps_file))
            file_path, _ = QFileDialog.getSaveFileName(
                self, "Save Program", initial_dir, "JSON Files (*.json);;All Files (*)"
            )
            if file_path:
                with open(file_path, 'w') as f:
                    json.dump(self.steps, f, indent=2)
                QMessageBox.information(self, "Success", "Program saved successfully!")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save program: {str(e)}")
            
    def load_program(self):
        """Load program steps from file"""
        try:
            # Get directory of steps file for initial directory
            initial_dir = os.path.dirname(os.path.abspath(self.steps_file))
            file_path, _ = QFileDialog.getOpenFileName(
                self, "Load Program", initial_dir, "JSON Files (*.json);;All Files (*)"
            )
            if file_path:
                with open(file_path, 'r') as f:
                    self.steps = json.load(f)
                self.update_steps_table()
                self.auto_save_steps()  # Auto-save the loaded steps
                QMessageBox.information(self, "Success", "Program loaded successfully!")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load program: {str(e)}")
            
    def auto_save_steps(self):
        """Auto-save program steps to default file"""
        try:
            with open(self.steps_file, 'w') as f:
                json.dump(self.steps, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to auto-save steps: {e}")
            
    def auto_load_steps(self):
        """Auto-load program steps from default file"""
        try:
            if os.path.exists(self.steps_file):
                with open(self.steps_file, 'r') as f:
                    self.steps = json.load(f)
                self.update_steps_table()
        except Exception as e:
            logger.warning(f"Failed to auto-load steps: {e}")
            
    def save_config(self):
        """Save configuration to file"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save config: {e}")
            
    def load_config(self):
        """Load configuration from file"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    loaded_config = json.load(f)
                self.config.update(loaded_config)
                
                # Update UI
                self.port_combo.setCurrentText(str(self.config['port']))
                self.baudrate_combo.setCurrentText(str(self.config['baudrate']))
                self.diameter_edit.setValue(self.config['diameter'])
                self.max_volume_edit.setValue(self.config['max_volume'])
                self.max_rate_edit.setValue(self.config['max_rate'])
                
        except Exception as e:
            logger.warning(f"Failed to load config: {e}")
            
            
    def load_settings(self):
        """Load all settings from files"""
        self.load_config()
        self.auto_load_steps()  # Auto-load program steps
    
            
    def closeEvent(self, event):
        """Handle application close"""
        try:
            if self.connected and self.connection:
                self.connection.closeConnection()
        except Exception as e:
            logger.warning(f"Error closing connection: {e}")
        event.accept()

class StepParameterDialog(QDialog):
    """Dialog for entering step parameters"""
    
    def __init__(self, function, parent=None, existing_params=None):
        super().__init__(parent)
        self.function = function
        self.existing_params = existing_params or {}
        self.setWindowTitle(f"Parameters for {function}")
        self.setModal(True)
        self.setup_ui()
        
    def setup_ui(self):
        """Setup the parameter input UI"""
        layout = QVBoxLayout(self)
        
        form_layout = QFormLayout()
        self.param_widgets = {}
        
        if self.function == 'pump_volume':
            self.param_widgets['volume'] = QDoubleSpinBox()
            self.param_widgets['volume'].setRange(-1000, 1000)
            self.param_widgets['volume'].setSuffix(" mL")
            self.param_widgets['volume'].setValue(self.existing_params.get('volume', 0))
            form_layout.addRow("Volume:", self.param_widgets['volume'])
            
            self.param_widgets['rate'] = QDoubleSpinBox()
            self.param_widgets['rate'].setRange(0.1, 1000)
            self.param_widgets['rate'].setValue(self.existing_params.get('rate', 10))
            self.param_widgets['rate'].setSuffix(" mL/min")
            form_layout.addRow("Rate:", self.param_widgets['rate'])
            
        elif self.function == 'pump_time':
            self.param_widgets['time'] = QDoubleSpinBox()
            self.param_widgets['time'].setRange(0.1, 10000)
            self.param_widgets['time'].setValue(self.existing_params.get('time', 1))
            self.param_widgets['time'].setSuffix(" sec")
            form_layout.addRow("Time:", self.param_widgets['time'])
            
            self.param_widgets['rate'] = QDoubleSpinBox()
            self.param_widgets['rate'].setRange(-1000, 1000)
            self.param_widgets['rate'].setValue(self.existing_params.get('rate', 10))
            self.param_widgets['rate'].setSuffix(" mL/min")
            form_layout.addRow("Rate:", self.param_widgets['rate'])
            
        elif self.function == 'wait':
            self.param_widgets['time'] = QDoubleSpinBox()
            self.param_widgets['time'].setRange(0.1, 10000)
            self.param_widgets['time'].setValue(self.existing_params.get('time', 1))
            self.param_widgets['time'].setSuffix(" sec")
            form_layout.addRow("Wait Time:", self.param_widgets['time'])
            
        elif self.function == 'start_loop':
            self.param_widgets['iterations'] = QSpinBox()
            self.param_widgets['iterations'].setRange(1, 1000)
            self.param_widgets['iterations'].setValue(self.existing_params.get('iterations', 2))
            form_layout.addRow("Iterations:", self.param_widgets['iterations'])
            
        layout.addLayout(form_layout)
        
        # OK/Cancel buttons
        button_layout = QHBoxLayout()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        
        button_layout.addStretch()
        button_layout.addWidget(ok_btn)
        button_layout.addWidget(cancel_btn)
        
        layout.addLayout(button_layout)
        
    def get_parameters(self):
        """Get parameter values as dictionary"""
        params = {}
        for name, widget in self.param_widgets.items():
            params[name] = widget.value()
        return params

if __name__ == '__main__':
    app = QApplication(sys.argv)
    gui = MyChemyxGUI()
    gui.show()
    sys.exit(app.exec())
