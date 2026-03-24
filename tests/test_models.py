"""Tests for models: BucketStatus, BucketResult, CheckpointData."""
import json
import os
import tempfile

from s3_multipart_cleanup.models import BucketResult, BucketStatus, CheckpointData


class TestBucketStatus:
    def test_all_statuses_have_string_values(self):
        for status in BucketStatus:
            assert isinstance(status.value, str)

    def test_expected_statuses_exist(self):
        expected = {
            "pending", "success", "already_configured", "skipped_exception",
            "skipped_object_lock", "skipped_special", "skipped_lifecycle_limit",
            "failed_permission", "failed_throttled", "failed_error", "dry_run",
        }
        actual = {s.value for s in BucketStatus}
        assert expected == actual


class TestBucketResult:
    def test_to_dict_serialises_status(self):
        r = BucketResult(bucket_name="b", status=BucketStatus.SUCCESS, message="ok")
        d = r.to_dict()
        assert d["status"] == "success"
        assert d["bucket_name"] == "b"

    def test_default_fields(self):
        r = BucketResult(bucket_name="b", status=BucketStatus.PENDING, message="")
        assert r.region == ""
        assert r.account_id is None
        assert r.had_scoped_rules is False
        assert r.timestamp  # non-empty


class TestCheckpointData:
    def test_save_and_load_roundtrip(self):
        cp = CheckpointData(
            started_at="2026-01-01T00:00:00",
            config={"days": 7, "dry_run": True},
            processed_buckets=["bucket-a", "bucket-b"],
            results=[{"bucket_name": "bucket-a", "status": "success"}],
        )
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            cp.save(path)
            loaded = CheckpointData.load(path)
            assert loaded.started_at == cp.started_at
            assert loaded.config == cp.config
            assert loaded.processed_buckets == cp.processed_buckets
            assert loaded.results == cp.results
        finally:
            os.unlink(path)
