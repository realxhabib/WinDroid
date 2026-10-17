"""children.py: long-lived children end with us, however we end; their own children don't."""
import ctypes
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# A venv's python.exe starts the real interpreter as a child of its own: use the real one, so the
# processes below are exactly the ones we mean.
PYTHON = getattr(sys, "_base_executable", sys.executable)

# The agent: starts a child with children.popen, says who's who, then ends as it's told to.
AGENT = """
import os, subprocess, sys, time
from androiddesktop import children
child = children.popen([sys.argv[1], "-c", sys.argv[2]], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
print(os.getpid(), child.pid, child.stdout.readline().strip(), flush=True)
sys.stdin.readline()
if sys.argv[3] == "exit":
    os._exit(0)
raise RuntimeError("crash")
"""
# The child (like adb): starts a child of its own (like the adb server), which must outlive the agent.
CHILD = """
import subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL)
print(grandchild.pid, flush=True)
time.sleep(60)
"""

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
SYNCHRONIZE, PROCESS_TERMINATE = 0x00100000, 0x0001


class DiesWithUs(unittest.TestCase):
    def open(self, pid: int) -> int:
        handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_TERMINATE, False, pid)
        self.assertTrue(handle, f"process {pid} isn't running")
        self.addCleanup(kernel32.CloseHandle, handle)
        self.addCleanup(kernel32.TerminateProcess, handle, 1)
        return handle

    @staticmethod
    def ended(handle: int, within: float) -> bool:
        return kernel32.WaitForSingleObject(handle, int(within * 1000)) == 0

    def check(self, how: str) -> None:
        agent = subprocess.Popen([PYTHON, "-c", AGENT, PYTHON, CHILD, how], cwd=ROOT, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for cleanup in (agent.stdout.close, agent.stdin.close, agent.kill):
            self.addCleanup(cleanup)
        agent_pid, child_pid, grandchild_pid = map(int, agent.stdout.readline().split())
        child, grandchild = self.open(child_pid), self.open(grandchild_pid)
        self.assertFalse(self.ended(child, 0.2), "the child should run until the agent ends")
        if how == "kill":  # as Task Manager does
            kernel32.TerminateProcess(self.open(agent_pid), 1)
        else:
            agent.stdin.write("go\n")
            agent.stdin.flush()
        agent.wait(10)
        self.assertTrue(self.ended(child, 5), "the child outlived the agent")
        self.assertFalse(self.ended(grandchild, 0.5), "the child's own child (the adb server) ended too")

    def test_os_exit(self):
        self.check("exit")

    def test_crash(self):
        self.check("crash")

    def test_killed(self):
        self.check("kill")


class EveryLongLivedChild(unittest.TestCase):
    def test_goes_through_children_py(self):
        """subprocess.Popen only where a child is meant to outlive us: the emulator (taken over by the
        next agent after a crash), the agent that the MCP server starts, the agent windroid-cli.exe
        hands over to (app.py), and an update's installer, which replaces us (update.py)."""
        allowed = {"children.py", "vm.py", "ghost.py", "app.py", "update.py"}
        for path in (ROOT / "androiddesktop").glob("*.py"):
            if path.name not in allowed:
                self.assertIsNone(re.search(r"subprocess\.Popen\(", path.read_text(encoding="utf-8")),
                                  f"{path.name}: start long-lived children with adb.start or children.popen")


if __name__ == "__main__":
    unittest.main()
