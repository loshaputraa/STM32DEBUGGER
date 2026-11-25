# ============================================================================
# Config - Central device configuration
# ============================================================================
# Single source of truth for ESP32 connection settings. Previously this IP/
# port pair was hardcoded independently in tcp_client.py, FlashDialog.py,
# and main_pyside6.py (as a mock-backend fallback) - three copies that could
# silently drift out of sync. Every module now imports from here instead.
#
# To point the app at a different ESP32 bridge, change the values below.

ESP32_IP =  "10.99.230.117" #"10.235.159.177" #"10.67.19.117" #"10.38.149.156"  #"192.168.0.36"
ESP32_PORT = 5000

# Socket receive timeout (seconds), used by DebugBridge.
#
# The STM32 is silent for ~20-30 s at boot while MPU6050_Init() calibrates
# the gyro (2000 I2C reads). tcp_client.py therefore IGNORES recv() timeouts
# until the first "STM32 alive" arrives, so this value only bounds how long
# the app waits once the STM32 HAS reported alive. The STM32 streams telemetry
# ~every 95 ms and "STM32 alive" once a second, so 30 s of true silence
# post-alive really does mean the link is dead.
SOCKET_TIMEOUT_S = 30