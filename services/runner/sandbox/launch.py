"""Sandbox launcher. Runs as the unprivileged sandbox uid (never as root):
creates a private work directory, writes the submitted code there and runs it
as __main__."""

import os
import runpy
import sys

workdir = sys.argv[1]
os.makedirs(workdir, mode=0o700)
code = sys.stdin.read()
path = os.path.join(workdir, "main.py")
with open(path, "w", encoding="utf-8") as fh:
    fh.write(code)
os.chdir(workdir)
sys.stdin = open(os.devnull, encoding="utf-8")  # noqa: SIM115
sys.argv = ["main.py"]
runpy.run_path(path, run_name="__main__")
