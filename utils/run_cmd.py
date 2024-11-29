import logging
import shlex
import subprocess

logger = logging.getLogger(__name__)


def _run_command(cmd):
  '''!
  Execute a given command

  @param cmd (str) Command to execute. Since we execute the command directly, and not within the
                   context of the shell, the full path needs to be provided ($PATH is not used).
                   Command parameters are simply separated by a space.
                   Should be either string or a list.
  '''
  try:
    if not cmd:
      return (0, None, None)
    shcmd = shlex.split(cmd)
    proc = subprocess.Popen(shcmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1, close_fds=True)
    output_stdout, output_stderr = proc.communicate()
    list_stdout = []
    for l in output_stdout.splitlines():
       list_stdout.append(str(l.decode()))
    list_stderr = []
    for l in output_stderr.splitlines():
      list_stderr.append(str(l.decode()))
    return (proc.returncode, list_stdout, list_stderr)
  except (OSError, ValueError) as e:
    logging.error(
        "!Exception [%s] encountered while processing the command : %s",
        str(e), str(cmd))
    return (1, None, None)
