# Chemyx Syringe Pump Controller Setup Guide

This guide will walk you through setting up the Chemyx Syringe Pump Controller on Windows so you can run it by double-clicking a desktop shortcut.

## Prerequisites

### Step 1: Install Python
1. Download Python 3.8 or newer from https://www.python.org/downloads/
2. **IMPORTANT**: During installation, check the box "Add Python to PATH"
3. Complete the installation

### Step 2: Install Git
1. Download Git from https://git-scm.com/downloads/win
2. Install with default settings
3. Restart your computer after installation

## Installation

### Step 3: Clone the Repository
1. Open Command Prompt (press `Win + R`, type `cmd`, press Enter)
2. Navigate to where you want to install the application (e.g., your Documents folder):
   ```
   cd %USERPROFILE%\Documents
   ```
3. Clone the repository:
   ```
   git clone https://github.com/SuperCriticalSolutions/Chemyx-Syringe-Pump.git
   ```
4. Navigate into the project folder:
   ```
   cd Chemyx-Syringe-Pump
   ```

### Step 4: Install Python Dependencies
1. In the same Command Prompt window (still in the project folder), install the required packages:
   ```
   pip install -r requirements.txt
   ```

## Running the Application

### Step 5: Run the Application
1. Double-click the `run_chemyx.bat` file in the project folder to launch the application
2. If you see any error messages, check that all dependencies are installed correctly

### Step 6: Create Desktop Shortcut
1. Right-click on `run_chemyx.bat` in the project folder and select "Create shortcut"
2. Move the shortcut to your Desktop
3. Rename it to "Chemyx Pump Controller"

## Hardware Setup

### Step 7: Connect Your Chemyx Pump
1. Connect your Chemyx syringe pump to your computer via USB or serial cable
2. Note the COM port number (you can find this in Device Manager)
3. Launch the application and go to the "Configuration" tab
4. Select the correct COM port and configure your syringe diameter
5. Click "Connect" to establish communication with the pump

## Configuration Files

The application will automatically create and save configuration files:
- `chemyx_config.json` - Stores pump connection and syringe settings
- `chemyx_steps.json` - Stores your programmed pump sequences

These files will be created in the same folder as the application.

## Troubleshooting

### Common Issues:
- **"Python is not recognized"**: Python wasn't added to PATH during installation. Reinstall Python and check "Add Python to PATH"
- **Import errors**: Missing dependencies. Run the pip install commands again
- **COM port issues**: Check Device Manager to confirm the correct COM port for your pump
- **Permission errors**: Run Command Prompt as Administrator

### Getting Help:
- Check that your pump is properly connected and drivers are installed
- Verify the COM port settings match your pump's configuration
- Ensure your syringe diameter is correctly set in the Configuration tab

## Features

Once set up, you can:
- Program complex pump sequences with volume and time-based operations
- Create loops for repetitive operations
- Save and load pump programs
- Use jog controls for manual pump operation
- Monitor execution progress with visual feedback

---

*Note: This application requires a compatible Chemyx syringe pump with serial/USB communication capabilities.*