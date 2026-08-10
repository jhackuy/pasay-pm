#!/usr/bin/env python3
"""Standalone V1.2 operations worker (scheduler + notifier).

Usage:
    .venv/bin/python bin/run-operations-worker.py [--once] [--interval 60]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.operations.worker import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
