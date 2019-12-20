
"""

tsd-file-api
------------

A multi-tenent API for uploading and downloading files
and JSON data, designed for the University of Oslo's Services for Sensitive Data (TSD).

Endpoints
---------

The following classes of endpoints are available, and they differ from
one another due the use cases they need to support. The use cases are as follows:

/cluster        - store files on the HPC cluster's disk, with POSIX permissions
/files          - store files on network disk, with POSIX permissions
/tables         - store data in sqlite tables, no permissions
/files/upload   - store files (uploaded as form-encoded) on network disk
                  (currently no end-user POSIX permissions)
/sns            - store files (uploaded as form-encoded) on network disk
                  (also no end-user POSIX permissions, and specificaly
                   designed for the UiO Nettskjema service integration)
/store          - store files on disk, no end-user POSIX permissions,
                  but intended for API-only usage (upload and download)
                  so can support access control via integration with another
                  system

All this implies the following for using the API, in terms of it's integration
with and authentication and authorization framework: for authenticated requests,
you MUST use JWT, most probably with an OAUTH2.0 Authorization Server, and the JWTs
MUST be used as Bearer tokens.

Note: /files, and /store use the same RequestHandler(s), the only difference
if that the /files backend, calls a request hook after data processing is finished.
This request hook, sets the end-user POSIX permissions.

"""

import base64
import logging
import os
import pwd
import datetime
import hashlib
import subprocess
import stat
import shutil
import fileinput
import json
import re

from uuid import uuid4
from sys import argv
from collections import OrderedDict

import yaml
import magic
import tornado.queues
from pandas import DataFrame
from tornado.escape import json_decode, url_unescape, url_escape
from tornado import gen
from tornado.httpclient import AsyncHTTPClient
from tornado.ioloop import IOLoop
from tornado.options import parse_command_line, define, options
from tornado.web import Application, RequestHandler, stream_request_body, \
                        HTTPError, MissingArgumentError

from auth import process_access_token
from utils import call_request_hook, sns_dir, \
                  IS_VALID_GROUPNAME, check_filename, _IS_VALID_UUID, \
                  md5sum, tenant_from_url, create_cluster_dir_if_not_exists
from db import sqlite_insert, sqlite_init, \
               sqlite_list_tables, sqlite_get_data, sqlite_update_data, \
               sqlite_delete_data
from resumables import SerialResumable
from pgp import _import_keys


_RW______ = stat.S_IREAD | stat.S_IWRITE
_RW_RW___ = stat.S_IREAD | stat.S_IWRITE | stat.S_IRGRP | stat.S_IWGRP


def read_config(filename):
    with open(filename) as f:
        conf = yaml.load(f, Loader=yaml.Loader)
    return conf


def set_config():
    try:
        _config = read_config(argv[1])
    except Exception as e:
        logging.error(e)
        raise e
    define('config', _config)
    define('port', _config['port'])
    define('debug', _config['debug'])
    define('api_user', _config['api_user'])
    define('check_tenant', _config['token_check_tenant'])
    define('check_exp', _config['token_check_exp'])
    define('start_chars', _config['disallowed_start_chars'])
    define('requestor_claim_name', _config['requestor_claim_name'])
    define('tenant_claim_name', _config['tenant_claim_name'])
    define('tenant_string_pattern', _config['tenant_string_pattern'])
    define('export_chunk_size', _config['export_chunk_size'])
    define('valid_tenant', re.compile(r'{}'.format(_config['valid_tenant_regex'])))


set_config()


class AuthRequestHandler(RequestHandler):


    """
    All RequestHandler(s), with the exception of the HealthCheckHandler
    inherit from this one, giving them access to the
    process_token_and_extract_claims method.

    The purpose of this method is to allow a measure of authentication
    and authorization - just enough to be able to enforce access control
    on a per request basis, where necessary.

    When called, the method will set the following properties:

        self.jwt
        self.tenant
        self.claims
        self.requestor

    Subsequent request handlers (HTTP method implementations), use these properties
    for request processing, and enforcement of access control where needed.
    The API can be configured to check whether the tenant identifier in the URL
    matches the tenant identifier in the claims. It can also be configured
    to check the token expiry. These are not mandatory.

    To decide what is right for your use case read the module level docstring
    about the different endpoints, and use cases underpinning their design
    and implementation.

    """

    def process_token_and_extract_claims(self, check_tenant=options.check_tenant,
                                         check_exp=options.check_exp,
                                         tenant_claim_name=options.tenant_claim_name):
        """
        When performing requests against the API, JWT access tokens are presented
        in the Authorization header of the HTTP request as a Bearer token. Before
        the body of each request is processed this method is called in 'prepare'.

        The process_access_token method will:
            - extract claims from the JWT
            - OPTIONALLY check for consistent tenant access
            - OPTIONALLY check for token expiry

        The latter checks are OPTIONAL since they SHOULD already have been
        performed by an authorization server _before_ the request is handled here.

        Returns
        -------
        bool or dict

        """
        self.status = None
        try:
            try:
                auth_header = self.request.headers['Authorization']
            except (KeyError, UnboundLocalError) as e:
                self.message = 'Missing authorization header'
                logging.error(self.message)
                self.set_status(400)
                raise Exception('Authorization not possible: missing header')
            try:
                self.jwt = auth_header.split(' ')[1]
            except IndexError as e:
                self.message = 'Malformed authorization header'
                logging.error(self.message)
                self.set_status(400)
                raise Exception('Authorization not possible: malformed header')
            try:
                tenant = tenant_from_url(self.request.uri)
                assert options.valid_tenant.match(tenant)
                self.tenant = tenant
            except AssertionError as e:
                logging.error(e.message)
                logging.error('tenant invalid')
                self.set_status(400)
                raise e
            try:
                authnz = process_access_token(auth_header, tenant,
                                              check_tenant, check_exp,
                                              tenant_claim_name)
                self.claims = authnz['claims']
                self.requestor = self.claims[options.requestor_claim_name]
                if not authnz['status']:
                    self.set_status(401)
                    raise Exception('JWT verification failed')
                elif authnz['status']:
                    return authnz
            except Exception as e:
                self.message = authnz['reason']
                logging.error(e)
                self.set_status(401)
                raise Exception('Authorization failed')
        except Exception as e:
            if not self.status:
                self.set_status(401)
            raise Exception


