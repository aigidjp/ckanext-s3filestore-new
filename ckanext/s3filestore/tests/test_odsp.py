# encoding: utf-8
"""Tests for ODSP (AWS Open Data Sponsorship Program) functionality.

Covers:
  - BaseS3Uploader._using_odsp_bucket() and get_other_bucket_name()
  - S3ResourceUploader routing uploads to the correct bucket based on dataset license
  - s3-migrate-odsp CLI command (check / copy / move modes)

Note on click_commands module-level vars:
  click_commands.py reads config at import time (module-level assignments).
  @pytest.mark.ckan_config cannot override those values, so the CLI tests
  use monkeypatch to set them directly.
"""

import os
import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner

import ckan.tests.factories as factories

from ckanext.s3filestore.uploader import BaseS3Uploader
import ckanext.s3filestore.click_commands as click_commands
from ckanext.s3filestore.click_commands import migrate_odsp


OPEN_LICENSE = 'cc-by'
NON_OPEN_LICENSE = 'notspecified'
REGULAR_BUCKET = 'test-bucket'
ODSP_BUCKET = 'test-odsp-bucket'


# ---------------------------------------------------------------------------
# BaseS3Uploader ODSP helper methods
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures('ckan_config_dynamic')
@pytest.mark.ckan_config_dynamic('ckanext.s3filestore.odsp_bucket_name', ODSP_BUCKET)
class TestBaseS3UploaderOdspMethods:
    """Unit tests for ODSP helper methods. No DB access needed."""

    def test_using_odsp_bucket_false_for_default_bucket(self, ckan_config):
        uploader = BaseS3Uploader()
        assert not uploader._using_odsp_bucket()

    def test_using_odsp_bucket_true_when_on_odsp_bucket(self, ckan_config):
        uploader = BaseS3Uploader()
        uploader.bucket_name = ODSP_BUCKET
        assert uploader._using_odsp_bucket()

    def test_get_other_bucket_name_from_regular_bucket(self, ckan_config):
        uploader = BaseS3Uploader()
        assert uploader.get_other_bucket_name() == ODSP_BUCKET

    def test_get_other_bucket_name_from_odsp_bucket(self, ckan_config):
        uploader = BaseS3Uploader()
        uploader.bucket_name = ODSP_BUCKET
        assert uploader.get_other_bucket_name() == REGULAR_BUCKET

    @pytest.mark.ckan_config_dynamic('ckanext.s3filestore.odsp_bucket_name', '')
    def test_get_other_bucket_name_none_when_odsp_not_configured(self, ckan_config):
        uploader = BaseS3Uploader()
        assert uploader.get_other_bucket_name() is None


# ---------------------------------------------------------------------------
# S3ResourceUploader: bucket routing on upload
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures('clean_db', 'clean_index', 'with_plugins', 'ckan_config_dynamic')
@pytest.mark.ckan_config_dynamic('ckanext.s3filestore.odsp_bucket_name', ODSP_BUCKET)
@pytest.mark.ckan_config_dynamic('ckanext.s3filestore.odsp_open_license_ids', 'cc-by cc-zero')
class TestS3ResourceUploaderOdspRouting:
    """Test that S3ResourceUploader routes files to the correct bucket based on dataset license."""

    def test_open_license_resource_goes_to_odsp_bucket(
            self, s3_client, create_with_upload, ckan_config):
        dataset = factories.Dataset(license_id=OPEN_LICENSE)
        resource = create_with_upload(
            'some content', 'data.csv', package_id=dataset['id']
        )
        key = 'resources/{0}/data.csv'.format(resource['id'])

        s3_client.head_object(Bucket=ODSP_BUCKET, Key=key)
        with pytest.raises(ClientError) as exc:
            s3_client.head_object(Bucket=REGULAR_BUCKET, Key=key)
        assert exc.value.response['Error']['Code'] == '404'

    def test_non_open_license_resource_goes_to_regular_bucket(
            self, s3_client, create_with_upload, ckan_config):
        dataset = factories.Dataset(license_id=NON_OPEN_LICENSE)
        resource = create_with_upload(
            'some content', 'data.csv', package_id=dataset['id']
        )
        key = 'resources/{0}/data.csv'.format(resource['id'])

        s3_client.head_object(Bucket=REGULAR_BUCKET, Key=key)
        with pytest.raises(ClientError) as exc:
            s3_client.head_object(Bucket=ODSP_BUCKET, Key=key)
        assert exc.value.response['Error']['Code'] == '404'


