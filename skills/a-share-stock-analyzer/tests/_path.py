"""把 scripts/ 加入 import 路径, 让测试能直接 import quote/score/... (它们彼此按裸模块名互相 import)。"""
import os
import sys

_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
