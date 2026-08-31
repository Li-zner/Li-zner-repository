"""Run deploy batch file using subprocess"""
import subprocess, os

# Run the batch file
result = subprocess.run(
    [r'D:\桌面\agent_gateway\tests\deploy.bat'],
    capture_output=True, text=True, shell=True,
    cwd=r'D:\桌面\agent_gateway'
)
print('STDOUT:')
print(result.stdout)
print('STDERR:')
print(result.stderr)
print('Return code:', result.returncode)
