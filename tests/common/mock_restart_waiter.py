class MockRestartWaiter(object):
    advancedReboot = False
    """
        Mock Config DB which responds to data tables requests and store updates to the data table
    """
    def waitAdvancedBootDone(maxWaitSec=180, dbTimeout=0, isTcpConn=False):
        return True

    def waitWarmBootDone(maxWaitSec=180, dbTimeout=0, isTcpConn=False):
        return False

    def waitFastBootDone(maxWaitSec=180, dbTimeout=0, isTcpConn=False):
        return False

    def isAdvancedBootInProgress(stateDb):
        return MockRestartWaiter.advancedReboot

    def isFastBootInProgress(stateDb):
        return False

    def isWarmBootInProgress(stateDb):
        return False

    def __init__(self):
        pass
