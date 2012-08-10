#
# Copyright (c) 2010-2012 Liraz Siri <liraz@turnkeylinux.org>
# Copyright (c) 2010 Alon Swartz <alon@turnkeylinux.org>
#
# This file is part of TKLBAM (TurnKey Linux BAckup and Migration).
#
# TKLBAM is open source software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of
# the License, or (at your option) any later version.
#
"""TurnKey Hub API - Backup

Notes:
    - Default URL: https://hub.turnkeylinux.org/api/backup/
    - REST compliant (GET, POST, PUT)
    - Responses are returned in application/json format
    - API subkey must be sent in the header for all calls (except subkey/)

subkey/
    method: GET
    fields: apikey
    return: subkey

credentials/
    method: GET
    fields:
    return: accesskey, secretkey, usertoken, producttoken

record/create/
    method: POST
    fields: key, turnkey_version, [server_id]
    return: backuprecord

record/update/
    method: PUT
    fields: address
    return: <response_code>

record/<backup_id>/
    method: GET
    fields:
    return: backuprecord

record/<backup_id>/
    method: PUT
    fields: key
    return: backuprecord

records/
    method: GET
    fields:
    return: [ backuprecord, ... ]

archive/
    method: GET
    fields: turnkey_version
    return: archive_content

archive/timestamp/
    method: GET
    fields: turnkey_version
    return: archive_timestamp

Exceptions::

    400 Request.MissingHeader
    400 Request.MissingArgument
    401 HubAccount.Forbidden
    400 HubAccount.InvalidApiKey
    400 BackupAccount.InvalidSubKey
    401 BackupAccount.MalformedSubKey
    404 BackupAccount.NotFound
    401 BackupAccount.NotSubscribed
    404 BackupRecord.NotFound
    401 BackupRecord.LimitExceeded
    400 BackupRecord.ServerIDNotFound
    404 BackupArchive.NotFound
"""

import os

import base64
import tempfile
import simplejson as json
from datetime import datetime

import executil
from pycurl_wrapper import Curl
from utils import AttrDict

class Error(Exception):
    pass

class NotSubscribedError(Error):
    DESC = """\
Backups are not yet enabled for your TurnKey Hub account. Log
into the Hub and go to the "Backups" section for instructions."""

    def __init__(self, desc=DESC):
        Error.__init__(self, desc)

class InvalidBackupError(Error):
    pass

class API:
    ALL_OK = 200
    CREATED = 201
    DELETED = 204

    @classmethod
    def request(cls, method, url, attrs={}, headers={}):
        c = Curl(url, headers)
        func = getattr(c, method.lower())
        func(attrs)

        if not c.response_code in (cls.ALL_OK, cls.CREATED, cls.DELETED):
            name, description = c.response_data.split(":", 1)

            if name == "BackupRecord.NotFound":
                raise InvalidBackupError(description)

            if name in ("BackupAccount.NotSubscribed",
                        "BackupAccount.NotFound"): 
                raise NotSubscribedError()

            raise Error(c.response_code, name, description)

        return json.loads(c.response_data)

class BackupRecord(AttrDict):
    @staticmethod
    def _parse_datetime(s):
        # return datetime("Y-M-D h:m:s")
        if not s:
            return None

        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")

    def __init__(self, response):
        self.key = response['key']
        self.address = response['address']
        self.backup_id = response['backup_id']
        self.server_id = response['server_id']
        self.turnkey_version = response['turnkey_version']

        self.created = self._parse_datetime(response['date_created'])
        self.updated = self._parse_datetime(response['date_updated'])

        self.size = int(response['size']) # in MBs
        self.label = response['description']

        # no interface for this in tklbam, so not returned from hub
        self.sessions = []

class Credentials(AttrDict):
    def __init__(self, response):
        self.accesskey = response['accesskey']
        self.secretkey = response['secretkey']
        self.usertoken = response['usertoken']
        self.producttoken = response['producttoken']

class Backups:
    API_URL = os.getenv('TKLBAM_APIURL', 'https://hub.turnkeylinux.org/api/backup/')
    API_HEADERS = {'Accept': 'application/json'}

    Error = Error

    def __init__(self, subkey=None):
        if subkey is None:
            raise Error("no APIKEY - tklbam not initialized")

        self.subkey = subkey

    def _api(self, method, uri, attrs={}):
        headers = self.API_HEADERS.copy()
        headers['subkey'] = str(self.subkey)

        # workaround: http://redmine.lighttpd.net/issues/1017
        if method == "PUT":
            headers['Expect'] = ''

        return API.request(method, self.API_URL + uri, attrs, headers)

    @classmethod
    def get_sub_apikey(cls, apikey):
        response = API.request('GET', cls.API_URL + 'subkey/', {'apikey': apikey}, cls.API_HEADERS)
        return response['subkey']

    def get_credentials(self):
        response = self._api('GET', 'credentials/')
        return Credentials(response)

    def get_new_profile(self, profile_id, profile_timestamp):
        """
        Gets a profile for <profile_id> that is newer than <profile_timestamp>.

        If there's a new profile, returns a ProfileArchive instance.
        Otherwise returns None.

        Raises an exception if no profile exists for profile_id.
        """
        #attrs = {'profile_id': profile_id}
        attrs = {'turnkey_version': profile_id} # quick hack until we fix the Hub API

        response = self._api('GET', 'archive/timestamp/', attrs)
        archive_timestamp = float(response['archive_timestamp'])

        if profile_timestamp and profile_timestamp >= archive_timestamp:
            return None

        response = self._api('GET', 'archive/', attrs)
        content = base64.urlsafe_b64decode(str(response['archive_content']))

        fd, archive_path = tempfile.mkstemp(prefix="archive.")
        fh = os.fdopen(fd, "w")
        fh.write(content)
        fh.close()

        return ProfileArchive(profile_id, archive_path, archive_timestamp)

    def new_backup_record(self, key, turnkey_version, server_id=None):
        attrs = {'key': key, 'turnkey_version': turnkey_version}
        if server_id:
            attrs['server_id'] = server_id

        response = self._api('POST', 'record/create/', attrs)
        return BackupRecord(response)

    def get_backup_record(self, backup_id):
        response = self._api('GET', 'record/%s/' % backup_id)
        return BackupRecord(response)

    def set_backup_inprogress(self, backup_id, bool):
        response = self._api('PUT', 'record/%s/inprogress/' % backup_id,
                             {'bool': bool})

        return response

    def update_key(self, backup_id, key):
        response = self._api('PUT', 'record/%s/' % backup_id, {'key': key})
        return BackupRecord(response)

    def updated_backup(self, address):
        response = self._api('PUT', 'record/update/', {'address': address})
        return response

    def list_backups(self):
        response = self._api('GET', 'records/')
        return map(lambda r: BackupRecord(r), response)

class ProfileArchive:
    def __init__(self, profile_id, archive, timestamp):
        self.path_archive = archive
        self.timestamp = timestamp
        self.profile_id = profile_id

    def extract(self, path):
        executil.system("tar -zxf %s -C %s" % (self.path_archive, path))

    def __del__(self):
        if os.path.exists(self.path_archive):
            os.remove(self.path_archive)

from conf import Conf
if os.environ.get("TKLBAM_DUMMYHUB") or os.path.exists(os.path.join(Conf.DEFAULT_PATH, "dummyhub")):
    from dummyhub import Backups

