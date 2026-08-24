"""Auto-Repair agent — RAG (Chroma + HuggingFace) retrieval + Claude code repair.

The self-healing arm of defect intelligence: find the offending code, propose a minimal
patch with Claude, apply it, lint + build, and prepare a PR. See repair_failed_test.py.
"""
