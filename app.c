#include "app.h"
#include "main.h"
#include <string.h>
#include <stdio.h>
#include <stdlib.h>

extern UART_HandleTypeDef huart1;

// Forward declaration - UART_SendFramed is in uart_handler.c
extern void UART_SendFramed(const char* data);

// ============================================================================
// Variable Storage
// ============================================================================
Variable_t variables[MAX_VARIABLES];
uint8_t variable_count = 0;

// ============================================================================
// Timing Variables for Send Alive (every 1 second)
// ============================================================================
static uint32_t last_alive_time = 0;

// ============================================================================
// APP_Init - Initialize the application
// ============================================================================
void APP_Init(void)
{
    variable_count = 0;
    last_alive_time = HAL_GetTick();

    // Send startup event using framing
    UART_SendFramed("SYSTEM_START");
}

// ============================================================================
// APP_Loop - Main application loop
// ============================================================================
void APP_Loop(void)
{
    uint32_t current_time = HAL_GetTick();
    uint32_t elapsed = current_time - last_alive_time;

    if (elapsed >= 1000)
    {
        APP_SendAlive();
        last_alive_time = current_time;
    }
}

// ============================================================================
// APP_SendAlive - Send status message to PC
// ============================================================================
void APP_SendAlive(void)
{
    // Using framing for consistent protocol
    UART_SendFramed("STM32 alive");
}

// ============================================================================
// APP_SendVariable - Send variable value to PC
// Format: VAR:name=value
// ============================================================================
void APP_SendVariable(const char* name, int32_t value)
{
    char buffer[100];

    // Build packet: VAR:name=value
    snprintf(buffer, sizeof(buffer), "VAR:%s=%ld", name, value);

    // Send with framing for reliable delivery
    UART_SendFramed(buffer);
}

// ============================================================================
// APP_SendEvent - Send event message to PC
// Format: EVENT:message
// ============================================================================
void APP_SendEvent(const char* event)
{
    char buffer[100];

    // Build packet: EVENT:message
    snprintf(buffer, sizeof(buffer), "EVENT:%s", event);

    // Send with framing for reliable delivery
    UART_SendFramed(buffer);
}

// ============================================================================
// APP_SendConfirmation - Send command confirmation
// Format: CONFIRM:command,status
// status = "OK" or "ERROR"
// ============================================================================
void APP_SendConfirmation(const char* cmd, uint8_t success)
{
    char buffer[100];
    const char* status = success ? "OK" : "ERROR";

    // Build packet: CONFIRM:command,status
    snprintf(buffer, sizeof(buffer), "CONFIRM:%s,%s", cmd, status);

    // Send with framing for reliable delivery
    UART_SendFramed(buffer);
}

// ============================================================================
// APP_SetVariable - Store/update variable in memory
// ============================================================================
void APP_SetVariable(const char* name, int32_t value)
{
    // Search if variable already exists
    for (int i = 0; i < variable_count; i++)
    {
        if (strcmp(variables[i].name, name) == 0)
        {
            // Update existing variable
            variables[i].value = value;
            variables[i].is_set = 1;
            return;
        }
    }

    // Variable not found, add new one (if space available)
    if (variable_count < MAX_VARIABLES)
    {
        strncpy(variables[variable_count].name, name, sizeof(variables[variable_count].name) - 1);
        variables[variable_count].value = value;
        variables[variable_count].is_set = 1;
        variable_count++;
    }
}

// ============================================================================
// APP_GetVariable - Retrieve variable value
// Returns: value if found, 0 if not found
// ============================================================================
int32_t APP_GetVariable(const char* name)
{
    for (int i = 0; i < variable_count; i++)
    {
        if (strcmp(variables[i].name, name) == 0)
        {
            return variables[i].value;
        }
    }
    return 0;  // Not found
}

// ============================================================================
// APP_ParseCommand - Parse and execute commands from PC
// ============================================================================
// Supported Commands:
// - GET:name          → Read variable (responds with VAR:name=value)
// - SET:name=value    → Write variable (responds with CONFIRM + VAR + EVENT)
// - LIST              → List all variables
// ============================================================================
void APP_ParseCommand(const char* command)
{
    char cmd_copy[100];
    strncpy(cmd_copy, command, sizeof(cmd_copy) - 1);
    cmd_copy[sizeof(cmd_copy) - 1] = '\0';

    // ========================================================================
    // GET Command - Read variable value
    // Format: GET:variable_name
    // ========================================================================
    if (strncmp(cmd_copy, "GET:", 4) == 0)
    {
        const char* var_name = cmd_copy + 4;
        int32_t value = APP_GetVariable(var_name);

        // Send variable value back to PC (with framing)
        APP_SendVariable(var_name, value);

        // Send confirmation (with framing)
        APP_SendConfirmation("GET", 1);

        // Send event (with framing)
        char event[80];
        snprintf(event, sizeof(event), "Variable read: %s=%ld", var_name, value);
        APP_SendEvent(event);
        return;
    }

    // ========================================================================
    // SET Command - Write variable value
    // Format: SET:variable_name=value
    // ========================================================================
    if (strncmp(cmd_copy, "SET:", 4) == 0)
    {
        const char* payload = cmd_copy + 4;

        // Split on "="
        char* equals = strchr(payload, '=');
        if (equals == NULL)
        {
            APP_SendConfirmation("SET", 0);  // Error
            APP_SendEvent("SET: Invalid format (no =)");
            return;
        }

        // Extract variable name
        int name_len = equals - payload;
        char var_name[32];
        strncpy(var_name, payload, name_len);
        var_name[name_len] = '\0';

        // Extract and convert value
        int32_t value = atoi(equals + 1);

        // Set the variable
        APP_SetVariable(var_name, value);

        // Send confirmation back to PC (with framing)
        APP_SendConfirmation("SET", 1);  // Success

        // Send the updated variable (with framing)
        APP_SendVariable(var_name, value);

        // Send event (with framing)
        char event[80];
        snprintf(event, sizeof(event), "Variable set: %s=%ld", var_name, value);
        APP_SendEvent(event);
        return;
    }

    // ========================================================================
    // LIST Command - Send all stored variables
    // Format: LIST
    // ========================================================================
    if (strcmp(cmd_copy, "LIST") == 0)
    {
        if (variable_count == 0)
        {
            APP_SendEvent("No variables stored");
        }
        else
        {
            for (int i = 0; i < variable_count; i++)
            {
                APP_SendVariable(variables[i].name, variables[i].value);
               // HAL_Delay(10);  // Small delay between packets
            }
        }

        // Send confirmation (with framing)
        APP_SendConfirmation("LIST", 1);

        // Send completion event (with framing)
        APP_SendEvent("Variables listed");
        return;
    }

    // ========================================================================
    // Unknown Command
    // ========================================================================
    APP_SendConfirmation(cmd_copy, 0);  // Error

    char event[100];
    snprintf(event, sizeof(event), "Unknown command: %s", cmd_copy);
    APP_SendEvent(event);
}
