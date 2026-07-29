# ============================================================================
# Data Store - Central storage for variables and events
# ============================================================================
# Stores all STM32 data in memory for access by parser and GUI

from datetime import datetime  # For timestamping entries

# Dictionary to store variables
# Format: {name: {"value": X, "timestamp": T}}
# Example: {"temperature": {"value": 25.5, "timestamp": "2024-01-15 10:30:45"}}
variables = {}

# List to store events with timestamps
# Format: [{"event": "SYSTEM_START", "timestamp": T}, ...]
# Example: {"event": "Button pressed", "timestamp": "2024-01-15 10:30:45"}
events = []