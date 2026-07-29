# ============================================================================
# TCP Client - ESP32 Communication Module
# ============================================================================
# Handles all TCP socket communication with ESP32

import socket  # Library for TCP/IP network communication
import threading  # Library for running code in background threads
import time  # Library for delays and timing functions
import sys  # System library for flushing output buffer

# ============================================================================
# CONFIGURATION SECTION
# ============================================================================
# Change these values if your ESP32 has a different IP or port

ESP32_IP = "192.168.0.36"  # IP address of your ESP32 (from serial monitor)
ESP32_PORT = 5000  # Port number the ESP32 server listens on


# ============================================================================
# DebugBridge CLASS - Handles all TCP communication with ESP32
# ============================================================================

class DebugBridge:
    """
    A class that manages the TCP connection to ESP32 and handles sending/
    receiving data to/from the STM32 microcontroller.

    Key Feature: Only displays status changes (STM32 alive/dead), not
    every single "alive" message to avoid spam.
    """

    def __init__(self):
        """
        Constructor - Initialize the DebugBridge when creating a new object.
        Sets up empty socket and disconnected state.
        """
        self.socket = None  # Will hold the TCP socket object
        self.connected = False  # Flag: True when connected to ESP32
        self.lock = threading.Lock()  # Lock to prevent print conflicts between threads
        self.stm32_alive = False  # Track previous STM32 status

    def connect(self):
        """
        Establish a TCP connection to the ESP32 and start receiving data.

        This method:
        1. Creates a TCP socket
        2. Connects to ESP32 at the configured IP and port
        3. Starts a background thread to listen for incoming messages
        4. Resets STM32 status tracking

        If connection fails, prints error message and stays disconnected.
        """
        try:
            # Create a TCP socket:
            # - socket.AF_INET: Use IPv4 (IP address version 4)
            # - socket.SOCK_STREAM: Use TCP (reliable, connection-oriented)
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

            # Try to connect to ESP32
            # (IP_ADDRESS, PORT) - connects to that server and port
            # This will wait until connected or timeout occurs
            self.socket.connect((ESP32_IP, ESP32_PORT))

            # If we reach this line, connection was successful
            self.connected = True

            # Reset STM32 status - we haven't heard from it yet
            self.stm32_alive = False

            # Use lock to ensure this print doesn't overlap with other threads
            with self.lock:
                print(f"\n✓ Connected to ESP32 at {ESP32_IP}:{ESP32_PORT}")
                print("  Waiting for STM32 status...\n")

            # Create a background thread to receive messages
            # target=self.receive_data: Run the receive_data method in this thread
            # daemon=True: Thread exits automatically when main program exits
            receiver_thread = threading.Thread(
                target=self.receive_data,
                daemon=True
            )

            # Start the background thread (begins running receive_data)
            receiver_thread.start()

        # If connection fails, catch the exception and print error
        except Exception as e:
            with self.lock:
                print(f"\n✗ Connection failed: {e}\n")
            self.connected = False

    def receive_data(self):
        """
        Background thread function - Continuously listen for messages from ESP32.

        This runs in a separate thread (not blocking the main program), so it can
        receive messages while the user is typing commands.

        IMPORTANT: Only shows status changes to avoid spam!
        - First "STM32 alive" message → displays "✓ STM32: ALIVE"
        - Later "STM32 alive" messages → ignored (no spam)
        - Connection lost → displays "✗ STM32: DEAD"

        Process:
        1. Wait for data to arrive from ESP32
        2. Decode the data from bytes to text
        3. Check if status changed (first alive message or connection lost)
        4. Print only if status changed
        5. Repeat until connection is lost
        """
        # Keep running while connected
        while self.connected:
            try:
                # Receive up to 1024 bytes of data from ESP32
                # recv() blocks (waits) until data arrives
                # The data comes back as bytes, not text
                raw_data = self.socket.recv(1024)

                # If recv() returns empty bytes, connection is closed
                if not raw_data:
                    # Connection was closed by ESP32
                    if self.stm32_alive:
                        # Status changed from alive to dead - show message
                        with self.lock:
                            print("\n✗ STM32: DEAD (connection lost)")
                            print("  Type 'reset' to reconnect\n")
                            sys.stdout.write("Enter command: ")
                            sys.stdout.flush()
                        # Update status
                        self.stm32_alive = False
                    # Stop trying to receive
                    self.connected = False
                    break

                # Convert bytes to text string using UTF-8 encoding
                # strip() removes extra whitespace (spaces, newlines, tabs)
                message = raw_data.decode('utf-8').strip()

                # Only process if we actually received something
                if message:
                    # Check if this is a "STM32 alive" message
                    if "STM32 alive" in message.lower():
                        # STM32 is sending alive messages

                        # Check if this is the FIRST alive message (status change)
                        if not self.stm32_alive:
                            # First time we heard STM32 is alive - show status
                            self.stm32_alive = True  # Update status flag

                            # Use lock to prevent overlap with input prompt
                            with self.lock:
                                print("\n✓ STM32: ALIVE")
                                print("  Ready to send commands\n")
                                # Re-display the input prompt
                                sys.stdout.write("Enter command: ")
                                sys.stdout.flush()
                        # else: Already showed alive message, don't spam

                    else:
                        # This is a different message (not "STM32 alive")
                        # Import parser (delayed to avoid circular imports)
                        from backend.parser import parse_packet
                        parse_packet(message)

                        # Show in console
                        with self.lock:
                            print(f"\nSTM32: {message}")
                            # Re-display input prompt
                            sys.stdout.write("Enter command: ")
                            sys.stdout.flush()

            # If anything goes wrong reading data, connection is lost
            except Exception as e:
                # Set connected flag to False to exit the loop
                if self.stm32_alive:
                    self.stm32_alive = False
                    with self.lock:
                        print(f"\n✗ STM32: DEAD ({str(e)})")
                        print("  Type 'reset' to reconnect\n")
                        sys.stdout.write("Enter command: ")
                        sys.stdout.flush()
                self.connected = False
                break

    def send_command(self, command):
        """
        Send a command from the user to the STM32 (through the ESP32).

        Args:
            command (str): The command text to send to STM32

        The command is sent as text with a newline character at the end,
        which is what the ESP32/STM32 expects.
        """
        # Check if we're still connected before trying to send
        if self.connected:
            try:
                # Add newline character '\n' because STM32 reads until newline
                # .encode('utf-8') converts text to bytes for network transmission
                # sendall() sends all bytes (unlike send() which might send partial)
                message_bytes = (command + '\n').encode('utf-8')
                self.socket.sendall(message_bytes)

                # Use lock and print confirmation
                with self.lock:
                    print(f"✓ Sent: {command}\n")

            # If sending fails, connection is probably lost
            except Exception as e:
                with self.lock:
                    print(f"✗ Send failed: {e}\n")
                self.connected = False
        else:
            # User tried to send but we're not connected
            with self.lock:
                print("✗ Not connected to ESP32\n")

    def disconnect(self):
        """
        Close the connection to ESP32 and clean up resources.

        This should be called when exiting the program to properly
        close the socket and release network resources.
        """
        # Check if socket was created
        if self.socket:
            try:
                # Close the socket (stops all communication)
                self.socket.close()
            except:
                pass  # Socket might already be closed, ignore error

        # Update connection status
        self.connected = False
        self.stm32_alive = False