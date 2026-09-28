import sys
import os
import pytest
import requests
from unittest import mock
from host_modules import file_service

class TestFileService(object):
    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.stat")
    @mock.patch("os.umask")
    def test_get_file_stat_valid(self, mock_umask, mock_stat, MockInit, MockBusName, MockSystemBus):
        mock_stat_result = mock.Mock()
        mock_stat_result.st_mtime = 1609459200.0  # 2021-01-01 00:00:00 in nanoseconds
        mock_stat_result.st_mode = 0o100644  # Regular file with permissions
        mock_stat_result.st_size = 1024
        mock_stat.return_value = mock_stat_result

        mock_umask.return_value = 0o022  # Default umask

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        path = "/valid/path"
        ret, msg = file_service_stub.get_file_stat(path)

        assert ret == 0
        assert msg['path'] == path
        assert msg['last_modified'] == "1609459200000000000"
        assert msg['permissions'] == "644"
        assert msg['size'] == "1024"
        assert msg['umask'] == "o22"

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.stat")
    def test_get_file_stat_invalid_path(self, mock_stat, MockInit, MockBusName, MockSystemBus):
        mock_stat.side_effect = FileNotFoundError("[Errno 2] No such file or directory")

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        path = "/invalid/path"
        ret, msg = file_service_stub.get_file_stat(path)

        assert ret == 1
        assert 'error' in msg
        assert "No such file or directory" in msg['error']

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    def test_get_file_stat_empty_path(self, MockInit, MockBusName, MockSystemBus):
        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        path = ""
        ret, msg = file_service_stub.get_file_stat(path)

        assert ret == 1
        assert "Dbus get_file_stat called with no path specified" in msg['error']

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("paramiko.SSHClient")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_sftp_success(self, mock_exists, mock_stat, MockSSHClient, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        mock_ssh = mock.Mock()
        MockSSHClient.return_value = mock_ssh
        mock_sftp = mock.Mock()
        mock_ssh.open_sftp.return_value = mock_sftp

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        ret, msg = file_service_stub.download(
            hostname="example.com",
            username="user",
            password="password",
            remote_path="/remote/path/file.txt",
            local_path="/local/path/file.txt",
            protocol="SFTP"
        )

        assert ret == 0
        assert msg == ""
        mock_ssh.connect.assert_called_once_with("example.com", username="user", password="password")
        mock_sftp.get.assert_called_once_with("/remote/path/file.txt", "/local/path/file.txt")
        mock_sftp.close.assert_called_once()
        mock_ssh.close.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("paramiko.SSHClient")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_sftp_failure(self, mock_exists, mock_stat, MockSSHClient, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        mock_ssh = mock.Mock()
        MockSSHClient.return_value = mock_ssh
        mock_ssh.open_sftp.side_effect = Exception("SFTP error")

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        ret, msg = file_service_stub.download(
            hostname="example.com",
            username="user",
            password="password",
            remote_path="/remote/path/file.txt",
            local_path="/local/path/file.txt",
            protocol="SFTP"
        )

        assert ret == 1
        assert "SFTP error" in msg
        mock_ssh.connect.assert_called_once_with("example.com", username="user", password="password")
        mock_ssh.close.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("host_modules.file_service.create_http_session")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_http_success(self, mock_exists, mock_stat, MockSessionFactory, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        mock_response = mock.Mock()
        mock_response.iter_content.return_value = [b"chunk1", b"chunk2"]
        mock_response.raise_for_status.return_value = None
        mock_response.status_code = 200
        MockSessionFactory.return_value.get.return_value = mock_response

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        with mock.patch("builtins.open", mock.mock_open()) as mock_file:
            ret, msg = file_service_stub.download(
                hostname="example.com",
                username="user",
                password="password",
                remote_path="http://example.com/file.txt",
                local_path="/local/path/file.txt",
                protocol="HTTP"
            )

            assert ret == 0
            assert msg == ""
            MockSessionFactory.return_value.get.assert_called_once_with(
                "http://example.com/file.txt",
                auth=("user", "password"),
                stream=True,
                timeout=file_service.HTTP_TIMEOUT,
                allow_redirects=False,
            )
            mock_file.assert_called_once_with("/local/path/file.txt", "xb")
            mock_file().write.assert_any_call(b"chunk1")
            mock_file().write.assert_any_call(b"chunk2")
            mock_response.close.assert_called_once_with()
            MockSessionFactory.return_value.close.assert_called_once_with()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("host_modules.file_service.create_http_session")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_http_failure(self, mock_exists, mock_stat, MockSessionFactory, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        MockSessionFactory.return_value.get.side_effect = Exception("HTTP error")

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        ret, msg = file_service_stub.download(
            hostname="example.com",
            username="user",
            password="password",
            remote_path="http://example.com/file.txt",
            local_path="/local/path/file.txt",
            protocol="HTTP"
        )

        assert ret == 1
        assert "HTTP error" in msg
        MockSessionFactory.return_value.close.assert_called_once_with()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("host_modules.file_service.create_http_session")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_http_status_failure_closes_resources(self, mock_exists, mock_stat, MockSessionFactory, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777
        mock_stat.return_value = mock_dir_stat

        mock_response = mock.Mock()
        mock_response.status_code = 401
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError("401 Unauthorized")
        MockSessionFactory.return_value.get.return_value = mock_response

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        with mock.patch("builtins.open", mock.mock_open()) as mock_file:
            ret, msg = file_service_stub.download(
                hostname="example.com",
                username="user",
                password="password",
                remote_path="http://example.com/file.txt",
                local_path="/local/path/file.txt",
                protocol="HTTP",
            )

        assert ret == file_service.EXIT_FAILURE
        assert "401 Unauthorized" in msg
        mock_file.assert_not_called()
        mock_response.close.assert_called_once_with()
        MockSessionFactory.return_value.close.assert_called_once_with()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("host_modules.file_service.create_http_session")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_http_rejects_untrusted_urls(self, mock_exists, mock_stat, MockSessionFactory, MockInit, MockBusName, MockSystemBus):
        """remote_path must not be able to redirect the daemon away from hostname."""
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        file_service_stub = file_service.FileService(file_service.MOD_NAME)

        cases = [
            # (hostname, remote_path, protocol, reason)
            ("example.com", "http://169.254.169.254/latest/meta-data/", "HTTP", "host mismatch"),
            ("example.com", "http://127.0.0.1:8080/probe", "HTTP", "loopback"),
            ("example.com", "http://example.com@169.254.169.254/x", "HTTP", "userinfo bypass"),
            ("example.com", "http://example.com:pw@evil.com/x", "HTTP", "userinfo with password"),
            ("example.com", "https://example.com/file.txt", "HTTP", "scheme/protocol mismatch"),
            ("example.com", "http://example.com/file.txt", "HTTPS", "scheme/protocol mismatch"),
            ("example.com", "file:///etc/passwd", "HTTP", "non-http scheme"),
            ("example.com", "ftp://example.com/x", "HTTP", "non-http scheme"),
            ("example.com", "//169.254.169.254/x", "HTTP", "scheme-relative url"),
            ("example.com", "/local/relative/path", "HTTP", "no scheme"),
            ("example.com", "http:///file.txt", "HTTP", "no host"),
            ("example.com", "", "HTTP", "empty remote_path"),
            ("example.com", "not a url at all", "HTTP", "unparseable"),
            ("", "http://example.com/file.txt", "HTTP", "empty hostname"),
            ("example.com", "http://evil.example.com/x", "HTTP", "subdomain is a different host"),
            ("example.com", "http://example.com.evil.com/x", "HTTP", "suffix confusion"),
        ]

        for hostname, remote_path, protocol, reason in cases:
            MockSessionFactory.return_value.get.reset_mock()
            with mock.patch("builtins.open", mock.mock_open()) as mock_file:
                ret, msg = file_service_stub.download(
                    hostname=hostname,
                    username="user",
                    password="password",
                    remote_path=remote_path,
                    local_path="/local/path/file.txt",
                    protocol=protocol,
                )

            assert ret == file_service.EXIT_FAILURE, "expected rejection ({}): {}".format(reason, remote_path)
            assert msg, "rejection must explain itself ({})".format(reason)
            # The request must never leave the box, and nothing may be written.
            MockSessionFactory.assert_not_called()
            mock_file.assert_not_called()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("host_modules.file_service.create_http_session")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_http_accepts_equivalent_host_spellings(self, mock_exists, mock_stat, MockSessionFactory, MockInit, MockBusName, MockSystemBus):
        """Case, trailing dot, ports and IPv6 brackets must not cause false rejections."""
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        file_service_stub = file_service.FileService(file_service.MOD_NAME)

        cases = [
            ("example.com", "HTTP://EXAMPLE.COM/file.txt", "HTTP"),
            ("EXAMPLE.com", "http://example.com./file.txt", "HTTP"),
            ("example.com", "http://example.com:8080/file.txt", "HTTP"),
            ("example.com", "https://example.com/file.txt", "HTTPS"),
            ("10.0.0.5", "http://10.0.0.5/file.txt", "HTTP"),
            ("[::1]", "http://[::1]/file.txt", "HTTP"),
        ]

        for hostname, remote_path, protocol in cases:
            MockSessionFactory.return_value.get.reset_mock()
            mock_response = mock.Mock()
            mock_response.iter_content.return_value = [b"data"]
            mock_response.raise_for_status.return_value = None
            mock_response.status_code = 200
            MockSessionFactory.return_value.get.return_value = mock_response

            with mock.patch("builtins.open", mock.mock_open()):
                ret, msg = file_service_stub.download(
                    hostname=hostname,
                    username="user",
                    password="password",
                    remote_path=remote_path,
                    local_path="/local/path/file.txt",
                    protocol=protocol,
                )

            assert ret == 0, "unexpected rejection of {} for {}: {}".format(remote_path, hostname, msg)
            MockSessionFactory.return_value.get.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("host_modules.file_service.create_http_session")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_http_rejects_every_3xx(self, mock_exists, mock_stat, MockSessionFactory, MockInit, MockBusName, MockSystemBus):
        """No 3xx may be treated as a download, with or without a Location header.

        requests.Response.is_redirect only covers 301/302/303/307/308 and only when a
        Location header is present, and raise_for_status() ignores 3xx entirely, so a
        300 or 304 would otherwise be written out as a successful (often empty) file.
        """
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        file_service_stub = file_service.FileService(file_service.MOD_NAME)

        for status in (300, 301, 302, 303, 304, 305, 306, 307, 308, 399):
            for with_location in (True, False):
                MockSessionFactory.return_value.get.reset_mock()
                mock_response = mock.Mock()
                mock_response.status_code = status
                mock_response.headers = (
                    {"Location": "http://169.254.169.254/latest/meta-data/"}
                    if with_location else {}
                )
                mock_response.raise_for_status.return_value = None
                mock_response.iter_content.return_value = [b"should-never-be-written"]
                MockSessionFactory.return_value.get.return_value = mock_response

                with mock.patch("builtins.open", mock.mock_open()) as mock_file:
                    ret, msg = file_service_stub.download(
                        hostname="example.com",
                        username="user",
                        password="password",
                        remote_path="http://example.com/file.txt",
                        local_path="/local/path/file.txt",
                        protocol="HTTP",
                    )

                label = "{} (Location={})".format(status, with_location)
                assert ret == file_service.EXIT_FAILURE, "{} was accepted".format(label)
                assert str(status) in msg, "error should name the status: {}".format(label)
                # Nothing may be written for any 3xx.
                mock_file.assert_not_called()
                if with_location:
                    assert "169.254.169.254" in msg, label
                # requests must have been told not to follow it in the first place.
                assert MockSessionFactory.return_value.get.call_args.kwargs["allow_redirects"] is False
                mock_response.close.assert_called_once_with()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("host_modules.file_service.create_http_session")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_http_omits_auth_when_no_username(self, mock_exists, mock_stat, MockSessionFactory, MockInit, MockBusName, MockSystemBus):
        """Anonymous downloads must not send an empty Basic auth header."""
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        mock_response = mock.Mock()
        mock_response.iter_content.return_value = [b"data"]
        mock_response.raise_for_status.return_value = None
        mock_response.status_code = 200
        MockSessionFactory.return_value.get.return_value = mock_response

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        with mock.patch("builtins.open", mock.mock_open()):
            ret, msg = file_service_stub.download(
                hostname="example.com",
                username="",
                password="",
                remote_path="http://example.com/file.txt",
                local_path="/local/path/file.txt",
                protocol="HTTP",
            )

        assert ret == 0
        assert MockSessionFactory.return_value.get.call_args.kwargs["auth"] is None
        mock_response.close.assert_called_once_with()
        MockSessionFactory.return_value.close.assert_called_once_with()

    def test_create_http_session_ignores_netrc_and_proxy_environment(self, tmp_path, monkeypatch):
        """Root's ambient credentials and proxies must never affect a D-Bus request."""
        netrc = tmp_path / ".netrc"
        netrc.write_text("machine example.test login rootnetrc password rootsecret\n")
        netrc.chmod(0o600)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:8080")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:8080")

        session = file_service.create_http_session()
        try:
            prepared = session.prepare_request(
                requests.Request("GET", "http://example.test/file", auth=None)
            )
            settings = session.merge_environment_settings(
                prepared.url, {}, False, None, None
            )
        finally:
            session.close()

        assert prepared.headers.get("Authorization") is None
        assert settings["proxies"] == {}
        assert session.trust_env is False

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("host_modules.file_service.create_http_session")
    def test_download_http_stream_failure_removes_partial_file(self, MockSessionFactory, MockInit, MockBusName, MockSystemBus, tmp_path):
        """A failed stream must close resources and leave no blocking partial file."""
        tmp_path.chmod(0o777)
        local_path = str(tmp_path / "download.tar")

        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None

        def failing_chunks(chunk_size):
            yield b"partial"
            raise requests.exceptions.ConnectionError("stream broke")

        mock_response.iter_content.side_effect = failing_chunks
        MockSessionFactory.return_value.get.return_value = mock_response

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        ret, msg = file_service_stub.download(
            hostname="example.com",
            username="user",
            password="password",
            remote_path="http://example.com/file.tar",
            local_path=local_path,
            protocol="HTTP",
        )

        assert ret == file_service.EXIT_FAILURE
        assert "stream broke" in msg
        assert not os.path.exists(local_path)
        mock_response.close.assert_called_once_with()
        MockSessionFactory.return_value.close.assert_called_once_with()

    def test_validate_http_url_returns_none_when_acceptable(self):
        """The helper reports problems as strings and success as None."""
        assert file_service.validate_http_url(
            "http://example.com/file.txt", "example.com", "HTTP"
        ) is None
        assert file_service.validate_http_url(
            "https://example.com/file.txt", "example.com", "HTTPS"
        ) is None
        assert file_service.validate_http_url(
            "http://example.com/x", "example.com", "SFTP"
        ) is not None
        assert file_service.validate_http_url(None, "example.com", "HTTP") is not None
        assert file_service.validate_http_url(12345, "example.com", "HTTP") is not None

    def test_normalize_host_equivalences(self):
        assert file_service.normalize_host("EXAMPLE.COM.") == "example.com"
        assert file_service.normalize_host("  Example.Com  ") == "example.com"
        assert file_service.normalize_host("[::1]") == "::1"
        assert file_service.normalize_host("") == ""
        assert file_service.normalize_host(None) == ""

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("paramiko.SSHClient")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_scp_success(self, mock_exists, mock_stat, MockSSHClient, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        mock_ssh = mock.Mock()
        MockSSHClient.return_value = mock_ssh
        mock_scp = mock.Mock()
        with mock.patch("scp.SCPClient", return_value=mock_scp) as MockSCPClient:
            file_service_stub = file_service.FileService(file_service.MOD_NAME)
            ret, msg = file_service_stub.download(
                hostname="example.com",
                username="user",
                password="password",
                remote_path="/remote/path/file.txt",
                local_path="/local/path/file.txt",
                protocol="SCP"
            )

            assert ret == 0
            assert msg == ""
            mock_ssh.connect.assert_called_once_with("example.com", username="user", password="password")
            MockSCPClient.assert_called_once_with(mock_ssh.get_transport())
            mock_scp.get.assert_called_once_with("/remote/path/file.txt", "/local/path/file.txt")
            mock_scp.close.assert_called_once()
            mock_ssh.close.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("paramiko.SSHClient")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_scp_failure(self, mock_exists, mock_stat, MockSSHClient, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        mock_ssh = mock.Mock()
        MockSSHClient.return_value = mock_ssh
        with mock.patch("scp.SCPClient", side_effect=Exception("SCP error")):
            file_service_stub = file_service.FileService(file_service.MOD_NAME)
            ret, msg = file_service_stub.download(
                hostname="example.com",
                username="user",
                password="password",
                remote_path="/remote/path/file.txt",
                local_path="/local/path/file.txt",
                protocol="SCP"
            )

            assert ret == 1
            assert "SCP error" in msg
            mock_ssh.connect.assert_called_once_with("example.com", username="user", password="password")
            mock_ssh.close.assert_called_once()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_download_unsupported_protocol(self, mock_exists, mock_stat, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable
        mock_stat.return_value = mock_dir_stat

        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        ret, msg = file_service_stub.download(
            hostname="example.com",
            username="user",
            password="password",
            remote_path="/remote/path/file.txt",
            local_path="/local/path/file.txt",
            protocol="FTP"  # Unsupported protocol
        )

        assert ret == 1
        assert "Unsupported protocol" in msg

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.remove")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_remove_success(self, mock_exists, mock_stat, mock_remove, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = True
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40777  # World writable directory
        mock_stat.return_value = mock_dir_stat
        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        path = "/some/file.txt"
        ret, msg = file_service_stub.remove(path)
        assert ret == 0
        assert msg == ""
        mock_remove.assert_called_once_with(path)

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.remove")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_remove_file_not_found(self, mock_exists, mock_stat, mock_remove, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = False
        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        path = "/nonexistent/file.txt"
        ret, msg = file_service_stub.remove(path)
        assert ret == 1
        assert "File not found" in msg
        mock_remove.assert_not_called()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.remove")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_remove_dir_not_world_writable(self, mock_exists, mock_stat, mock_remove, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = True
        mock_dir_stat = mock.Mock()
        mock_dir_stat.st_mode = 0o40755  # Not world writable directory
        mock_stat.return_value = mock_dir_stat
        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        path = "/some/file.txt"
        ret, msg = file_service_stub.remove(path)
        assert ret == 1
        assert "Directory is not world writable" in msg
        mock_remove.assert_not_called()

    @mock.patch("dbus.SystemBus")
    @mock.patch("dbus.service.BusName")
    @mock.patch("dbus.service.Object.__init__")
    @mock.patch("os.remove")
    @mock.patch("os.stat")
    @mock.patch("os.path.exists")
    def test_remove_dir_not_found(self, mock_exists, mock_stat, mock_remove, MockInit, MockBusName, MockSystemBus):
        mock_exists.return_value = True
        mock_stat.side_effect = FileNotFoundError("No such directory")
        file_service_stub = file_service.FileService(file_service.MOD_NAME)
        path = "/some/file.txt"
        ret, msg = file_service_stub.remove(path)
        assert ret == 1
        assert "Directory not found" in msg
        mock_remove.assert_not_called()