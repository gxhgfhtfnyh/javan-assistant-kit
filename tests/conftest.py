# -*- coding: utf-8 -*-
"""Let the tests import generate.py from the kit folder, wherever pytest is started from."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
