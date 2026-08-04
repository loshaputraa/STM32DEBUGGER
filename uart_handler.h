#ifndef UART_HANDLER_H
#define UART_HANDLER_H
#include "stm32f4xx_hal.h"
#include <stdint.h>

// ============================================================================
// UART Configuration
// ============================================================================
#define RX_BUFFER_SIZE 64

// ============================================================================
// Function Declarations
// ============================================================================

/**
 * UART_Handler_Init()
 *
 * Initialize UART receive in interrupt mode.
 * Call this once in main() after HAL_Init()
 *
 * Setup:
 * - Configures UART1 for interrupt-driven reception
 * - Starts receiving 1 byte at a time
 * - Accumulates complete commands until \r or \n
 * - Calls APP_ParseCommand() for each complete command
 */
void UART_Handler_Init(void);

/**
 * UART_SendFramed(const char* data)
 *
 * Send data with length-prefixed framing.
 * Adds [0xAA][length][payload]\r\n format for reliable delivery.
 *
 * Args:
 *   data: Null-terminated string to send
 *         Max 255 bytes (length field is 1 byte)
 *
 * Example:
 *   UART_SendFramed("VAR:temperature=25");
 *   Output: 0xAA 0x13 VAR:temperature=25\r\n
 *
 * Advantages:
 *   - TCP-safe: Handles packet fragmentation
 *   - Reliable: No data loss even under stress
 *   - Self-describing: Payload length known in advance
 *
 * Note: This is called by APP_SendVariable(), APP_SendEvent(), etc.
 *       Do not call directly unless implementing custom packets.
 */
void UART_SendFramed(const char* data);

/**
 * HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
 *
 * INTERNAL: Called by HAL when UART byte arrives.
 * This is the interrupt handler - DO NOT CALL DIRECTLY.
 *
 * Functionality:
 * - Called for each received byte
 * - Accumulates in buffer until newline
 * - Calls APP_ParseCommand() when complete
 * - Re-enables receiver for next byte
 */
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart);

#endif // UART_HANDLER_H
