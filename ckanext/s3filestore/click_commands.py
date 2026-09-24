
import os
import click

from botocore.exceptions import ClientError
from sqlalchemy import create_engine
from sqlalchemy.sql import text
from ckantoolkit import config
from ckanext.s3filestore.uploader import BaseS3Uploader, call_with_acl_fallback

storage_path = config.get('ckan.storage_path',
                          '/var/lib/ckan/default/resources')
sqlalchemy_url = config.get('sqlalchemy.url',
                            'postgresql://user:pass@localhost/db')
bucket_name = config.get('ckanext.s3filestore.aws_bucket_name')
acl = config.get('ckanext.s3filestore.acl', 'public-read')
odsp_bucket_name = config.get('ckanext.s3filestore.odsp_bucket_name')
odsp_open_license_ids = config.get(
    'ckanext.s3filestore.odsp_open_license_ids', '').split()
resources_storage_path = os.path.join(
    config.get('ckanext.s3filestore.aws_storage_path', ''), 'resources')


def _object_exists(client, bucket, key):
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response['Error']['Code'] in ['404', 'NoSuchKey']:
            return False
        raise


def _copy_object(wrong_client, wrong_bucket, correct_client, correct_bucket,
                 key, acl, allow_download_fallback, no_acl_buckets):
    '''Copy an object between buckets, preferring a server-side S3 copy.

    A server-side copy (via correct_client.copy) never transfers the
    object's bytes through this host, but it requires correct_client's
    credentials to have read access to the source bucket. When the two
    buckets belong to different accounts/credentials, that read may be
    denied; in that case fall back to downloading and re-uploading the
    object, but only if the caller explicitly allowed it, since that
    fallback is far more expensive in time and data transfer cost.

    Either method may also be rejected with AccessControlListNotSupported
    if correct_bucket has S3 Object Ownership set to "Bucket owner
    enforced" (ACLs disabled); that is handled by retrying without ACL.
    '''
    try:
        call_with_acl_fallback(
            no_acl_buckets, correct_bucket,
            lambda: correct_client.copy(
                {u'Bucket': wrong_bucket, u'Key': key},
                correct_bucket, key,
                ExtraArgs={u'ACL': acl}, SourceClient=wrong_client),
            lambda: correct_client.copy(
                {u'Bucket': wrong_bucket, u'Key': key},
                correct_bucket, key,
                ExtraArgs={}, SourceClient=wrong_client))
        return u'server-side copy'
    except ClientError as e:
        if not (allow_download_fallback
                and e.response[u'Error'][u'Code'] in (
                    u'AccessDenied', u'403')):
            raise

    def _download_and_upload(extra_args):
        response = wrong_client.get_object(Bucket=wrong_bucket, Key=key)
        extra_args = dict(extra_args)
        extra_args[u'ContentType'] = response.get(
            u'ContentType', u'application/octet-stream')
        correct_client.upload_fileobj(
            response[u'Body'], correct_bucket, key, ExtraArgs=extra_args)

    call_with_acl_fallback(
        no_acl_buckets, correct_bucket,
        lambda: _download_and_upload({u'ACL': acl}),
        lambda: _download_and_upload({}))
    return u'download/upload fallback'


@click.command(u's3-upload',
               short_help=u'Uploads all resources '
                          u'from "ckan.storage_path"'
                          u' to the configured s3 bucket')
