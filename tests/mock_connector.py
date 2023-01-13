class MockConnector(object):
    STATE_DB = None
    data = {}

    def __init__(self, host):
        pass

    def connect(self, db_id):
        pass

    def get(self, db_id, key, field):
        return MockConnector.data[key][field]

    def keys(self, db_id, pattern):
        match = pattern.split('*')[0]
        ret = []
        for key in MockConnector.data.keys():
            if match in key:
                ret.append(key)

        return ret

    def get_all(self, db_id, key):
        return MockConnector.data[key]

    def delete(self, db_id, key):
        return MockConnector.data.delete(db_id, key)

    def delete_all_by_pattern(self, db_id, pattern):
        client = MockConnector.data
        keys = client.keys()
        for key in keys:
            client.delete(db_id, key)

