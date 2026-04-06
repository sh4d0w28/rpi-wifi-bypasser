import subprocess


def run_command(cmd, check=False):
    return subprocess.run(cmd, text=True, capture_output=True, check=check)

