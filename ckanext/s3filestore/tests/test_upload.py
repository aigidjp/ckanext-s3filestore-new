# encoding: utf-8
import os

import pytest

from botocore.exceptions import ClientError

from ckantoolkit import config
import ckan.tests.factories as factories
import ckan.tests.helpers as helpers

from ckanext.s3filestore.uploader import S3Uploader
from ckanext.s3filestore.uploader import S3ResourceUploader
from ckanext.s3filestore.uploader import call_with_acl_fallback


class TestCallWithAclFallback(object):
    """Unit tests for call_with_acl_fallback. No DB/S3 access needed."""

    def _denied(self, code):
        def raiser():
            raise ClientError(
                {'Error': {'Code': code, 'Message': 'x'}}, 'PutObject')
        return raiser

    def test_retries_without_acl_on_access_control_list_not_supported(self):
        no_acl_buckets = set()
        calls = []

        result = call_with_acl_fallback(
            no_acl_buckets, 'my-bucket',
            self._denied('AccessControlListNotSupported'),
            lambda: calls.append('without_acl') or 'ok')

        assert result == 'ok'
        assert calls == ['without_acl']
        assert 'my-bucket' in no_acl_buckets

    def test_skips_with_acl_call_for_already_known_bucket(self):
        no_acl_buckets = {'my-bucket'}
        with_acl_calls = []

        result = call_with_acl_fallback(
            no_acl_buckets, 'my-bucket',
            lambda: with_acl_calls.append('with_acl'),
            lambda: 'ok')

        assert result == 'ok'
        assert with_acl_calls == []

    def test_reraises_unrelated_client_errors(self):
        with pytest.raises(ClientError):
            call_with_acl_fallback(
                set(), 'my-bucket',
                self._denied('AccessDenied'),
                lambda: pytest.fail('without_acl should not be called'))


@pytest.mark.usefixtures(u'clean_db', u'clean_index', u'with_plugins')
class TestS3ResourceUpload(object):

    @classmethod
    def setup_class(cls):
        cls.bucket_name = config.get(u'ckanext.s3filestore.aws_bucket_name')

    def test_resource_upload(self,
                             s3_client,
                             resource_with_upload, ckan_config):
        u'''Test a basic resource file upload'''

        key = u'resources/{0}/test.csv' \
            .format(resource_with_upload[u'id'])

        assert s3_client.head_object(Bucket=self.bucket_name, Key=key)

    def test_resource_upload_then_clear(self,
                                        s3_client,
                                        resource_with_upload,
                                        ckan_config):
        u'''Test that clearing an upload removes the S3 key'''

        key = u'resources/{0}/test.csv' \
            .format(resource_with_upload[u'id'])

        # key must exist
        assert s3_client.head_object(Bucket=self.bucket_name, Key=key)

        context = {u'user': factories.Sysadmin()[u'name']}
        helpers.call_action(u'resource_update', context,
                            clear_upload=True,
                            id=resource_with_upload[u'id'])

        # key shouldn't exist, this raises ClientError
        with pytest.raises(ClientError) as e:
            s3_client.head_object(Bucket=self.bucket_name, Key=key)

        assert e.value.response[u'Error'][u'Code'] == u'404'

    def test_resource_uploader_get_path(self):
        u'''Uploader get_path returns as expected'''
        dataset = factories.Dataset()
        resource = factories.Resource(package_id=dataset['id'],
                                      name=u'myfile.txt')

        uploader = S3ResourceUploader(resource)
        returned_path = uploader.get_path(resource[u'id'], resource[u'name'])
        assert returned_path == u'resources/{0}/{1}'.format(resource[u'id'],
                                                            resource[u'name'])

    def test_uploader_get_path(self):
        storage_path = S3Uploader.get_storage_path(u'group')
        assert 'storage/uploads/group' == storage_path

    def test_create_organization_with_image(self,
                                            s3_client,
                                            organization_with_image,
                                            ckan_config):
        user = factories.Sysadmin()
        context = {
            u"user": user["name"]
        }
        result = helpers.call_action(u'organization_show', context,
                                     id=organization_with_image[u'id'])

        storage_path = S3Uploader.get_storage_path(u'group')
        filepath = os.path.join(storage_path, result[u'image_url'])

        # key must exist
        assert s3_client.head_object(Bucket=self.bucket_name, Key=filepath)

    # Since there is a bug in CKAN in _group_or_org_update()
    # def test_create_organization_with_image_then_clear(self,
    #                                                    s3_client,
    #                                                    organization_with_image,
    #                                                    ckan_config):
    #     user = factories.Sysadmin()
    #     context = {
    #         u"user": user["name"]
    #     }
    #     result = helpers.call_action(u'organization_show', context,
    #                                  id=organization_with_image[u'id'])
    #
    #     storage_path = S3Uploader.get_storage_path(u'group')
    #     key = os.path.join(storage_path, result[u'image_url'])
    #
    #     # key must exist
    #     assert s3_client.head_object(Bucket=self.bucket_name, Key=key)
    #
    #     helpers.call_action(u'organization_update', context,
    #                         clear_upload=True,
    #                         id=organization_with_image[u'id'],
    #                         name=organization_with_image[u'name'])
    #
    #     # key shouldn't exist, this raises ClientError
    #     with pytest.raises(ClientError) as e:
    #         s3_client.head_object(Bucket=self.bucket_name, Key=key)
    #
    #     assert e.value.response[u'Error'][u'Code'] == u'404'

    def test_delete_resource_from_s3(self, s3_client,
                                     resource_with_upload):

        resource_id = resource_with_upload[u'id']

        key = u'resources/{0}/test.csv' \
            .format(resource_with_upload[u'id'])

        # key must exist
        assert s3_client.head_object(Bucket=self.bucket_name, Key=key)

        uploader = S3ResourceUploader(resource_with_upload)

        uploader.delete(resource_id, u'test.csv')

        # key shouldn't exist, this raises ClientError
        with pytest.raises(ClientError):
            s3_client.head_object(Bucket=self.bucket_name, Key=key)

    def test_delete_image_from_s3(self, s3_client,
                                  organization_with_image):

        uploader = S3Uploader(u'group')
        storage_path = S3Uploader.get_storage_path(u'group')
        key = os.path.join(storage_path, organization_with_image[u'image_url'])

        # key must exist
        assert s3_client.head_object(Bucket=self.bucket_name, Key=key)

        uploader.delete(organization_with_image[u'image_url'])

        # key shouldn't exist, this raises ClientError
        with pytest.raises(ClientError):
            s3_client.head_object(Bucket=self.bucket_name, Key=key)
