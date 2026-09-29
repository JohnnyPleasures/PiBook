"""
To-Do List App Module

Provides To-Do list functionality for PiBook including:
- TodoManager: Data persistence and screen refresh
- ToDoScreen: E-ink display screen
- Flask Blueprint: REST API routes
"""

from .manager import TodoManager
from .screen import ToDoScreen

# Flask routes are intentionally not imported here. Importing ToDoScreen
# during PiBook startup must not pull Flask/Werkzeug/Jinja into memory.
__all__ = ['TodoManager', 'ToDoScreen']
