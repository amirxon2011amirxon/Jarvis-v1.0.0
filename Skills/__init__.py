"""Jarvis Skills.

This top-level package holds the Skills Engine and every Skill package. It is
deliberately independent of the application UI and of the tool registry: the
engine exposes data and activation results, and ``actions/tool_calling.py``
registers the three LLM-facing tools.
"""
