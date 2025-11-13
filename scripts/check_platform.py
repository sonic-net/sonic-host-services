#!/usr/bin/env python3
"""
Check if the current platform is a SmartSwitch NPU (not DPU).
Exit 0 if SmartSwitch NPU, exit 1 otherwise.
"""
import sys
import subprocess

def main():
    try:
        # Get subtype from config
        result = subprocess.run(
            ['sonic-cfggen', '-d', '-v', 'DEVICE_METADATA.localhost.subtype'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            # Optionally print error for debugging
            print(f"Error: sonic-cfggen failed with return code {result.returncode}", file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            sys.exit(1)
        subtype = result.stdout.strip()
        
        # Check if DPU
        try:
            from utilities_common.chassis import is_dpu
            is_dpu_platform = is_dpu()
        except Exception:
            is_dpu_platform = False
        
        # Check if SmartSwitch NPU (not DPU)
        if subtype == "SmartSwitch" and not is_dpu_platform:
            sys.exit(0)
        else:
            sys.exit(1)
    except Exception:
        sys.exit(1)

if __name__ == "__main__":
    main()