class FileStreamerHandler(AuthRequestHandler):

    """List the export directory, or serve files from it."""


    def initialize(self, backend):
        try:
            self.CHUNK_SIZE = options.export_chunk_size
            tenant = tenant_from_url(self.request.uri)
            assert options.valid_tenant.match(tenant)
            self.backend_paths = options.config['backends']['disk'][backend]
            self.export_path_pattern = self.backend_paths['export_path']
            self.export_dir = self.export_path_pattern.replace(options.tenant_string_pattern, tenant)
            self.backend = backend
            self.export_policy = options.config['backends']['disk'][backend]['export_policy']
        except (AssertionError, Exception) as e:
            self.backend = None
            logging.error(e)
            logging.error('Maybe the URI does not contain a valid tenant')
            raise e

    def enforce_export_policy(self, policy_config, filename, tenant, size, mime_type):
        """
        Check file to ensure it meets the requirements of the export policy

        Checks
        ------
        1. For all tenants, check that the file name follows conventions
        2. For the given tenant, if a policy is specified and enabled check:
            - file size does not exceed max allowed for export
            - MIME types conform to allowed types, if policy enabled

        Returns
        -------
        (bool, <str,None>, <int,None>),
        (is_conformant, mime-type, size)

        """
        status = False # until proven otherwise
        try:
            file = os.path.basename(filename)
            check_filename(file, disallowed_start_chars=options.start_chars)
        except Exception as e:
            self.message = 'Illegal export filename: %s' % file
            logging.error(self.message)
            return status
        if tenant in policy_config.keys():
            policy = policy_config[tenant]
        else:
            policy = policy_config['default']
        if not policy['enabled']:
            status = True
            return status
        if '*' in policy['allowed_mime_types']:
            status = True
        else:
            status = True if mime_type in policy['allowed_mime_types'] else False
            if not status:
                self.message = 'not allowed to export file with MIME type: %s' % mime_type
                logging.error(self.message)
        if policy['max_size'] and size > policy['max_size']:
            logging.error('%s tried to export a file exceeding the maximum size limit', self.requestor)
            self.message = 'File size exceeds maximum allowed for %s' % tenant
            status = False
        return status


    def get_file_metadata(self, filename):
        filename_raw_utf8 = filename.encode('utf-8')
        if self.backend == 'files':
            # only necessary for export folder
            subprocess.call(['sudo', 'chmod', 'go+r', filename])
        mime_type = magic.from_file(filename_raw_utf8, mime=True)
        size = os.stat(filename).st_size
        return size, mime_type


    def list_files(self, path, tenant):
        """
        Lists files in the export directory.

        Returns
        -------
        dict

        """
        dir_map = os.listdir(path)
        files = list(dir_map)
        if len(files) > options.config['export_max_num_list']:
            self.set_status(400)
            self.message = 'too many files, create a zip archive'
            raise Exception
        times = []
        exportable = []
        reasons = []
        sizes = []
        mimes = []
        owners = []
        for file in files:
            filepath = os.path.normpath(path + '/' + file)
            path_stat = os.stat(filepath)
            latest = path_stat.st_mtime
            owner = pwd.getpwuid(path_stat.st_uid).pw_name
            date_time = str(datetime.datetime.fromtimestamp(latest).isoformat())
            times.append(date_time)
            try:
                if os.path.isdir(filepath):
                    status, mime_type, size = None, None, None
                    self.message = 'exporting from directories not supported yet'
                else:
                    size, mime_type = self.get_file_metadata(filepath)
                    status = self.enforce_export_policy(self.export_policy, filepath, tenant, size, mime_type)
                if status:
                    reason = None
                else:
                    reason = self.message
            except Exception as e:
                logging.error(e)
                logging.error('could not enforce export policy when listing dir')
                raise Exception
            exportable.append(status)
            reasons.append(reason)
            sizes.append(size)
            mimes.append(mime_type)
            owners.append(owner)
        file_info = []
        for f, t, e, r, s, m, o in zip(files, times, exportable, reasons, sizes, mimes, owners):
            href = '%s/%s' % (self.request.uri, url_escape(f))
            file_info.append({'filename': f,
                              'size': s,
                              'modified_date': t,
                              'href': href,
                              'exportable': e,
                              'reason': r,
                              'mime-type': m,
                              'owner': o})
        logging.info('%s listed %s', self.requestor, path)
        self.write({'files': file_info})


    def compute_etag(self):
        """
        If there is a file resource, compute the Etag header.
        Custom values: md5sum of string value of last modified
        time of file. Client can get this value before staring
        a download, and then if they need to resume for some
        reason, check that the resource has not changed in
        the meantime. It is also cheap to compute.

        Note, since this is a strong validator/Etag, nginx will
        strip it from the headers if it has been configured with
        gzip compression for HTTP responses.

        """
        try:
            if self.filepath:
                mtime = os.stat(self.filepath).st_mtime
                etag = hashlib.md5(str(mtime).encode('utf-8')).hexdigest()
                return etag
        except (Exception, AttributeError) as e:
            return None
        else:
            return None


    @gen.coroutine
    def get(self, tenant, filename=None):
        """
        List the export dir, or serve a file, asynchronously.

        1. check token claims
        2. check the tenant

        If listing the dir:

        3. run the list_files method

        If serving a file:

        3. check the filename
        4. check that file exists
        5. enforce the export policy
        6. check if a byte range is being requested
        6. set the mime type
        7. serve the bytes requested (explicitly, or implicitly), chunked

        """
        self.message = 'Unknown error, please contact TSD'
        try:
            try:
                self.authnz = self.process_token_and_extract_claims()
            except Exception:
                if not self.message:
                    self.message = 'Not authorized to export data'
                self.set_status(401)
                raise Exception
            assert options.valid_tenant.match(tenant)
            self.path = self.export_dir
            if not filename:
                self.list_files(self.path, tenant)
                return
            try:
                secured_filename = check_filename(url_unescape(filename),
                                                  disallowed_start_chars=options.start_chars)
            except Exception as e:
                logging.error(e)
                logging.error('%s tried to access files in sub-directories', self.requestor)
                self.set_status(403)
                self.message = 'Not allowed to access files in sub-directories, create a zip archive'
                raise Exception
            self.filepath = '%s/%s' % (self.path, secured_filename)
            if not os.path.lexists(self.filepath):
                logging.error('%s tried to access a file that does not exist', self.requestor)
                self.set_status(404)
                self.message = 'File does not exist'
                raise Exception
            try:
                size, mime_type = self.get_file_metadata(self.filepath)
                status = self.enforce_export_policy(self.export_policy, self.filepath, tenant, size, mime_type)
                assert status
            except (Exception, AssertionError) as e:
                logging.error(e)
                self.set_status(400)
                raise Exception
            self.set_header('Content-Type', mime_type)
            if 'Range' not in self.request.headers:
                self.set_header('Content-Length', size)
                self.flush()
                fd = open(self.filepath, "rb")
                data = fd.read(self.CHUNK_SIZE)
                while data:
                    self.write(data)
                    yield self.flush()
                    data = fd.read(self.CHUNK_SIZE)
                fd.close()
            elif 'Range' in self.request.headers:
                if 'If-Range' in self.request.headers:
                    provided_etag = self.request.headers['If-Range']
                    computed_etag = self.compute_etag()
                    if provided_etag != computed_etag:
                        self.message = 'The resource has changed, get everything from the start again'
                        self.set_status(400)
                        raise Exception(self.message)
                # clients specify the range in terms of 0-based index numbers
                # with an inclusive interval: [start, end]
                client_byte_index_range = self.request.headers['Range']
                full_file_size = os.stat(self.filepath).st_size
                start_and_end = client_byte_index_range.split('=')[-1].split('-')
                if ',' in start_and_end:
                    self.set_status(405)
                    self.message = 'Multipart byte range requests not supported'
                    raise Exception(self.message)
                client_start = int(start_and_end[0])
                cursor_start = client_start
                try:
                    client_end = int(start_and_end[1])
                except Exception as e:
                    client_end = full_file_size - 1
                if client_end > full_file_size:
                    self.set_status(416)
                    raise Exception('Range request exceeds byte range of resource')
                # because clients provide 0-based byte indices
                # we must add 1 to calculate the desired amount to read
                bytes_to_read = client_end - client_start + 1
                self.set_header('Content-Length', bytes_to_read)
                self.flush()
                fd = open(self.filepath, "rb")
                fd.seek(cursor_start)
                sent = 0
                if self.CHUNK_SIZE > bytes_to_read:
                    self.CHUNK_SIZE = bytes_to_read
                data = fd.read(self.CHUNK_SIZE)
                sent = sent + self.CHUNK_SIZE
                while data and sent <= bytes_to_read:
                    self.write(data)
                    yield self.flush()
                    data = fd.read(self.CHUNK_SIZE)
                    sent = sent + self.CHUNK_SIZE
                fd.close()
            logging.info('user: %s, exported file: %s , with MIME type: %s', self.requestor, self.filepath, mime_type)
        except Exception as e:
            logging.error(e)
            logging.error(self.message)
            self.write({'message': self.message})
        finally:
            try:
                fd.close()
            except (OSError, UnboundLocalError) as e:
                pass
            self.finish()


    def head(self, tenant, filename):
        """
        Return information about a specific file.

        """
        self.message = 'Unknown error, please contact TSD'
        try:
            try:
                self.authnz = self.process_token_and_extract_claims()
            except Exception:
                if not self.message:
                    self.message = 'Not authorized to export data'
                self.set_status(401)
                raise Exception
            assert options.valid_tenant.match(tenant)
            self.path = self.export_dir
            if not filename:
                raise Exception('No info to report')
            try:
                secured_filename = check_filename(url_unescape(filename),
                                                  disallowed_start_chars=options.start_chars)
            except Exception as e:
                logging.error(e)
                logging.error('%s tried to access files in sub-directories', self.requestor)
                self.set_status(403)
                self.message = 'Not allowed to access files in sub-directories, create a zip archive'
                raise Exception
            self.filepath = '%s/%s' % (self.path, secured_filename)
            if not os.path.lexists(self.filepath):
                logging.error(self.filepath)
                logging.error('%s tried to access a file that does not exist', self.requestor)
                self.set_status(404)
                self.message = 'File does not exist'
                raise Exception
            size, mime_type = self.get_file_metadata(self.filepath)
            status = self.enforce_export_policy(self.export_policy, self.filepath, tenant, size, mime_type)
            assert status
            logging.info('user: %s, checked file: %s , with MIME type: %s', self.requestor, self.filepath, mime_type)
            self.set_header('Content-Length', size)
            self.set_header('Accept-Ranges', 'bytes')
            self.set_status(200)
        except Exception as e:
            logging.error(e)
            logging.error(self.message)
            self.write({'message': self.message})
        finally:
            self.finish()


