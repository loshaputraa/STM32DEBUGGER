#ifndef APP_H
#define APP_H

#include "main.h"
#include <stdint.h>

// ============================================================================
// Variable Storage - Stores modifiable variables from PC
// ============================================================================

typedef struct {
    char name[32];
    int32_t value;
    uint8_t is_set;  // 1 if PC has set this variable
} Variable_t;

#define MAX_VARIABLES 10

extern Variable_t variables[MAX_VARIABLES];
extern uint8_t variable_count;
extern UART_HandleTypeDef huart1;
// ============================================================================
// Function Prototypes
// ============================================================================
void APP_Init(void);
void APP_Loop(void);
void APP_SendAlive(void);
void APP_ParseCommand(const char* command);
void APP_SendVariable(const char* name, int32_t value);
void APP_SendEvent(const char* event);
void APP_SendConfirmation(const char* cmd, uint8_t success);
int32_t APP_GetVariable(const char* name);
void APP_SetVariable(const char* name, int32_t value);

#endif  // APP_H
