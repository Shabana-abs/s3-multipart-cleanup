"""Tests for retry_with_backoff decorator."""
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from s3_multipart_cleanup.retry import retry_with_backoff


def _make_client_error(code: str) -> ClientError:
    return ClientError({'Error': {'Code': code, 'Message': 'test'}}, 'op')


class TestRetryWithBackoff:
    @patch('s3_multipart_cleanup.retry.time.sleep')
    def test_retries_on_throttle(self, mock_sleep):
        call_count = 0

        @retry_with_backoff(max_retries=3, base_delay=0.01)
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _make_client_error('SlowDown')
            return 'ok'

        assert flaky() == 'ok'
        assert call_count == 3
        assert mock_sleep.call_count == 2

    def test_does_not_retry_non_throttle_errors(self):
        @retry_with_backoff(max_retries=3)
        def fail():
            raise _make_client_error('AccessDenied')

        with pytest.raises(ClientError):
            fail()

    def test_does_not_retry_generic_exceptions(self):
        @retry_with_backoff(max_retries=3)
        def fail():
            raise ValueError('boom')

        with pytest.raises(ValueError):
            fail()

    @patch('s3_multipart_cleanup.retry.time.sleep')
    def test_raises_after_max_retries(self, mock_sleep):
        @retry_with_backoff(max_retries=2, base_delay=0.01)
        def always_throttle():
            raise _make_client_error('Throttling')

        with pytest.raises(ClientError):
            always_throttle()