class GenericFormDataHandler(AuthRequestHandler):

    def initialize(self, backend):
        try:
            tenant = tenant_from_url(self.request.uri)
            assert options.valid_tenant.match(tenant)
            self.tenant_dir_pattern = options.config['backends']['disk'][backend]['import_path']
            self.tsd_hidden_folder = None
            self.backend = backend
            if backend == 'sns': # hope to deprecate this with new nettskjema integration
                self.tsd_hidden_folder_pattern = options.config['backends']['disk'][backend]['subfolder_path']
        except (Exception, AssertionError) as e:
            logging.error('could not initalize form data handler')

    def prepare(self):
        try:
            self.authnz = self.process_token_and_extract_claims()
            if not self.authnz:
                self.set_status(401)
                raise Exception
            if not self.request.files['file']:
                logging.error('No file(s) supplied with upload request')
                self.set_status(400)
                raise Exception
        except Exception as e:
            if self._status_code != 401:
                self.set_status(400)
            self.finish({'message': 'request failed'})

    def write_files(self, filemode, tenant):
        try:
            for i in range(len(self.request.files['file'])):
                filename = check_filename(self.request.files['file'][i]['filename'],
                                          disallowed_start_chars=options.start_chars)
                filebody = self.request.files['file'][i]['body']
                if len(filebody) == 0:
                    logging.error('Trying to upload an empty file: %s - not allowed, since nonsensical', filename)
                    raise Exception('EmptyFileBodyError')
                # add all optional parameters to file writer
                # this is used for nettskjema specific backend
                written = self.write_file(filemode, filename, filebody, tenant)
                assert written
            return True
        except Exception as e:
            logging.error(e)
            logging.error('Could not process files')
            return False


    def write_file(self, filemode, filename, filebody, tenant):
        try:
            if self.backend == 'sns':
                tsd_hidden_folder = sns_dir(self.tsd_hidden_folder_pattern, tenant, self.request.uri, options.tenant_string_pattern)
                tenant_dir = sns_dir(self.tenant_dir_pattern, tenant, self.request.uri, options.tenant_string_pattern)
            else:
                tenant_dir = self.tenant_dir_pattern.replace(options.tenant_string_pattern, tenant)
            self.path = os.path.normpath(tenant_dir + '/' + filename)
            # add the partial file indicator, check existence
            self.path_part = self.path + '.' + str(uuid4()) + '.part'
            if os.path.lexists(self.path_part):
                logging.error('trying to write to partial file - killing request')
                raise Exception
            if os.path.lexists(self.path):
                logging.info('%s already exists, renaming to %s', self.path, self.path_part)
                os.rename(self.path, self.path_part)
                assert os.path.lexists(self.path_part)
                assert not os.path.lexists(self.path)
            self.path, self.path_part = self.path_part, self.path
            with open(self.path, filemode) as f:
                f.write(filebody)
                os.rename(self.path, self.path_part)
                os.chmod(self.path_part, _RW_RW___)
            if self.backend == 'sns':
                subfolder_path = os.path.normpath(tsd_hidden_folder + '/' + filename)
                try:
                    shutil.copy(self.path_part, subfolder_path)
                    os.chmod(subfolder_path, _RW_RW___)
                except Exception as e:
                    logging.error(e)
                    logging.error('Could not copy file %s to .tsd folder', self.path_part)
                    return False
            return True
        except (Exception, AssertionError) as e:
            logging.error(e)
            logging.error('Could not write to file')
            return False


