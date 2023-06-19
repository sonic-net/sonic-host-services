import subprocess

def run_command_test(command):
    """
    Utility function to run an shell command and return the output.
    :param command: Shell command string.
    :return: Output of the shell command.
    """
    try:
        process = subprocess.Popen(command, shell=True, universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return process.communicate()[0]
    except Exception:
        return None

