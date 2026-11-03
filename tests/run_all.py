# -*- coding: utf-8 -*-
"""Runs every offline test in its own process. Usage: python -I tests/run_all.py"""
import os, subprocess, sys

here = os.path.dirname(os.path.abspath(__file__))
failed = 0
for name in ("test_central.py", "test_pc.py"):
    print("== %s" % name)
    failed += subprocess.run([sys.executable, "-I", os.path.join(here, name)]).returncode != 0
print("FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