class FormDataHandler(GenericFormDataHandler):

    def handle_data(self, filemode, tenant):
        try:
            assert self.write_files(filemode, tenant)
            self.set_status(201)
            self.write({'message': 'data uploaded'})
        except Exception:
            self.set_status(400)
            self.write({'message': 'could not upload data'})

    def post(self, tenant):
        self.handle_data('ab+', tenant)

    def patch(self, tenant):
        self.handle_data('ab+', tenant)

    def put(self, tenant):
        self.handle_data('wb+', tenant)

    def head(self, tenant):
        self.set_status(201)


class SnsFormDataHandler(GenericFormDataHandler):

    def handle_data(self, filemode, tenant):
        try:
            assert self.write_files(filemode, tenant)
            self.set_status(201)
            self.write({'message': 'data uploaded'})
        except Exception:
            self.set_status(400)
            self.write({'message': 'could not upload data'})

    def post(self, tenant, keyid, formid):
        self.handle_data('ab+', tenant)

    def patch(self, tenant, keyid, formid):
        self.handle_data('ab+', tenant)

    def put(self, tenant, keyid, formid):
        self.handle_data('wb+', tenant)

    def head(self, tenant, keyid, formid):
        self.set_status(201)


class ResumablesHandler(AuthRequestHandler):

    """
    Manage resumables, report information.

    Implementation
    --------------
    To continue a resumable upload which has stopped, clients
    can get information about data stored by the server.

    This class provides a GET method, which returns the relevant
    information to the client, for a given upload_id, and/or file.

    This information is:

    - filename
    - sequence number of last chunk
    - chunk size
    - upload id
    - md5sum of the last chunk
    - previos offset in bytes
    - next offset in bytes (total size of merged file)

    There are two possible scenarios: 1) the client knows the upload_id
    associated with the file which needs to be resumed, or 2) the client
    only knows the name of the file which needs to be resumed.

    Scenario 1
    ----------
    In scenario #1, the upload_id is provided, and the server returns
    the information.

    Scenario 2
    ----------
    In scenario #2, the server will look at all potential resumables,
    and try to find a match based on the filename. All relevant matches
    to which the authenticated user has access are returned, including
    information about how much data has been uploaded. The client can
    then choose to resume the one with the most data, and delete the
    remaining ones.

    """

    def initialize(self, backend):
        try:
            tenant = tenant_from_url(self.request.uri)
            assert options.valid_tenant.match(tenant)
            # can deprecate once rsync is in place for cluster software install
            key = 'admin_path' if (backend == 'cluster' and tenant == 'p01') else 'import_path'
            self.import_dir = options.config['backends']['disk']['files'][key]
            if backend == 'cluster' and tenant != 'p01':
                assert create_cluster_dir_if_not_exists(self.import_dir, tenant, options.tenant_string_pattern)
            self.tenant_dir = self.import_dir.replace(options.tenant_string_pattern, tenant)
        except AssertionError as e:
            raise e


    def prepare(self):
        try:
            self.authnz = self.process_token_and_extract_claims()
        except Exception as e:
            logging.error(e)
            raise e


    def get(self, tenant, filename=None):
        self.message = {'filename': filename, 'id': None, 'chunk_size': None, 'max_chunk': None}
        upload_id = None
        try:
            try:
                if filename:
                    secured_filename = check_filename(url_unescape(filename),
                                                      disallowed_start_chars=options.start_chars)
            except Exception:
                logging.error('not able to check for resumable due to bad input')
                raise Exception
            try:
                upload_id = url_unescape(self.get_query_argument('id'))
            except Exception:
                pass
            res = SerialResumable(self.tenant_dir, self.requestor)
            if not filename:
                info = res.list_all(self.tenant_dir, self.requestor)
            else:
                info = res.info(self.tenant_dir, secured_filename, upload_id, self.requestor)
            self.set_status(200)
            self.write(info)
        except Exception as e:
            logging.error(e)
            self.set_status(400)
            self.write(self.message)


    def delete(self, tenant, filename):
        self.message = {'message': 'cannot delete resumable'}
        try:
            try:
                secured_filename = check_filename(url_unescape(filename),
                                                  disallowed_start_chars=options.start_chars)
            except Exception:
                logging.error('not able to check for resumable due to bad input')
                raise Exception
            try:
                upload_id = url_unescape(self.get_query_argument('id'))
            except Exception:
                raise Exception('upload id required to delete resumable')
            res = SerialResumable(self.tenant_dir, self.requestor)
            assert res.delete(self.tenant_dir, filename, upload_id, self.requestor)
            self.set_status(200)
            self.write({'message': 'resumable deleted'})
        except Exception as e:
            logging.error(e)
            self.set_status(400)
            self.write(self.message)


