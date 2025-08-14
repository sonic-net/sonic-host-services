#!/bin/bash

subtype=$(sonic-cfggen -d -v DEVICE_METADATA.localhost.subtype)
is_dpu=$(python3 -c "from utilities_common.chassis import is_dpu; print(is_dpu())")

if [[ "$subtype" == "SmartSwitch" && "$is_dpu" != "True" ]]; then
    exit 0
else
    exit 1
fi
