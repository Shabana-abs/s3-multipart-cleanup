"""Tests for S3MultipartCleanupManagerV2 — pure-logic methods (no AWS calls)."""
import threading
from unittest.mock import MagicMock, patch

from s3_multipart_cleanup.constants import RULE_ID
from s3_multipart_cleanup.manager import S3MultipartCleanupManagerV2 as Manager
from s3_multipart_cleanup.models import BucketStatus


def _make_manager(**kwargs):
    """Build a Manager with AWS calls mocked out."""
    with patch.object(Manager, '_init_client'):
        m = Manager.__new__(Manager)
        m.region = None
        m.profile = None
        m.preserve_scoped_rules = True
        m.expected_days = kwargs.get('expected_days', 7)
        m.skip_aws_managed = True
        m._aws_managed_patterns = []
        m.exceptions = set(kwargs.get('exceptions', []))
        m._results_lock = threading.Lock()
        m._results = []
        m._processed_buckets = set()
        m._our_account_id = '123456789012'
        m.s3_client = MagicMock()
        m.sts_client = MagicMock()
        return m


class TestAnalyzeExistingRules:
    def test_empty_config(self):
        m = _make_manager()
        result = m.analyze_existing_rules(None)
        assert result['has_global_rule'] is False
        assert result['is_compliant'] is False
        assert result['our_rule_exists'] is False

    def test_no_multipart_rules(self):
        m = _make_manager()
        config = {'Rules': [{'ID': 'expire', 'Expiration': {'Days': 30}, 'Filter': {'Prefix': ''}, 'Status': 'Enabled'}]}
        result = m.analyze_existing_rules(config)
        assert result['has_global_rule'] is False

    def test_our_managed_rule_compliant(self):
        m = _make_manager(expected_days=7)
        config = {'Rules': [{
            'ID': RULE_ID, 'Status': 'Enabled', 'Filter': {'Prefix': ''},
            'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 7},
        }]}
        result = m.analyze_existing_rules(config)
        assert result['our_rule_exists'] is True
        assert result['has_global_rule'] is True
        assert result['is_compliant'] is True
        assert result['existing_days'] == 7

    def test_our_managed_rule_non_compliant(self):
        m = _make_manager(expected_days=7)
        config = {'Rules': [{
            'ID': RULE_ID, 'Status': 'Enabled', 'Filter': {'Prefix': ''},
            'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 14},
        }]}
        result = m.analyze_existing_rules(config)
        assert result['our_rule_exists'] is True
        assert result['is_compliant'] is False

    def test_scoped_rule_detected(self):
        m = _make_manager()
        config = {'Rules': [{
            'ID': 'scoped', 'Status': 'Enabled', 'Filter': {'Prefix': 'logs/'},
            'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 3},
        }]}
        result = m.analyze_existing_rules(config)
        assert result['has_scoped_rules'] is True
        assert len(result['scoped_rules']) == 1
        assert result['has_global_rule'] is False

    def test_third_party_global_rule(self):
        m = _make_manager(expected_days=7)
        config = {'Rules': [{
            'ID': 'other-tool', 'Status': 'Enabled', 'Filter': {},
            'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 7},
        }]}
        result = m.analyze_existing_rules(config)
        assert result['has_global_rule'] is True
        assert result['is_compliant'] is True
        assert result['our_rule_exists'] is False


class TestCreateRule:
    def test_default_rule(self):
        m = _make_manager()
        rule = m.create_multipart_cleanup_rule(7)
        assert rule['ID'] == RULE_ID
        assert rule['Status'] == 'Enabled'
        assert rule['AbortIncompleteMultipartUpload']['DaysAfterInitiation'] == 7

    def test_custom_days(self):
        m = _make_manager()
        rule = m.create_multipart_cleanup_rule(14)
        assert rule['AbortIncompleteMultipartUpload']['DaysAfterInitiation'] == 14


