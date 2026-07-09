"""
Mythos – Full Autonomous AI System
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Mythos is a self-directed, goal-driven AI agent.
It plans, executes, and reflects autonomously without requiring
step-by-step human guidance.
"""

from .agent import MythosAgent
from .config import MythosConfig

__all__ = ["MythosAgent", "MythosConfig"]
__version__ = "0.2.0"