def upload_resources():
    resource_ids_and_paths = {}

    for root, dirs, files in os.walk(storage_path):
        if files:
            resource_id = root.split('/')[-2] + root.split('/')[-1] + files[0]
            resource_ids_and_paths[resource_id] = os.path.join(root, files[0])

    click.secho(
        'Found {0} resource files in '
        'the file system'.format(len(resource_ids_and_paths.keys())),
        fg=u'green',
        bold=True)

    engine = create_engine(sqlalchemy_url)
    connection = engine.connect()

    resource_ids_and_names = {}
    resource_ids_and_license_ids = {}

    try:
        for resource_id, file_path in resource_ids_and_paths.items():
            resource = connection.execute(text('''
                   SELECT r.id, r.url, r.url_type, p.license_id
                   FROM resource r
                   JOIN package p ON r.package_id = p.id
                   WHERE r.id = :id
               '''), id=resource_id)
            if resource.rowcount:
                _id, url, _type, license_id = resource.first()
                if _type == 'upload' and url:
                    file_name = url.split('/')[-1] if '/' in url else url
                    resource_ids_and_names[_id] = file_name.lower()
                    resource_ids_and_license_ids[_id] = license_id
    finally:
        connection.close()
        engine.dispose()

    click.secho('{0} resources matched on the database'.format(
        len(resource_ids_and_names.keys())),
        fg=u'green',
        bold=True)

    s3_regular_connection = BaseS3Uploader().get_s3_resource()
    s3_odsp_connection = None
    if odsp_bucket_name and odsp_open_license_ids:
        odsp_uploader = BaseS3Uploader()
        odsp_uploader.bucket_name = odsp_bucket_name
        s3_odsp_connection = odsp_uploader.get_s3_resource()

    no_acl_buckets = set()
    uploaded_resources = []
    for resource_id, file_name in resource_ids_and_names.items():
        license_id = resource_ids_and_license_ids.get(resource_id)
        if (s3_odsp_connection and license_id
                and license_id in odsp_open_license_ids):
            s3_connection = s3_odsp_connection
            target_bucket = odsp_bucket_name
        else:
            s3_connection = s3_regular_connection
            target_bucket = bucket_name
        key = 'resources/{resource_id}/{file_name}'.format(
            resource_id=resource_id, file_name=file_name)
        with open(resource_ids_and_paths[resource_id], u'rb') as f:
            body = f.read()
        call_with_acl_fallback(
            no_acl_buckets, target_bucket,
            lambda: s3_connection.Object(target_bucket, key).put(
                Body=body, ACL=acl),
            lambda: s3_connection.Object(target_bucket, key).put(
                Body=body))
        uploaded_resources.append(resource_id)
        click.secho(
            'Uploaded resource {0} ({1}) to S3 bucket {2}'.format(
                resource_id, file_name, target_bucket),
            fg=u'green',
            bold=True)

    click.secho(
        'Done, uploaded {0} resources to S3'.format(
            len(uploaded_resources)),
        fg=u'green',
        bold=True)


@click.command(u's3-assets',
               short_help=u'Uploads all group assets '
                          u'from "ckan.storage_path"'
                          u' to the configured s3 bucket')
def upload_assets():
    group_ids_and_paths = {}
    for root, dirs, files in os.walk(storage_path):
        if root[-5:] == 'group':
            for idx, group_file in enumerate(files):
                group_ids_and_paths[group_file] = os.path.join(
                    root, files[idx])
    click.secho('Found {0} resource files in the file system'.format(
        len(group_ids_and_paths)),
        fg=u'green',
        bold=True)

    click.secho('{0} group assets found in the database'.format(
        len(group_ids_and_paths.keys())),
        fg=u'green',
        bold=True)

    uploader = BaseS3Uploader()
    s3_connection = uploader.get_s3_resource()

    no_acl_buckets = set()
    uploaded_resources = []
    for resource_id, file_name in group_ids_and_paths.items():
        key = 'storage/uploads/group/{resource_id}'.format(
            resource_id=resource_id)
        with open(file_name, u'rb') as f:
            body = f.read()
        call_with_acl_fallback(
            no_acl_buckets, bucket_name,
            lambda: s3_connection.Object(bucket_name, key).put(
                Body=body, ACL=acl),
            lambda: s3_connection.Object(bucket_name, key).put(
                Body=body))
        uploaded_resources.append(resource_id)
        click.secho(
            'Uploaded resource {0} to S3'.format(file_name),
            fg=u'green', bold=True)
    click.secho('Done, uploaded {0} resources to S3'.format(
        len(uploaded_resources)),
        fg=u'green', bold=True)


@click.command(u's3-migrate-odsp',
               short_help=u'Migrate resources between regular and ODSP S3 '
                          u'buckets based on dataset license')
@click.option(u'--mode',
              type=click.Choice([u'check', u'copy', u'move']),
              default=u'check',
              show_default=True,
              help=u'check: inspect only; '
                   u'copy: copy to correct bucket without deleting; '
                   u'move: copy to correct bucket and delete from wrong bucket')
