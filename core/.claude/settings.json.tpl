{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"{{python_exe}}\" \"{{root_path}}/scripts/session_start.py\""
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"{{python_exe}}\" \"{{root_path}}/scripts/prompt_context_hook.py\"",
            "timeout": 10
          },
          {
            "type": "command",
            "command": "\"{{python_exe}}\" \"{{root_path}}/scripts/recall_hook.py\"",
            "timeout": 10
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "\"{{python_exe}}\" \"{{root_path}}/scripts/send_gate.py\""
          },
          {
            "type": "command",
            "command": "\"{{python_exe}}\" \"{{root_path}}/scripts/activity_log_hook.py\"",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "WebFetch|WebSearch",
        "hooks": [
          {
            "type": "command",
            "command": "\"{{python_exe}}\" \"{{root_path}}/scripts/injection_prescan_hook.py\"",
            "timeout": 10
          }
        ]
      },
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"{{python_exe}}\" \"{{root_path}}/scripts/doctor_edit_hook.py\"",
            "timeout": 15
          },
          {
            "type": "command",
            "command": "\"{{python_exe}}\" \"{{root_path}}/scripts/vault_checkpoint_hook.py\"",
            "timeout": 5
          }
        ],
        "matcher": "Write|Edit|MultiEdit"
      },
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"{{python_exe}}\" \"{{root_path}}/scripts/tool_heartbeat_hook.py\"",
            "timeout": 5
          }
        ]
      },
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"{{python_exe}}\" \"{{root_path}}/scripts/similar_check.py\"",
            "timeout": 10
          }
        ],
        "matcher": "Write"
      },
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"{{python_exe}}\" \"{{root_path}}/scripts/activity_log_hook.py\"",
            "timeout": 5
          }
        ],
        "matcher": "*"
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"{{python_exe}}\" \"{{root_path}}/scripts/stop_gate.py\""
          },
          {
            "type": "command",
            "command": "\"{{python_exe}}\" \"{{root_path}}/scripts/keep_going.py\"",
            "timeout": 10
          },
          {
            "type": "command",
            "command": "\"{{python_exe}}\" \"{{root_path}}/scripts/claim_gate.py\"",
            "timeout": 10
          },
          {
            "type": "command",
            "command": "\"{{python_exe}}\" \"{{root_path}}/scripts/handoff_write.py\"",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