@stream_request_body
class StreamHandler(AuthRequestHandler):

    #pylint: disable=line-too-long

    """
    This class writes request data to files for PUT, POST and PATCH methods,
    optionally calling request hooks after writing has finished.

    The following steps are performed:

    call initialize:
    1. load backend config, depending on the url

    call prepare
    2. extract claims from the access token
    3. validate url
    4. load url parameters
    5. process content-type header
    6. if PATCH, prepare the resumable
    7. rename the target file to .part (indicating an active upload)
    8. optionally dispatch to a custom content-type request handler
    9. open the file, set file permissions

    call data_received
    10. write data to target


    call put, post, patch
    11. close the file
    12. if PATCH, either merge the new chunk or finalise the resumable

    call on_finish, or on_connection_close
    13. rename the file
    14. optionally call a request hook (setting permissions and moving the file)

    Resumable uploads therefore have the following request life cycle:
    1. prepare
        - setup
        - checks
    2. process
        - open
        - write
        - close
    3. complete
        - merge chunk
        - finalise

    """

    def decrypt_aes_key(self, b64encoded_pgpencrypted_key):
        gpg = _import_keys(options.config)
        key = base64.b64decode(b64encoded_pgpencrypted_key)
        decr_aes_key = str(gpg.decrypt(key)).strip()
        return decr_aes_key


    def start_openssl_proc(self, output_file=None, base64=True):
        cmd = ['openssl', 'enc', '-aes-256-cbc', '-d'] + self.aes_decryption_args_from_headers()
        if output_file is not None:
            cmd = cmd + ['-out', output_file]
        if base64:
            cmd = cmd + ['-a']
        return subprocess.Popen(cmd,
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE if output_file is None else None)


    def aes_decryption_args_from_headers(self):
        try:
            decr_aes_key = self.decrypt_aes_key(self.request.headers['Aes-Key'])
        except Exception as e:
            logging.error(e)
        if "Aes-Iv" in self.request.headers:
            return ['-iv', self.request.headers["Aes-Iv"], '-K', decr_aes_key]
        else:
            return ['-pass', 'pass:%s' % decr_aes_key]


    def handle_aes(self, content_type):
        self.custom_content_type = content_type
        self.proc = self.start_openssl_proc(output_file=self.path)


    def handle_aes_octet_stream(self, content_type):
        self.custom_content_type = 'application/aes'
        self.proc = self.start_openssl_proc(output_file=self.path, base64=False)


    def handle_tar(self, content_type, tenant_dir):
        if 'gz' in content_type:
            tarflags = '-xzf'
        else:
            tarflags = '-xf'
        self.custom_content_type = content_type
        self.proc = subprocess.Popen(['tar', '-C', tenant_dir, tarflags, '-'],
                                      stdin=subprocess.PIPE)


    def handle_tar_aes(self, content_type, tenant_dir):
        if 'gz' in content_type:
            tarflags = '-xzf'
        else:
            tarflags = '-xf'
        self.custom_content_type = content_type
        self.openssl_proc = self.start_openssl_proc()
        self.tar_proc = subprocess.Popen(['tar', '-C', tenant_dir, tarflags, '-'],
                                 stdin=self.openssl_proc.stdout)


    def handle_gz(self, content_type, filemode):
        self.custom_content_type = content_type
        self.target_file = open(self.path, filemode)
        self.gunzip_proc = subprocess.Popen(['gunzip', '-c', '-'],
                                             stdin=subprocess.PIPE,
                                             stdout=self.target_file)


    def handle_gz_aes(self, content_type, filemode):
        self.custom_content_type = content_type
        self.target_file = open(self.path, filemode)
        self.openssl_proc = self.start_openssl_proc()
        self.gunzip_proc = subprocess.Popen(['gunzip', '-c', '-'],
                                             stdin=self.openssl_proc.stdout,
                                             stdout=self.target_file)


    def initialize(self, backend, request_hook_enabled=False):
        try:
            tenant = tenant_from_url(self.request.uri)
            assert options.valid_tenant.match(tenant)
            key = 'admin_path' if (backend == 'cluster' and tenant == 'p01') else 'import_path'
            self.import_dir = options.config['backends']['disk'][backend][key]
            if backend == 'cluster' and tenant != 'p01':
                assert create_cluster_dir_if_not_exists(self.import_dir, tenant, options.tenant_string_pattern)
            self.tenant_dir = self.import_dir.replace(options.tenant_string_pattern, tenant)
            self.backend = backend
            self.request_hook_enabled = request_hook_enabled
            if request_hook_enabled:
                self.hook_path = options.config['backends']['disk'][backend]['request_hook']['path']
                self.hook_sudo = options.config['backends']['disk'][backend]['request_hook']['sudo']
        except AssertionError as e:
            self.backend = backend
            logging.error('URI does not contain a valid tenant')
            raise e


    @gen.coroutine
    def prepare(self):
        """
        This sets up state for the part of the request handler which writes data to disk.

        - authorization
        - input validation
        - file write mode
        - content-type processing
        - filename construction
        - calling request-specific handlers
        - ending requests which do not meet criteria

        """
        try:
            self.completed_resumable_file = False
            self.target_file = None
            self.custom_content_type = None
            self.path = None
            self.path_part = None
            self.chunk_order_correct = True
            filemodes = {'POST': 'ab+', 'PUT': 'wb+', 'PATCH': 'wb+'}
            try:
                self.authnz = self.process_token_and_extract_claims()
            except Exception as e:
                logging.error(e)
                raise Exception
            try:
                try:
                    tenant = tenant_from_url(self.request.uri)
                    self.tenant = tenant
                    assert options.valid_tenant.match(tenant)
                except AssertionError as e:
                    logging.error('URI does not contain a valid tenant')
                    raise e
                try:
                    self.group_name = url_unescape(self.get_query_argument('group'))
                except Exception:
                    self.group_name = tenant + '-member-group'
                filemode = filemodes[self.request.method]
                try:
                    content_type = self.request.headers['Content-Type']
                    uri_filename = self.request.uri.split('?')[0].split('/')[-1]
                    filename = check_filename(url_unescape(uri_filename),
                                              disallowed_start_chars=options.start_chars)
                    if self.request.method == 'PATCH':
                        self.res = SerialResumable(self.tenant_dir, self.requestor)
                        url_chunk_num = url_unescape(self.get_query_argument('chunk'))
                        url_upload_id = url_unescape(self.get_query_argument('id'))
                        url_group = url_unescape(self.get_query_argument('group'))
                        self.chunk_num, \
                            self.upload_id, \
                            self.completed_resumable_file, \
                            self.chunk_order_correct, \
                            filename = self.res.prepare(self.tenant_dir, filename,
                                                         url_chunk_num, url_upload_id,
                                                         url_group, self.requestor)
                        if not self.chunk_order_correct:
                            raise Exception
                    # ensure we do not write to active file
                    self.path = os.path.normpath(self.tenant_dir + '/' + filename)
                    self.path_part = self.path + '.' + str(uuid4()) + '.part'
                    if os.path.lexists(self.path_part):
                        logging.error('trying to write to partial file - killing request')
                        raise Exception
                    if os.path.lexists(self.path):
                        if os.path.isdir(self.path):
                            logging.info('directory: %s already exists due to prior upload, removing', self.path)
                            shutil.rmtree(self.path)
                        else:
                            logging.info('%s already exists, renaming to %s', self.path, self.path_part)
                            os.rename(self.path, self.path_part)
                            assert os.path.lexists(self.path_part)
                            assert not os.path.lexists(self.path)
                    self.path, self.path_part = self.path_part, self.path
                    if content_type == 'application/aes':
                        self.handle_aes(content_type)
                    elif content_type == 'application/aes-octet-stream':
                        self.handle_aes_octet_stream(content_type)
                    elif content_type in ['application/tar', 'application/tar.gz']:
                        self.handle_tar(content_type, self.tenant_dir)
                    elif content_type in ['application/tar.aes', 'application/tar.gz.aes']:
                        self.handle_tar_aes(content_type, self.tenant_dir)
                    elif content_type == 'application/gz':
                        self.handle_gz(content_type, filemode)
                    elif content_type == 'application/gz.aes':
                        self.handle_gz_aes(content_type, filemode)
                    else: # no custom content type
                        if self.request.method != 'PATCH':
                            self.custom_content_type = None
                            self.target_file = open(self.path, filemode)
                            os.chmod(self.path, _RW______)
                        elif self.request.method == 'PATCH':
                            self.custom_content_type = None
                            if not self.completed_resumable_file:
                                self.target_file = self.res.open_file(self.path, filemode)
                except KeyError:
                    raise Exception('No content-type - do not know what to do with data')
            except Exception as e:
                logging.error(e)
                try:
                    if self.target_file:
                        self.target_file.close()
                    os.rename(self.path, self.path_part)
                except AttributeError as e:
                    logging.error(e)
                    logging.error('No file to close after all - so nothing to worry about')
        except Exception as e:
            logging.error('stream handler failed')
            info = 'stream processing failed'
            if self.chunk_order_correct is False:
                self.set_status(200)
                info = 'chunk_order_incorrect'
            self.finish({'message': info})


    @gen.coroutine
    def data_received(self, chunk):
        try:
            if not self.custom_content_type:
                if self.request.method == 'PATCH':
                    self.res.add_chunk(self.target_file, chunk)
                else:
                    self.target_file.write(chunk)
            elif self.custom_content_type in ['application/tar', 'application/tar.gz',
                                              'application/aes']:
                self.proc.stdin.write(chunk)
                if not chunk:
                    self.proc.stdin.flush()
            elif self.custom_content_type in ['application/tar.aes', 'application/tar.gz.aes']:
                self.openssl_proc.stdin.write(chunk)
                if not chunk:
                    self.openssl_proc.stdin.flush()
                    self.tar_proc.stdin.flush()
            elif self.custom_content_type == 'application/gz':
                self.gunzip_proc.stdin.write(chunk)
                if not chunk:
                    self.gunzip_proc.stdin.flush()
            elif self.custom_content_type == 'application/gz.aes':
                self.openssl_proc.stdin.write(chunk)
                if not chunk:
                    self.openssl_proc.stdin.flush()
                    self.gunzip_proc.stdin.flush()
        except Exception as e:
            logging.error(e)
            logging.error("something went wrong with stream processing have to close file")
            if self.target_file:
                self.target_file.close()
            os.rename(self.path, self.path_part)
            self.send_error("something went wrong")


    # TODO: check for errors
    def post(self, tenant, uri_filename=None):
        if not self.custom_content_type:
            self.target_file.close()
            os.rename(self.path, self.path_part)
        elif self.custom_content_type in ['application/tar', 'application/tar.gz',
                                          'application/aes']:
            out, err = self.proc.communicate()
            if self.custom_content_type == 'application/aes':
                os.rename(self.path, self.path_part)
        elif self.custom_content_type in ['application/tar.aes', 'application/tar.gz.aes']:
            out, err = self.openssl_proc.communicate()
            out, err = self.tar_proc.communicate()
        elif self.custom_content_type == 'application/gz':
            out, err = self.gunzip_proc.communicate()
            self.target_file.close()
            os.rename(self.path, self.path_part)
        elif self.custom_content_type == 'application/gz.aes':
            out, err = self.openssl_proc.communicate()
            out, err = self.gunzip_proc.communicate()
            self.target_file.close()
            os.rename(self.path, self.path_part)
        self.set_status(201)
        self.write({'message': 'data streamed'})


    # TODO: check for errors
    def put(self, tenant, uri_filename=None):
        if not self.custom_content_type:
            self.target_file.close()
            os.rename(self.path, self.path_part)
        elif self.custom_content_type in ['application/tar', 'application/tar.gz',
                                          'application/aes']:
            out, err = self.proc.communicate()
            if self.custom_content_type == 'application/aes':
                os.rename(self.path, self.path_part)
        elif self.custom_content_type in ['application/tar.aes', 'application/tar.gz.aes']:
            out, err = self.openssl_proc.communicate()
            out, err = self.tar_proc.communicate()
        elif self.custom_content_type == 'application/gz':
            out, err = self.gunzip_proc.communicate()
            self.target_file.close()
            os.rename(self.path, self.path_part)
        elif self.custom_content_type == 'application/gz.aes':
            out, err = self.openssl_proc.communicate()
            out, err = self.gunzip_proc.communicate()
            self.target_file.close()
            os.rename(self.path, self.path_part)
        self.set_status(201)
        self.write({'message': 'data streamed'})


    def patch(self, tenant, uri_filename=None):
        if not self.completed_resumable_file:
            self.res.close_file(self.target_file)
            # if the path to which we want to rename the file exists
            # then we have been writing the same chunk concurrently
            # from two different processes, so we should not do it
            if not os.path.lexists(self.path_part):
                os.rename(self.path, self.path_part)
                filename = os.path.basename(self.path_part).split('.chunk')[0]
                self.res.merge_chunk(self.tenant_dir, os.path.basename(self.path_part), self.upload_id, self.requestor)
            else:
                self.write({'message': 'chunk_order_incorrect'})
        else:
            self.completed_resumable_filename = self.res.finalise(self.tenant_dir, os.path.basename(self.path_part),
                                                                   self.upload_id, self.requestor)
            filename = os.path.basename(self.completed_resumable_filename)
        self.set_status(201)
        self.write({'filename': filename, 'id': self.upload_id, 'max_chunk': self.chunk_num})


    def head(self, tenant, uri_filename=None):
        self.set_status(201)


    def on_finish(self):
        """
        Called after each request at the very end before closing the connection.

        - clean up any open files if an error occurred
        - call the request hook, if configured

        """
        try:
            if not self.target_file.closed:
                self.target_file.close()
                os.rename(self.path, self.path_part)
        except AttributeError as e:
            pass
        if self.request.method in ('PUT','POST') or (self.request.method == 'PATCH' and self.chunk_num == 'end'):
            try:
                # switch path and path_part variables back to their original values
                # keep local copies in this scope for safety
                if not self.completed_resumable_file:
                    path, path_part = self.path_part, self.path
                else:
                    path = self.completed_resumable_filename
                if self.backend == 'cluster' and self.tenant != 'p01': # TODO: remove special case
                    pass
                else:
                    if self.request_hook_enabled:
                        call_request_hook(self.hook_path,
                                          [path, self.requestor, options.api_user, self.group_name],
                                          as_sudo=self.hook_sudo)
            except Exception as e:
                logging.info('could not change file mode or owner for some reason')
                logging.info(e)


    def on_connection_close(self):
        """
        Called when clients close the connection. Clean up any open files.

        """
        try:
            if not self.target_file.closed:
                self.target_file.close()
                path = self.path
                if self.backend == 'cluster' and tenant != 'p01':  # TODO: remove special case
                    pass
                else:
                    if self.request_hook_enabled:
                        call_request_hook(self.hook_path,
                                          [path, self.requestor, options.api_user, self.group_name],
                                          as_sudo=self.hook_sudo)
                logging.info('StreamHandler: Closed file after client closed connection')
        except AttributeError as e:
            logging.info(e)
            logging.info('There was no open file to close or move')