@click.option(u'--allow-download-fallback', is_flag=True, default=False,
              help=u'If the two buckets use different credentials and a '
                   u'server-side copy is denied, fall back to downloading '
                   u'the object and re-uploading it. Off by default because '
                   u'this fallback is much slower and incurs data transfer '
                   u'cost.')
def migrate_odsp(mode, allow_download_fallback):
    if not odsp_bucket_name or not odsp_open_license_ids:
        click.secho(
            u'ckanext.s3filestore.odsp_bucket_name and '
            u'ckanext.s3filestore.odsp_open_license_ids must be configured',
            fg=u'red', bold=True)
        return

    regular_uploader = BaseS3Uploader()
    odsp_uploader = BaseS3Uploader()
    odsp_uploader.bucket_name = odsp_bucket_name

    regular_client = regular_uploader.get_s3_client()
    odsp_client = odsp_uploader.get_s3_client()

    engine = create_engine(sqlalchemy_url)
    connection = engine.connect()

    try:
        result = connection.execute(text(u'''
            SELECT r.id, r.url, p.license_id
            FROM resource r
            JOIN package p ON r.package_id = p.id
            WHERE r.url_type = 'upload'
        '''))
        resources = result.fetchall()
    finally:
        connection.close()
        engine.dispose()

    click.secho(
        u'{0} upload resources found in database'.format(len(resources)),
        fg=u'green', bold=True)

    counts = {u'ok': 0, u'missing_both': 0, u'needs_action': 0, u'in_both': 0}
    no_acl_buckets = set()

    for resource_id, url, license_id in resources:
        if not url:
            continue

        file_name = os.path.basename(url)
        key = os.path.join(resources_storage_path, resource_id, file_name)

        if license_id in odsp_open_license_ids:
            correct_bucket, correct_client = odsp_bucket_name, odsp_client
            wrong_bucket, wrong_client = bucket_name, regular_client
        else:
            correct_bucket, correct_client = bucket_name, regular_client
            wrong_bucket, wrong_client = odsp_bucket_name, odsp_client

        in_correct = _object_exists(correct_client, correct_bucket, key)
        in_wrong = _object_exists(wrong_client, wrong_bucket, key)

        if in_correct and not in_wrong:
            counts[u'ok'] += 1
            continue

        if not in_correct and not in_wrong:
            click.secho(
                u'WARNING: resource {0} ({1}) not found in either bucket'.format(
                    resource_id, file_name),
                fg=u'yellow', bold=True)
            counts[u'missing_both'] += 1
            continue

        if in_correct and in_wrong:
            click.secho(
                u'WARNING: resource {0} ({1}) exists in both buckets'.format(
                    resource_id, file_name),
                fg=u'yellow', bold=True)
            counts[u'in_both'] += 1
            if mode == u'move':
                wrong_client.delete_object(Bucket=wrong_bucket, Key=key)
                click.secho(
                    u'Deleted resource {0} ({1}) from {2}'.format(
                        resource_id, file_name, wrong_bucket),
                    fg=u'green', bold=True)
            continue

        # exists in wrong bucket only
        counts[u'needs_action'] += 1
        click.secho(
            u'Resource {0} ({1}): should be in {2}, found in {3}'.format(
                resource_id, file_name, correct_bucket, wrong_bucket),
            fg=u'yellow', bold=True)

        if mode in (u'copy', u'move'):
            method = _copy_object(
                wrong_client, wrong_bucket, correct_client, correct_bucket,
                key, acl, allow_download_fallback, no_acl_buckets)
            click.secho(
                u'Copied resource {0} ({1}) to {2} (via {3})'.format(
                    resource_id, file_name, correct_bucket, method),
                fg=u'green', bold=True)

            if mode == u'move':
                wrong_client.delete_object(Bucket=wrong_bucket, Key=key)
                click.secho(
                    u'Deleted resource {0} ({1}) from {2}'.format(
                        resource_id, file_name, wrong_bucket),
                    fg=u'green', bold=True)

    click.secho(
        u'Done. OK: {ok} | needs_action: {needs_action} | '
        u'in_both: {in_both} | missing: {missing_both}'.format(**counts),
        fg=u'green', bold=True)