# ---------------------------------------------------------------------------
# s3-migrate-odsp CLI command
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures('clean_db', 'clean_index', 'with_plugins', 'ckan_config_dynamic')
@pytest.mark.ckan_config_dynamic('ckanext.s3filestore.odsp_open_license_ids', '')
class TestMigrateOdspCommand:
    """Tests for the s3-migrate-odsp CLI command.

    The class-level ckan_config_dynamic marker clears odsp_open_license_ids so
    that create_with_upload always places files in REGULAR_BUCKET (no ODSP
    routing). The patched CLI vars then see those files as misplaced for
    open-license datasets, enabling each mode to be tested independently.
    """

    @pytest.fixture(autouse=True)
    def patch_cli_vars(self, monkeypatch, ckan_config_dynamic):
        """Patch module-level vars in click_commands that are read at import time."""
        monkeypatch.setattr(click_commands, 'odsp_bucket_name', ODSP_BUCKET)
        monkeypatch.setattr(click_commands, 'odsp_open_license_ids',
                            [OPEN_LICENSE, 'cc-zero'])
        monkeypatch.setattr(click_commands, 'bucket_name', REGULAR_BUCKET)
        monkeypatch.setattr(click_commands, 'resources_storage_path', 'resources')
        monkeypatch.setattr(
            click_commands, 'sqlalchemy_url',
            os.environ.get('CKAN_SQLALCHEMY_URL',
                           'postgresql://ckan_default:pass@db/ckan_test')
        )

    def _exists(self, s3_client, bucket, key):
        try:
            s3_client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                return False
            raise

    def test_check_mode_reports_misplaced_resource(self, s3_client, create_with_upload):
        """A file in the wrong bucket is counted as needs_action; nothing is moved."""
        dataset = factories.Dataset(license_id=OPEN_LICENSE)
        resource = create_with_upload('content', 'data.csv', package_id=dataset['id'])
        key = 'resources/{0}/data.csv'.format(resource['id'])

        result = CliRunner().invoke(migrate_odsp, ['--mode', 'check'])

        assert result.exit_code == 0, result.output
        assert 'needs_action: 1' in result.output
        assert not self._exists(s3_client, ODSP_BUCKET, key)
        assert self._exists(s3_client, REGULAR_BUCKET, key)

    def test_copy_mode_copies_to_correct_bucket_without_deleting(
            self, s3_client, create_with_upload):
        """copy mode puts misplaced files in the correct bucket without deleting the original.
        Files already in the correct bucket are not touched."""
        dataset_open = factories.Dataset(license_id=OPEN_LICENSE)
        resource_open = create_with_upload('content', 'data.csv', package_id=dataset_open['id'])
        key_open = 'resources/{0}/data.csv'.format(resource_open['id'])

        dataset_non_open = factories.Dataset(license_id=NON_OPEN_LICENSE)
        resource_non_open = create_with_upload('content', 'data.csv', package_id=dataset_non_open['id'])
        key_non_open = 'resources/{0}/data.csv'.format(resource_non_open['id'])

        result = CliRunner().invoke(migrate_odsp, ['--mode', 'copy'])

        assert result.exit_code == 0, result.output
        assert self._exists(s3_client, ODSP_BUCKET, key_open)
        assert self._exists(s3_client, REGULAR_BUCKET, key_open)
        assert self._exists(s3_client, REGULAR_BUCKET, key_non_open)
        assert not self._exists(s3_client, ODSP_BUCKET, key_non_open)

    def test_move_mode_copies_to_correct_bucket_and_deletes_from_wrong(
            self, s3_client, create_with_upload):
        """move mode puts misplaced files in the correct bucket and removes them from the wrong one.
        Files already in the correct bucket are not touched."""
        dataset_open = factories.Dataset(license_id=OPEN_LICENSE)
        resource_open = create_with_upload('content', 'data.csv', package_id=dataset_open['id'])
        key_open = 'resources/{0}/data.csv'.format(resource_open['id'])

        dataset_non_open = factories.Dataset(license_id=NON_OPEN_LICENSE)
        resource_non_open = create_with_upload('content', 'data.csv', package_id=dataset_non_open['id'])
        key_non_open = 'resources/{0}/data.csv'.format(resource_non_open['id'])

        result = CliRunner().invoke(migrate_odsp, ['--mode', 'move'])

        assert result.exit_code == 0, result.output
        assert self._exists(s3_client, ODSP_BUCKET, key_open)
        assert not self._exists(s3_client, REGULAR_BUCKET, key_open)
        assert self._exists(s3_client, REGULAR_BUCKET, key_non_open)
        assert not self._exists(s3_client, ODSP_BUCKET, key_non_open)

    def test_check_mode_ok_for_resource_in_correct_bucket(
            self, s3_client, create_with_upload):
        """A non-open-license resource already in the regular bucket is counted as OK."""
        dataset = factories.Dataset(license_id=NON_OPEN_LICENSE)
        resource = create_with_upload('content', 'data.csv', package_id=dataset['id'])
        key = 'resources/{0}/data.csv'.format(resource['id'])

        result = CliRunner().invoke(migrate_odsp, ['--mode', 'check'])

        assert result.exit_code == 0, result.output
        assert 'OK: 1' in result.output
        assert 'needs_action: 0' in result.output
        assert self._exists(s3_client, REGULAR_BUCKET, key)

    @pytest.mark.ckan_config_dynamic('ckanext.s3filestore.odsp_open_license_ids', 'cc-by cc-zero')
    def test_check_mode_ok_for_open_license_resource_in_odsp_bucket(
            self, s3_client, create_with_upload):
        """An open-license resource already in the ODSP bucket is counted as OK."""
        dataset = factories.Dataset(license_id=OPEN_LICENSE)
        resource = create_with_upload('content', 'data.csv', package_id=dataset['id'])
        key = 'resources/{0}/data.csv'.format(resource['id'])

        result = CliRunner().invoke(migrate_odsp, ['--mode', 'check'])

        assert result.exit_code == 0, result.output
        assert 'OK: 1' in result.output
        assert 'needs_action: 0' in result.output
        assert self._exists(s3_client, ODSP_BUCKET, key)

    def test_command_exits_early_when_odsp_not_configured(self, monkeypatch):
        """Command prints an error and returns immediately when ODSP bucket is not set."""
        monkeypatch.setattr(click_commands, 'odsp_bucket_name', None)
        result = CliRunner().invoke(migrate_odsp, ['--mode', 'check'])
        assert result.exit_code == 0
        assert 'must be configured' in result.output