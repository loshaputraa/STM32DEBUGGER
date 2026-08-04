#include "uart_handler.h"
#include "app.h"
#include "main.h"
#include <string.h>

extern UART_HandleTypeDef huart1;

// ============================================================================
// FRAME FORMAT
// ============================================================================
// [0xAA] [LENGTH] [PAYLOAD]\r\n
// Where:
//   0xAA = Frame header (sync byte)
//   LENGTH = Payload length (1 byte, 0-255)
//   PAYLOAD = Actual data
//   \r\n = Terminator for visibility in serial monitors

#define FRAME_HEADER 0xAA

// ============================================================================
// UART Receive Buffer
// ============================================================================
static uint8_t rx_single_char;
static char command_buffer[RX_BUFFER_SIZE];
static uint16_t command_index = 0;

// ============================================================================
// UART_SendFramed - Send data with length-prefixed framing
// ============================================================================
// This function adds a frame header and length byte before payload
// Format: [0xAA][length][payload]\r\n
//
// Args:
//   data: Null-terminated string to send
//
// Example:
//   Input: "VAR:temperature=25"
//   Output: 0xAA 0x13 V A R : t e m p e r a t u r e = 2 5 \r\n
//           └─header─ └length─ └────── payload (19 bytes) ──────┘
//
// Advantages:
//   - Python knows exact payload length
//   - TCP fragmentation doesn't corrupt data
//   - 100% reliable even under stress
// ============================================================================
void UART_SendFramed(const char* data)
{
    if (data == NULL)
        return;

    uint16_t payload_length = strlen(data);

    // Payload must fit in 1 byte length field (max 255)
    if (payload_length > 255)
    {
        // Truncate to fit - critical packets only
        payload_length = 255;
    }

    // Build frame: [HEADER][LENGTH][PAYLOAD]\r\n
    uint8_t frame[259];  // 1 (header) + 1 (length) + 255 (payload) + 2 (\r\n)
    uint16_t frame_len = 0;

    // Add header
    frame[frame_len++] = FRAME_HEADER;  // 0xAA

    // Add length byte
    frame[frame_len++] = (uint8_t)payload_length;

    // Add payload
    memcpy(&frame[frame_len], data, payload_length);
    frame_len += payload_length;

    // Add terminator for visibility in serial monitor
    frame[frame_len++] = '\r';
    frame[frame_len++] = '\n';

    // Send complete frame in one operation
    HAL_UART_Transmit(&huart1, frame, frame_len, HAL_MAX_DELAY);

    // DEBUG: Show frame sent
    // Uncomment if you want to see frame details on serial monitor:
    // char debug[50];
    // snprintf(debug, sizeof(debug), "[FRAME_TX] 0x%02X 0x%02X len=%d\r\n",
    //          FRAME_HEADER, (uint8_t)payload_length, payload_length);
    // HAL_UART_Transmit(&huart1, (uint8_t*)debug, strlen(debug), HAL_MAX_DELAY);
}

// ============================================================================
// UART_Handler_Init - Initialize UART receive in interrupt mode
// ============================================================================
void UART_Handler_Init(void)
{
    // Start receiving one byte at a time in interrupt mode
    // When a byte arrives, HAL_UART_RxCpltCallback will be called
    HAL_UART_Receive_IT(&huart1, &rx_single_char, 1);
}

// ============================================================================
// HAL_UART_RxCpltCallback - Called when a byte is received
// ============================================================================
// This function is called by the HAL layer when UART data arrives
// It accumulates characters until a newline is received, then processes
// the complete command
// ============================================================================
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
    // Only process USART1 (ignore other UARTs if present)
    if (huart->Instance != USART1)
        return;

    // Handle received character
    char received_char = (char)rx_single_char;

    // ========================================================================
    // Handle newline - process complete command
    // ========================================================================
    if (received_char == '\n' || received_char == '\r')
    {
        // Null-terminate the command string
        if (command_index > 0)
        {
            command_buffer[command_index] = '\0';

            // Remove any trailing \r if present
            if (command_index > 0 && command_buffer[command_index - 1] == '\r')
            {
                command_buffer[command_index - 1] = '\0';
                command_index--;
            }

            // Process the complete command
            if (command_index > 0)
            {
                APP_ParseCommand(command_buffer);
            }

            // Reset buffer for next command
            command_index = 0;
            memset(command_buffer, 0, sizeof(command_buffer));
        }
    }
    // ========================================================================
    // Handle regular characters
    // ========================================================================
    else if (command_index < RX_BUFFER_SIZE - 1)
    {
        command_buffer[command_index++] = received_char;
    }
    else
    {
        // Buffer overflow - reset and send error
        command_index = 0;
        memset(command_buffer, 0, sizeof(command_buffer));
        // Send error message using framing
        const char* error_msg = "RX buffer overflow";
        UART_SendFramed(error_msg);
    }

    // Re-enable receiver for next byte
    HAL_UART_Receive_IT(&huart1, &rx_single_char, 1);
}