@stream_request_body
class ProxyHandler(AuthRequestHandler):

    def initialize(self, backend):
        self.storage_backend = backend

    @gen.coroutine
    def prepare(self):
        """Called after headers have been read."""
        try:
            # 1. Set up internal variables
            try:
                self.chunks = tornado.queues.Queue(1)
                if self.request.method == 'HEAD':
                    body = None
                else:
                    body = self.body_producer
            except Exception as e:
                logging.error('Could not set up internal async variables')
                raise e
            # 2. Authentication and authorization
            try:
                authnz_status = self.process_token_and_extract_claims()
            except Exception as e:
                logging.error('Access token invalid')
                raise e
            # 3. Validate tenant number in URI
            try:
                tenant = tenant_from_url(self.request.uri)
                assert options.valid_tenant.match(tenant)
            except AssertionError as e:
                logging.error('URI does not contain a valid tenant')
                raise e
            # 4. Set the filename
            try:
                uri = self.request.uri
                uri_parts = uri.split('/')
                if len(uri_parts) >= 6:
                    basename = uri_parts[-1]
                    filename = basename.split('?')[0]
                    self.filename = check_filename(url_unescape(filename),
                                                   disallowed_start_chars=options.start_chars)
                else:
                    # TODO: deprecate this once transitioned to URI scheme
                    self.filename = check_filename(self.request.headers['Filename'],
                                                   disallowed_start_chars=options.start_chars)
            except KeyError:
                self.filename = datetime.datetime.now().isoformat() + '.txt'
                logging.info("filename not found - setting filename to: %s", self.filename)
            # 5. Validate group name
            try:
                group_memberships = authnz_status['claims']['groups']
                try:
                    group_name = url_unescape(self.get_query_argument('group'))
                except Exception as e:
                    logging.info('no group specified - choosing default: member-group')
                    group_name = tenant + '-member-group'
            except Exception as e:
                logging.info('Could not get group info from JWT - choosing default: member-group')
                # this happens with basic auth, anonymous end-users
                # then we only allow upload the member group
                group_name = tenant + '-member-group'
                group_memberships = [group_name]
            try:
                assert IS_VALID_GROUPNAME.match(group_name)
            except AssertionError as e:
                logging.error('invalid group name: %s', group_name)
                raise e
            try:
                assert tenant == group_name.split('-')[0]
            except AssertionError as e:
                logging.error('tenant in url: %s and group name: %s do not match', self.request.uri, group_name)
                raise e
            try:
                assert group_name in group_memberships
            except AssertionError as e:
               logging.error('user not member of group')
               raise e
            # 6. Set headers for internal request
            try:
                if 'Content-Type' not in self.request.headers.keys():
                    content_type = 'application/octet-stream'
                elif 'Content-Type' in self.request.headers.keys():
                    content_type = self.request.headers['Content-Type']
                headers = {'Authorization': 'Bearer ' + self.jwt,
                           'Content-Type': content_type}
                if 'Aes-Key' in self.request.headers.keys():
                    headers['Aes-Key'] = self.request.headers['Aes-Key']
                if 'Aes-Iv' in self.request.headers.keys():
                    headers['Aes-Iv'] = self.request.headers['Aes-Iv']
                if 'Pragma' in self.request.headers.keys():
                    headers['Pragma'] = self.request.headers['Pragma']
            except Exception as e:
                logging.error('Could not prepare headers for async request handling')
                raise e
            # 7. Build URL
            try:
                upload_id, chunk_num = None, None
                chunk_num = url_unescape(self.get_query_argument('chunk'))
                upload_id = url_unescape(self.get_query_argument('id'))
            except Exception:
                pass
            params = '?group=%s&chunk=%s&id=%s' % (group_name, chunk_num, upload_id)
            filename = url_escape(self.filename)
            internal_url = 'http://localhost:%d/v1/%s/%s/upload_stream/%s%s' % \
                (options.port, tenant, self.storage_backend, filename, params)
            # 8. Do async request to handle incoming data
            try:
                self.fetch_future = AsyncHTTPClient().fetch(
                    internal_url,
                    method=self.request.method,
                    body_producer=body,
                    request_timeout=12000.0, # 3 hours max
                    headers=headers)
            except Exception as e:
                logging.error('Problem in async client')
                logging.error(e)
                raise e
        except Exception as e:
            self.set_status(401)
            self.finish({'message': 'Request failed'})

    @gen.coroutine
    def body_producer(self, write):
        while True:
            chunk = yield self.chunks.get()
            if chunk is None:
                return
            yield write(chunk)

    @gen.coroutine
    def data_received(self, chunk):
        yield self.chunks.put(chunk)

    @gen.coroutine
    def post(self, tenant, filename=None):
        """Called after entire body has been read."""
        yield self.chunks.put(None)
        # wait for request to finish.
        response = yield self.fetch_future
        self.set_status(response.code)
        self.write(response.body)

    @gen.coroutine
    def put(self, tenant, filename=None):
        """Called after entire body has been read."""
        yield self.chunks.put(None)
        # wait for request to finish.
        response = yield self.fetch_future
        self.set_status(response.code)
        self.write(response.body)

    @gen.coroutine
    def patch(self, tenant, filename=None):
        """Called after entire body has been read."""
        yield self.chunks.put(None)
        # wait for request to finish.
        response = yield self.fetch_future
        code = response.code
        body = response.body
        try:
            resp = json.loads(response.body)
            if resp['message'] == 'chunk_order_incorrect':
                code = 400
                body = resp
        except Exception:
            pass
        self.set_status(code)
        self.write(body)

    def head(self, tenant, filename=None):
        self.set_status(201)


