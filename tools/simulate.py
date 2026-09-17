#!/usr/bin/env python3
"""Compatibility launcher for the independent lab package."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lab.referee import *
from lab.evaluate import main, run
if __name__=='__main__': main()