class TestMergeLifecycleRules:
    def test_no_existing_config(self):
        m = _make_manager()
        rule = m.create_multipart_cleanup_rule(7)
        analysis = m.analyze_existing_rules(None)
        config, desc = m.merge_lifecycle_rules(None, rule, analysis)
        assert len(config['Rules']) == 1
        assert config['Rules'][0]['ID'] == RULE_ID
        assert 'Created new' in desc

    def test_preserves_existing_rules(self):
        m = _make_manager()
        existing = {'Rules': [
            {'ID': 'expire-logs', 'Filter': {'Prefix': 'logs/'}, 'Status': 'Enabled',
             'Expiration': {'Days': 90}},
        ]}
        rule = m.create_multipart_cleanup_rule(7)
        analysis = m.analyze_existing_rules(existing)
        config, desc = m.merge_lifecycle_rules(existing, rule, analysis)
        assert len(config['Rules']) == 2
        ids = {r['ID'] for r in config['Rules']}
        assert 'expire-logs' in ids
        assert RULE_ID in ids

    def test_updates_existing_managed_rule(self):
        m = _make_manager(expected_days=7)
        existing = {'Rules': [
            {'ID': RULE_ID, 'Status': 'Enabled', 'Filter': {'Prefix': ''},
             'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 14}},
            {'ID': 'other', 'Status': 'Enabled', 'Filter': {}, 'Expiration': {'Days': 30}},
        ]}
        rule = m.create_multipart_cleanup_rule(7)
        analysis = m.analyze_existing_rules(existing)
        config, desc = m.merge_lifecycle_rules(existing, rule, analysis)
        assert len(config['Rules']) == 2
        managed = [r for r in config['Rules'] if r['ID'] == RULE_ID]
        assert len(managed) == 1
        assert managed[0]['AbortIncompleteMultipartUpload']['DaysAfterInitiation'] == 7
        assert 'Updated' in desc

    def test_preserves_scoped_rules(self):
        m = _make_manager()
        existing = {'Rules': [
            {'ID': 'scoped-logs', 'Status': 'Enabled', 'Filter': {'Prefix': 'logs/'},
             'AbortIncompleteMultipartUpload': {'DaysAfterInitiation': 3}},
        ]}
        rule = m.create_multipart_cleanup_rule(7)
        analysis = m.analyze_existing_rules(existing)
        config, desc = m.merge_lifecycle_rules(existing, rule, analysis)
        assert len(config['Rules']) == 2
        assert 'preserved 1 scoped' in desc

    def test_lifecycle_limit_999_rules(self):
        """merge still works but preflight_check should block before we get here."""
        m = _make_manager()
        existing = {'Rules': [{'ID': f'rule-{i}', 'Status': 'Enabled', 'Filter': {}} for i in range(998)]}
        rule = m.create_multipart_cleanup_rule(7)
        analysis = m.analyze_existing_rules(existing)
        config, _ = m.merge_lifecycle_rules(existing, rule, analysis)
        assert len(config['Rules']) == 999


class TestIsExcepted:
    def test_exact_match(self):
        m = _make_manager(exceptions=['my-bucket'])
        assert m._is_excepted('my-bucket') is True
        assert m._is_excepted('other-bucket') is False

    def test_glob_match(self):
        m = _make_manager(exceptions=['*-firehose*'])
        assert m._is_excepted('app-firehose-logs') is True
        assert m._is_excepted('app-data') is False


class TestPreflightCheck:
    def test_excepted_bucket(self):
        m = _make_manager(exceptions=['skip-me'])
        ok, reason, status, _ = m.preflight_check('skip-me')
        assert ok is False
        assert status == BucketStatus.SKIPPED_EXCEPTION

    def test_object_lock_bucket(self):
        m = _make_manager()
        m.s3_client.get_object_lock_configuration.return_value = {
            'ObjectLockConfiguration': {'ObjectLockEnabled': 'Enabled'}
        }
        m.s3_client.get_bucket_lifecycle_configuration.return_value = {'Rules': []}
        ok, reason, status, _ = m.preflight_check('locked-bucket')
        assert ok is False
        assert status == BucketStatus.SKIPPED_OBJECT_LOCK