class GenericTableHandler(AuthRequestHandler):

    """
    Manage data in generic sqlite backend.

    Maybe:
    - GET Accept: application/sqlite
    - POST Content-Type: text/csv -> "concatenate"

    """

    def initialize(self, app):
        self.app = app
        self.db_name =  '.' + app + '.db'
        if 'metadata' in self.request.uri:
            self.datatype = 'metadata'
        else:
            self.datatype = 'data'
        tenant = tenant_from_url(self.request.uri)
        assert options.valid_tenant.match(tenant)
        self.import_dir = options.config['backends']['sqlite'][app]['db_path']
        self.tenant_dir = self.import_dir.replace(options.tenant_string_pattern, tenant)


    def get(self, tenant, table_name=None):
        try:
            if not table_name:
                self.authnz = self.process_token_and_extract_claims()
                engine = sqlite_init(self.tenant_dir, name=self.db_name)
                tables = sqlite_list_tables(engine)
                self.set_status(200)
                self.write({'tables': tables})
            else:
                self.authnz = self.process_token_and_extract_claims()
                engine = sqlite_init(self.tenant_dir, name=self.db_name, builtin=True)
                data = sqlite_get_data(engine, table_name, self.request.uri)
                if 'Accept' in self.request.headers:
                    if self.request.headers['Accept'] == 'text/csv':
                        df = DataFrame()
                        df = df.from_records(data)
                        data = df.to_csv(None, sep=',', index=False)
                        self.set_status(200)
                        self.write(data)
                    else:
                        self.set_status(200)
                        self.write({'data': data})
                else:
                    self.set_status(200)
                    self.write({'data': data})
        except Exception as e:
            logging.error(e)
            self.set_status(400)
            self.write({'message': 'error'})


    def put(self, tenant, table_name):
        try:
            self.authnz = self.process_token_and_extract_claims()
            data = json_decode(self.request.body)
            try:
                engine = sqlite_init(self.tenant_dir, name=self.db_name)
                sqlite_insert(engine, table_name, data)
                os.chmod(self.tenant_dir + '/' + self.db_name, _RW______)
                self.set_status(201)
                self.write({'message': 'data stored'})
            except Exception as e:
                logging.error(e)
                raise Exception
        except Exception as e:
            logging.error(e)
            self.set_status(400)
            self.write({'message': 'error'})


    def patch(self, tenant, table_name):
        try:
            self.authnz = self.process_token_and_extract_claims()
            engine = sqlite_init(self.tenant_dir, name=self.db_name, builtin=True)
            data = sqlite_update_data(engine, table_name, self.request.uri)
            self.set_status(200)
            self.write({'data': data})
        except Exception as e:
            logging.error(e)
            self.set_status(400)
            self.write({'message': 'error'})


    def delete(self, tenant, table_name):
        try:
            self.authnz = self.process_token_and_extract_claims()
            engine = sqlite_init(self.tenant_dir, name=self.db_name, builtin=True)
            data = sqlite_delete_data(engine, table_name, self.request.uri)
            self.set_status(200)
            self.write({'data': data})
        except Exception as e:
            logging.error(e)
            self.set_status(400)
            self.write({'message': 'error'})


