#!/bin/bash

subtype=$(sonic-cfggen -d -v DEVICE_METADATA.localhost.subtype)
is_dpu=$(python3 -c "try:
    from utilities_common.chassis import is_dpu
    print(is_dpu())
except Exception:
    print('False')")

if [[ "$subtype" == "SmartSwitch" && "$is_dpu" != "True" ]]; then
    exit 0
else
    exit 1
fi
