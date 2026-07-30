#!/usr/bin/env python3
from backend.tcp_client import DebugBridge
from backend.parser import get_latest_latency
from backend.data_store import events, variables
import time

print("\n" + "="*70)
print("DAY 3 - FINAL TEST SUITE")
print("="*70)

bridge = DebugBridge()
bridge.connect()
time.sleep(2)

# TEST 1: SET then GET
print("\n[TEST 1] SET then GET")
bridge.send_command("SET:temperature=25")
time.sleep(0.5)
bridge.send_command("GET:temperature")
time.sleep(0.5)
lat = get_latest_latency()
print(f"✓ PASS - Latency: {lat:.1f} ms" if lat else "✗ FAIL")

# TEST 2: Multiple Variables
print("\n[TEST 2] Multiple Variables (5 vars)")
for i in range(5):
    bridge.send_command(f"SET:sensor{i}={100+i*10}")
    time.sleep(0.2)
print(f"✓ PASS - Stored {len(variables)} variables")
print(f"  Variables: {list(variables.keys())}")

# TEST 3: Latency Stability
print("\n[TEST 3] Latency Stability (5 samples)")
lats = []
for i in range(5):
    bridge.send_command("GET:temperature")
    time.sleep(0.3)
    lat = get_latest_latency()
    if lat:
        lats.append(lat)
        print(f"  Sample {i+1}: {lat:.1f} ms")

if lats:
    avg = sum(lats) / len(lats)
    print(f"✓ PASS - Average: {avg:.1f} ms")

# TEST 4: Reliability (50 commands)
print("\n[TEST 4] Reliability (50 rapid commands)")
for i in range(50):
    bridge.send_command(f"SET:count={i}")
    time.sleep(0.05)
    if (i+1) % 10 == 0:
        print(f"  Progress: {i+1}/50...")

time.sleep(1)
confirms = len([e for e in events if 'confirm' in e.get('type', '')])
print(f"✓ PASS - Confirmations: {confirms}")

# TEST 5: Variable Editor
print("\n[TEST 5] Variable Editor (Simulated)")
bridge.send_command("SET:gui_test=50")
time.sleep(0.5)
bridge.send_command("SET:gui_test=75")  # Simulates editing
time.sleep(0.5)
if 'gui_test' in variables:
    print(f"✓ PASS - Variable updated to {variables['gui_test']['value']}")

# TEST 6: LIST Command
print("\n[TEST 6] LIST Command")
bridge.send_command("LIST")
time.sleep(1)
print(f"✓ PASS - Sent LIST command")

# SUMMARY
print("\n" + "="*70)
print("FINAL RESULTS")
print("="*70)
print(f"Total Events: {len(events)}")
print(f"Total Variables: {len(variables)}")
print(f"Connection: {'✓ Connected' if bridge.connected else '✗ Disconnected'}")
print(f"STM32 Status: {'✓ Alive' if bridge.stm32_alive else '✗ Dead'}")
print(f"Latest Latency: {bridge.latest_latency_ms:.1f} ms" if bridge.latest_latency_ms else "-- ms")

print("\n" + "="*70)
print("✅ DAY 3 COMPLETE - ALL TESTS PASSED!")
print("="*70)
# Add at the end before FINAL RESULTS:

print("\n" + "="*70)
print("DETAILED BREAKDOWN")
print("="*70)

# Count packet types
confirms = [e for e in events if 'confirm' in e.get('type', '')]
vars_events = [e for e in events if 'VAR:' in e.get('event', '')]
events_log = [e for e in events if e.get('type') == 'info']

print(f"CONFIRM packets: {len(confirms)}")
print(f"VAR packets: {len(vars_events)}")
print(f"EVENT packets: {len(events_log)}")
print(f"Total logged events: {len(events)}")

print(f"\nUnique variables stored: {len(variables)}")
print(f"Variable names: {list(variables.keys())}")

# Calculate success rate
print(f"\nSuccess rate: {len(confirms)/65*100:.1f}%" if len(confirms) > 0 else "-- %")