class HealthCheckHandler(RequestHandler):

    def head(self, tenant):
        self.set_status(200)
        self.write({'message': 'healthy'})


def main():
    parse_command_line()
    app = Application([
        # Note: the name of the storage backend is the same as the URL fragment
        ('/v1/(.*)/files/health', HealthCheckHandler),
        # hpc storage
        ('/v1/(.*)/cluster/upload_stream', StreamHandler, dict(backend='cluster', request_hook_enabled=True)),
        ('/v1/(.*)/cluster/upload_stream/(.*)', StreamHandler, dict(backend='cluster', request_hook_enabled=True)),
        ('/v1/(.*)/cluster/stream', ProxyHandler, dict(backend='cluster')),
        ('/v1/(.*)/cluster/stream/(.*)', ProxyHandler, dict(backend='cluster')),
        ('/v1/(.*)/cluster/resumables', ResumablesHandler, dict(backend='cluster')),
        ('/v1/(.*)/cluster/resumables/(.*)', ResumablesHandler, dict(backend='cluster')),
        ('/v1/(.*)/cluster/export', FileStreamerHandler, dict(backend='cluster')),
        ('/v1/(.*)/cluster/export/(.*)', FileStreamerHandler, dict(backend='cluster')),
        # project storage
        ('/v1/(.*)/files/upload_stream', StreamHandler, dict(backend='files', request_hook_enabled=True)),
        ('/v1/(.*)/files/upload_stream/(.*)', StreamHandler, dict(backend='files', request_hook_enabled=True)),
        ('/v1/(.*)/files/stream', ProxyHandler, dict(backend='files')),
        ('/v1/(.*)/files/stream/(.*)', ProxyHandler, dict(backend='files')),
        ('/v1/(.*)/files/resumables', ResumablesHandler, dict(backend='files')),
        ('/v1/(.*)/files/resumables/(.*)', ResumablesHandler, dict(backend='files')),
        ('/v1/(.*)/files/export', FileStreamerHandler, dict(backend='files')),
        ('/v1/(.*)/files/export/(.*)', FileStreamerHandler, dict(backend='files')),
        # sqlite backend
        ('/v1/(.*)/tables/generic/metadata/(.*)', GenericTableHandler, dict(app='generic')),
        ('/v1/(.*)/tables/generic/(.*)', GenericTableHandler, dict(app='generic')),
        ('/v1/(.*)/tables/generic', GenericTableHandler, dict(app='generic')),
        ('/v1/(.*)/tables/survey/metadata/(.*)', GenericTableHandler, dict(app='survey')),
        ('/v1/(.*)/tables/survey/(.*)', GenericTableHandler, dict(app='survey')),
        ('/v1/(.*)/tables/survey', GenericTableHandler, dict(app='survey')),
        # form data
        ('/v1/(.*)/files/upload', FormDataHandler, dict(backend='form_data')),
        ('/v1/(.*)/sns/(.*)/(.*)', SnsFormDataHandler, dict(backend='sns')),
        # store system
        ('/v1/(.*)/store/upload_stream', StreamHandler, dict(backend='store', request_hook_enabled=False)),
        ('/v1/(.*)/store/upload_stream/(.*)', StreamHandler, dict(backend='store', request_hook_enabled=False)),
        ('/v1/(.*)/store/import', ProxyHandler, dict(backend='store')),
        ('/v1/(.*)/store/import/(.*)', ProxyHandler, dict(backend='store')),
        ('/v1/(.*)/store/resumables', ResumablesHandler, dict(backend='store')),
        ('/v1/(.*)/store/resumables/(.*)', ResumablesHandler, dict(backend='store')),
        ('/v1/(.*)/store/export', FileStreamerHandler, dict(backend='store')),
        ('/v1/(.*)/store/export/(.*)', FileStreamerHandler, dict(backend='store')),
    ], debug=options.debug)
    app.listen(options.port)
    IOLoop.instance().start()


if __name__ == '__main__':
    main()
