"""Rewind: crash-safe side effects for AI agents.

from rewind.runner import run_effect             # one effect, at most once
from rewind.langgraph_tools import RewindToolNode  # drop-in for LangGraph's ToolNode
from rewind.fencing import Lease                 # one worker per run
from rewind.compensation import compensate_run   # undo a run (saga)
"""
