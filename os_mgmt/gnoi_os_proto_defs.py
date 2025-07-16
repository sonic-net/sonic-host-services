"""Shared protobuf definitions from os.proto."""

# Shared field names

# Message TransferRequest, Validated, ActivateRequest, and VerifyResponse
VERSION_FIELD = 'version'

# Message TransferRequest, ActivateRequest
STANDBY_SUPERVISOR_FIELD = 'standbySupervisor'

# Message InstallError, ActivateError
TYPE_FIELD = 'type'
DETAIL_FIELD = 'detail'

# Unique field names

# Message InstallRequest
TRANSFER_REQUEST_FIELD = 'transferRequest'
TRANSFER_CONTENT_FIELD = 'transferContent'
TRANSFER_END_FIELD = 'transferEnd'

# Message InstallResponse
TRANSFER_READY_FIELD = 'transferReady'
TRANSFER_PROGRESS_FIELD = 'transferProgress'
SYNC_PROGRESS_FIELD = 'syncProgress'
VALIDATED_FIELD = 'validated'
INSTALL_ERROR_FIELD = 'installError'

# Message Validated
DESCRIPTION_FIELD = 'description'

# Message InstallError
INSTALL_ERROR_UNSPECIFIED = 0
INSTALL_ERROR_INCOMPATIBLE = 1
INSTALL_ERROR_TOO_LARGE = 2
INSTALL_ERROR_PARSE_FAIL = 3
INSTALL_ERROR_INTEGRITY_FAIL = 4
INSTALL_ERROR_INSTALL_RUN_PACKAGE = 5
INSTALL_ERROR_INSTALL_IN_PROGRESS = 6
INSTALL_ERROR_UNEXPECTED_SWITCHOVER = 7
INSTALL_ERROR_SYNC_FAIL = 8

# Message ActivateRequest
NO_REBOOT_FIELD = 'noReboot'

# Message ActivateResponse
ACTIVATE_OK_FIELD = 'activateOk'
ACTIVATE_ERROR_FIELD = 'activateError'

# Message ActivateError
ACTIVATE_ERROR_UNSPECIFIED = 0
ACTIVATE_ERROR_NON_EXISTENT_VERSION = 1

# Message VerifyResponse
VERIFY_RESPONSE_FAIL_MESSAGE = 'activationFailMessage'
VERIFY_RESPONSE_STANDBY = 'verifyStandby'

# Message VerifyStandby
VERIFY_STANDBY_STATE = 'standbyState'
VERIFY_STANDBY_RESPONSE = 'verifyResponse'
