#!/usr/bin/env python3
"""
Check if the current platform is a SmartSwitch NPU (not DPU).
Exit 0 if SmartSwitch NPU, exit 1 otherwise.
"""
import sys

def main():
    try:
        from sonic_py_common import device_info
        from utilities_common.chassis import is_dpu

        # Check if SmartSwitch NPU (not DPU)
        if device_info.is_smartswitch() and not is_dpu():
            sys.exit(0)
        else:
            sys.exit(1)
    except (ImportError, AttributeError, RuntimeError) as e:
        sys.stderr.write("check_platform failed: {}\n".format(str(e)))
        sys.exit(1)

if __name__ == "__main__":
    main()
