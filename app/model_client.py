"""Compatibility import: both businesses use core.llm."""
import sys
from core import llm
sys.modules[__name__] = llm
