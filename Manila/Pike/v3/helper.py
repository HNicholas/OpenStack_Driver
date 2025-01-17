# Copyright (c) 2014 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import base64
import copy
import requests
import time
from xml.etree import ElementTree as ET

from oslo_log import log
from oslo_serialization import jsonutils
import six

from manila import exception
from manila.i18n import _
from manila.share.drivers.huawei import constants
from manila.share.drivers.huawei import huawei_utils
from manila import utils

LOG = log.getLogger(__name__)


class RestHelper(object):
    """Helper class for Huawei OceanStor V3 storage system."""

    def __init__(self, configuration):
        self.configuration = configuration
        self.session = None

        LOG.warning("Suppressing requests library SSL Warnings")
        requests.packages.urllib3.disable_warnings(
            requests.packages.urllib3.exceptions.InsecureRequestWarning)
        requests.packages.urllib3.disable_warnings(
            requests.packages.urllib3.exceptions.InsecurePlatformWarning)

    def init_http_head(self):
        self.url = None
        self.session = requests.Session()
        self.session.headers.update({
            "Connection": "keep-alive",
            "Content-Type": "application/json"})
        self.session.verify = False

    def do_call(self, url, data=None, method=None,
                calltimeout=constants.SOCKET_TIMEOUT):
        """Send requests to server.

        Send HTTPS call, get response in JSON.
        Convert response into Python Object and return it.
        """
        if self.url:
            url = self.url + url
        if "xx/sessions" not in url:
            LOG.debug('Request URL: %(url)s\n'
                      'Call Method: %(method)s\n'
                      'Request Data: %(data)s\n',
                      {'url': url,
                       'method': method,
                       'data': data})

        kwargs = {'timeout': calltimeout}
        if data:
            kwargs['data'] = data

        method = method or 'POST'
        if method in ('POST', 'PUT', 'GET', 'DELETE'):
            func = getattr(self.session, method.lower())
        else:
            msg = _("Request method %s is invalid.") % method
            LOG.error(msg)
            raise exception.ShareBackendException(msg=msg)

        try:
            res = func(url, **kwargs)
        except Exception as err:
            LOG.error('\nBad response from server: %(url)s.'
                      ' Error: %(err)s', {'url': url, 'err': err})
            return {"error": {"code": constants.ERROR_CONNECT_TO_SERVER,
                              "description": "Connect server error"}}

        try:
            res.raise_for_status()
        except requests.HTTPError as exc:
            return {"error": {"code": exc.response.status_code,
                              "description": six.text_type(exc)}}

        result = res.json()
        LOG.debug('Response Data: %s', result)
        return result

    def login(self):
        """Login huawei array."""
        login_info = self._get_login_info()
        urlstr = login_info['RestURL']
        url_list = urlstr.split(";")
        deviceid = None
        for item_url in url_list:
            url = item_url.strip('').strip('\n') + "xx/sessions"
            data = jsonutils.dumps({"username": login_info['UserName'],
                                    "password": login_info['UserPassword'],
                                    "scope": "0"})
            self.init_http_head()
            result = self.do_call(url, data,
                                  calltimeout=constants.LOGIN_SOCKET_TIMEOUT)

            if((result['error']['code'] != 0)
               or ("data" not in result)
               or (result['data']['deviceid'] is None)):
                LOG.error("Login to %s failed, try another.", item_url)
                continue

            LOG.debug('Login success: %(url)s\n',
                      {'url': item_url})
            deviceid = result['data']['deviceid']
            self.url = item_url + deviceid
            self.session.headers['iBaseToken'] = result['data']['iBaseToken']
            if (result['data']['accountstate']
                    in constants.PWD_EXPIRED_OR_INITIAL):
                self.logout()
                msg = _("Password has expired or initial, "
                        "please change the password.")
                LOG.error(msg)
                raise exception.ShareBackendException(msg=msg)
            break

        if deviceid is None:
            err_msg = _("All url login fail.")
            LOG.error(err_msg)
            raise exception.InvalidShare(reason=err_msg)

        return deviceid

    def logout(self):
        """Logout the session."""
        url = "/sessions"
        if self.url:
            result = self.do_call(url, None, "DELETE")
            self._assert_rest_result(result, _('Logout session error.'))

    @utils.synchronized('huawei_manila')
    def call(self, url, data=None, method=None):
        """Send requests to server.

        If fail, try another RestURL.
        """
        deviceid = None
        old_url = self.url
        result = self.do_call(url, data, method)
        error_code = result['error']['code']
        if(error_code == constants.ERROR_CONNECT_TO_SERVER
           or error_code == constants.ERROR_UNAUTHORIZED_TO_SERVER):
            LOG.error("Can't open the recent url, re-login.")
            deviceid = self.login()

        if deviceid is not None:
            LOG.debug('Replace URL: \n'
                      'Old URL: %(old_url)s\n'
                      'New URL: %(new_url)s\n',
                      {'old_url': old_url,
                       'new_url': self.url})
            result = self.do_call(url, data, method)
        return result

    def _create_filesystem(self, fs_param):
        """Create file system."""
        url = "/filesystem"
        data = jsonutils.dumps(fs_param)
        result = self.call(url, data)

        msg = 'Create filesystem error.'
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

        return result['data']['ID']

    def _assert_rest_result(self, result, err_str):
        if result['error']['code'] != 0:
            err_msg = (_('%(err)s\nresult: %(res)s.') % {'err': err_str,
                                                         'res': result})
            LOG.error(err_msg)
            raise exception.InvalidShare(reason=err_msg)

    def _assert_data_in_result(self, result, msg):
        if "data" not in result:
            err_msg = (_('%s "data" was not in result.') % msg)
            LOG.error(err_msg)
            raise exception.InvalidShare(reason=err_msg)

    def _get_login_info(self):
        """Get login IP, username and password from config file."""
        logininfo = {}
        filename = self.configuration.manila_huawei_conf_file
        tree = ET.parse(filename)
        root = tree.getroot()
        RestURL = root.findtext('Storage/RestURL')
        logininfo['RestURL'] = RestURL.strip()

        # Prefix !$$$ means encoded already.
        prefix_name = '!$$$'
        need_encode = False
        for key in ['UserName', 'UserPassword']:
            node = root.find('Storage/%s' % key)
            if node.text.find(prefix_name) > -1:
                logininfo[key] = base64.b64decode(six.b(node.text[4:]))
            else:
                logininfo[key] = node.text
                node.text = prefix_name + six.text_type(
                    base64.b64encode(six.b(node.text)))
                need_encode = True
        if need_encode:
            self._change_file_mode(filename)
            try:
                tree.write(filename, 'UTF-8')
            except Exception as err:
                err_msg = (_('File write error %s.') % err)
                LOG.error(err_msg)
                raise exception.InvalidShare(reason=err_msg)

        return logininfo

    def _change_file_mode(self, filepath):
        try:
            utils.execute('chmod', '666', filepath, run_as_root=True)

        except Exception as err:
            LOG.error('Bad response from change file: %s.' % err)
            raise

    def create_share(self, share_name, fs_id, share_proto, vstore_id=None):
        """Create a share."""
        share_url_type = self._get_share_url_type(share_proto)
        share_path = self._get_share_path(share_name)

        filepath = {}
        if share_proto == 'NFS':
            filepath = {
                "DESCRIPTION": "",
                "FSID": fs_id,
                "SHAREPATH": share_path,
            }
        elif share_proto == 'CIFS':
            filepath = {
                "SHAREPATH": share_path,
                "DESCRIPTION": "",
                "ABEENABLE": "false",
                "ENABLENOTIFY": "true",
                "ENABLEOPLOCK": "true",
                "NAME": share_name.replace("-", "_"),
                "FSID": fs_id,
                "TENANCYID": "0",
            }
        else:
            raise exception.InvalidShare(
                reason=(_('Invalid NAS protocol supplied: %s.')
                        % share_proto))
        if vstore_id:
            filepath["vstoreId"] = vstore_id

        url = "/" + share_url_type
        data = jsonutils.dumps(filepath)

        result = self.call(url, data, "POST")

        msg = 'Create share error.'
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

        return result['data']['ID']

    def _delete_share_by_id(self, share_id, share_url_type, vstore_id=None):
        """Delete share by share id."""
        url = "/" + share_url_type + "/" + share_id
        data = jsonutils.dumps({'vstoreId': vstore_id}) if vstore_id else None
        result = self.call(url, data, "DELETE")
        self._assert_rest_result(result, 'Delete share error.')

    def _delete_fs(self, params):
        """Delete file system."""
        # Get available file system
        url = "/filesystem"
        data = jsonutils.dumps(params)
        result = self.call(url, data, "DELETE")
        self._assert_rest_result(result, 'Delete file system error.')

    def _find_pool_info(self, pool_name, result):
        if pool_name is None:
            return

        poolinfo = {}
        pool_name = pool_name.strip()
        for item in result.get('data', []):
            if pool_name == item['NAME'] and '2' == item['USAGETYPE']:
                poolinfo['name'] = pool_name
                poolinfo['ID'] = item['ID']
                poolinfo['CAPACITY'] = item['USERFREECAPACITY']
                poolinfo['TOTALCAPACITY'] = item['USERTOTALCAPACITY']
                poolinfo['CONSUMEDCAPACITY'] = item['USERCONSUMEDCAPACITY']
                poolinfo['TIER0CAPACITY'] = item['TIER0CAPACITY']
                if 'TIER1CAPACITY' in item:
                    poolinfo['TIER1CAPACITY'] = item['TIER1CAPACITY']
                if 'TIER2CAPACITY' in item:
                    poolinfo['TIER2CAPACITY'] = item['TIER2CAPACITY']
                break

        return poolinfo

    def _find_all_pool_info(self):
        url = "/storagepool"
        result = self.call(url, None, "GET")

        msg = "Query resource pool error."
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

        return result

    def _read_xml(self):
        """Open xml file and parse the content."""
        filename = self.configuration.manila_huawei_conf_file
        try:
            tree = ET.parse(filename)
            root = tree.getroot()
        except Exception as err:
            message = (_('Read Huawei config file(%(filename)s)'
                         ' for Manila error: %(err)s')
                       % {'filename': filename,
                          'err': err})
            LOG.error(message)
            raise exception.InvalidInput(reason=message)
        return root

    def get_product(self):
        root = self._read_xml()
        text = root.findtext('Storage/Product')
        if not text:
            msg = _("NAS product is not configured.")
            LOG.error(msg)
            raise exception.InvalidInput(reason=msg)

        product = text.strip()
        if product not in constants.VALID_PRODUCT:
            msg = (_("Invalid NAS product '%(text)s', NAS product must be in "
                     "%(valid)s.")
                   % {'text': product, 'valid': constants.VALID_PRODUCT})
            LOG.error(msg)
            raise exception.InvalidInput(reason=msg)
        setattr(self.configuration, 'nas_product', product)

    def _remove_access_from_share(self, access_id, share_proto,
                                  vstore_id=None):
        access_type = self._get_share_client_type(share_proto)
        url = "/" + access_type + "/" + access_id
        params = {'vstoreId': vstore_id} if vstore_id else None
        if params:
            params = jsonutils.dumps(params)
        result = self.call(url, params, "DELETE")
        self._assert_rest_result(result, 'delete access from share error!')

    def _get_access_count(self, share_id, share_client_type, vstore_id=None):
        url_subfix = ("/" + share_client_type + "/count?"
                      + "filter=PARENTID::" + share_id)
        url = url_subfix
        params = {'vstoreId': vstore_id} if vstore_id else None
        if params:
            params = jsonutils.dumps(params)
        result = self.call(url, params, "GET")

        msg = "Get access count by share error!"
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

        return int(result['data']['COUNT'])

    def _get_all_access_from_share(self, share_id, share_proto,
                                   vstore_id=None):
        """Return a list of all the access IDs of the share"""
        share_client_type = self._get_share_client_type(share_proto)
        count = self._get_access_count(share_id, share_client_type)

        access_ids = []
        range_begin = 0
        while count > 0:
            access_range = self._get_access_from_share_range(share_id,
                                                             range_begin,
                                                             share_client_type,
                                                             vstore_id)
            for item in access_range:
                access_ids.append(item['ID'])
            range_begin += 100
            count -= 100

        return access_ids

    def _get_access_from_share(self, share_id, access_to, share_proto,
                               vstore_id=None):
        """Segments to find access for a period of 100."""
        share_client_type = self._get_share_client_type(share_proto)
        count = self._get_access_count(share_id, share_client_type, vstore_id)

        access_id = None
        range_begin = 0
        while count > 0:
            if access_id:
                break
            access_range = self._get_access_from_share_range(share_id,
                                                             range_begin,
                                                             share_client_type,
                                                             vstore_id)
            for item in access_range:
                if item['NAME'] in (access_to, '@' + access_to):
                    access_id = item['ID']

            range_begin += 100
            count -= 100

        return access_id

    def _get_access_from_share_range(self, share_id,
                                     range_begin,
                                     share_client_type,
                                     vstore_id=None):
        range_end = range_begin + 100
        url = ("/" + share_client_type + "?filter=PARENTID::"
               + share_id + "&range=[" + six.text_type(range_begin)
               + "-" + six.text_type(range_end) + "]")
        params = {'vstoreId': vstore_id} if vstore_id else None
        if params:
            params = jsonutils.dumps(params)
        result = self.call(url, params, "GET")
        self._assert_rest_result(result, 'Get access id by share error!')
        return result.get('data', [])

    def _get_level_by_access_id(self, access_id, share_proto, vstore_id=None):
        share_client_type = self._get_share_client_type(share_proto)
        url = "/" + share_client_type + "/" + access_id
        params = {'vstoreId': vstore_id} if vstore_id else None
        if params:
            params = jsonutils.dumps(params)
        result = self.call(url, params, "GET")
        self._assert_rest_result(result, 'Get access information error!')
        access_info = result.get('data', [])
        access_level = access_info.get('ACCESSVAL')
        if not access_level:
            access_level = access_info.get('PERMISSION')
        return access_level

    def _change_access_rest(self, access_id,
                            share_proto, access_level, vstore_id=None):
        """Change access level of the share."""
        if share_proto == 'NFS':
            self._change_nfs_access_rest(access_id, access_level, vstore_id)
        elif share_proto == 'CIFS':
            self._change_cifs_access_rest(access_id, access_level, vstore_id)
        else:
            raise exception.InvalidInput(
                reason=(_('Invalid NAS protocol supplied: %s.')
                        % share_proto))

    def _change_nfs_access_rest(self, access_id, access_level, vstore_id=None):
        url = "/NFS_SHARE_AUTH_CLIENT/" + access_id
        access = {
            "ACCESSVAL": access_level,
            "SYNC": "0",
            "ALLSQUASH": "1",
            "ROOTSQUASH": "0",
        }
        if vstore_id:
            access.update({"vstoreId": vstore_id})
        data = jsonutils.dumps(access)
        result = self.call(url, data, "PUT")

        msg = 'Change access error.'
        self._assert_rest_result(result, msg)

    def _change_cifs_access_rest(self, access_id, access_level,
                                 vstore_id=None):
        url = "/CIFS_SHARE_AUTH_CLIENT/" + access_id
        access = {
            "PERMISSION": access_level,
        }
        if vstore_id:
            access.update({"vstoreId": vstore_id})
        data = jsonutils.dumps(access)
        result = self.call(url, data, "PUT")

        msg = 'Change access error.'
        self._assert_rest_result(result, msg)

    def _allow_access_rest(self, share_id, access_to, share_proto,
                           access_level, share_type_id, vstore_id=None):
        """Allow access to the share."""
        if share_proto == 'NFS':
            self._allow_nfs_access_rest(share_id, access_to, access_level,
                                        share_type_id, vstore_id)
        elif share_proto == 'CIFS':
            self._allow_cifs_access_rest(share_id, access_to,
                                         access_level, vstore_id)
        else:
            raise exception.InvalidInput(
                reason=(_('Invalid NAS protocol supplied: %s.')
                        % share_proto))

    def _allow_nfs_access_rest(self, share_id, access_to, access_level,
                               share_type_id=None, vstore_id=None):
        url = "/NFS_SHARE_AUTH_CLIENT"
        access = {
            "TYPE": "16409",
            "NAME": access_to,
            "PARENTID": share_id,
            "ACCESSVAL": access_level,
            "SYNC": "0",
            "ALLSQUASH": "1",
            "ROOTSQUASH": "0",
        }

        if share_type_id:
            specs = huawei_utils.get_share_extra_specs_params(share_type_id)
            if specs:
                if specs.get('sync'):
                    access['SYNC'] = specs['sync']
                if specs.get('allsquash'):
                    access['ALLSQUASH'] = specs['allsquash']
                if specs.get('rootsquash'):
                    access['ROOTSQUASH'] = specs['rootsquash']
                if specs.get('secure'):
                    access['SECURE'] = specs['secure']

        if vstore_id:
            access.update({"vstoreId": vstore_id})
        data = jsonutils.dumps(access)
        result = self.call(url, data, "POST")

        msg = 'Allow access error.'
        self._assert_rest_result(result, msg)

    def _allow_cifs_access_rest(self, share_id, access_to, access_level,
                                vstore=None):
        url = "/CIFS_SHARE_AUTH_CLIENT"
        domain_type = {
            'local': '2',
            'ad': '0'
        }
        error_msg = 'Allow access error.'
        access_info = ('Access info (access_to: %(access_to)s, '
                       'access_level: %(access_level)s, share_id: %(id)s)'
                       % {'access_to': access_to,
                          'access_level': access_level,
                          'id': share_id})

        def send_rest(access_to, domain_type, vstore_id=None):
            access = {
                "NAME": access_to,
                "PARENTID": share_id,
                "PERMISSION": access_level,
                "DOMAINTYPE": domain_type,
            }
            if vstore_id:
                access.update({"vstoreId": vstore_id})
            data = jsonutils.dumps(access)
            result = self.call(url, data, "POST")
            error_code = result['error']['code']
            if error_code == 0:
                return True
            elif error_code != constants.ERROR_USER_OR_GROUP_NOT_EXIST:
                self._assert_rest_result(result, error_msg)
            return False

        if '\\' not in access_to:
            # First, try to add user access.
            LOG.debug('Try to add user access. %s.', access_info)
            if send_rest(access_to, domain_type['local'], vstore):
                return
            # Second, if add user access failed,
            # try to add group access.
            LOG.debug('Failed with add user access, '
                      'try to add group access. %s.', access_info)
            # Group name starts with @.
            if send_rest('@' + access_to, domain_type['local'], vstore):
                return
        else:
            LOG.debug('Try to add domain user access. %s.', access_info)
            if send_rest(access_to, domain_type['ad'], vstore):
                return
            # If add domain user access failed,
            # try to add domain group access.
            LOG.debug('Failed with add domain user access, '
                      'try to add domain group access. %s.', access_info)
            # Group name starts with @.
            if send_rest('@' + access_to, domain_type['ad'], vstore):
                return

        raise exception.InvalidShare(reason=error_msg)

    def _get_share_client_type(self, share_proto):
        share_client_type = None
        if share_proto == 'NFS':
            share_client_type = "NFS_SHARE_AUTH_CLIENT"
        elif share_proto == 'CIFS':
            share_client_type = "CIFS_SHARE_AUTH_CLIENT"
        else:
            raise exception.InvalidInput(
                reason=(_('Invalid NAS protocol supplied: %s.')
                        % share_proto))

        return share_client_type

    def _check_snapshot_id_exist(self, snapshot_info):
        """Check the snapshot id exists."""

        if snapshot_info['error']['code'] == constants.MSG_SNAPSHOT_NOT_FOUND:
            return False
        elif snapshot_info['error']['code'] == 0:
            return True
        else:
            err_str = "Check the snapshot id exists error!"
            err_msg = (_('%(err)s\nresult: %(res)s.') % {'err': err_str,
                                                         'res': snapshot_info})
            raise exception.InvalidShareSnapshot(reason=err_msg)

    def _get_snapshot_by_id(self, snap_id):
        """Get snapshot by id"""
        url = "/FSSNAPSHOT/" + snap_id

        result = self.call(url, None, "GET")
        return result

    def _delete_snapshot(self, snap_id):
        """Deletes snapshot."""
        url = "/FSSNAPSHOT/%s" % snap_id
        data = jsonutils.dumps({"TYPE": "48", "ID": snap_id})
        result = self.call(url, data, "DELETE")
        self._assert_rest_result(result, 'Delete snapshot error.')

    def _create_snapshot(self, sharefsid, snapshot_name):
        """Create a snapshot."""
        filepath = {
            "PARENTTYPE": "40",
            "TYPE": "48",
            "PARENTID": sharefsid,
            "NAME": snapshot_name.replace("-", "_"),
            "DESCRIPTION": "",
        }

        url = "/FSSNAPSHOT"
        data = jsonutils.dumps(filepath)

        result = self.call(url, data, "POST")

        msg = 'Create a snapshot error.'
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

        return result['data']['ID']

    def _get_share_by_name(self, share_name, share_url_type, vstore_id=None):
        """Segments to find share for a period of 100."""
        count = self._get_share_count(share_url_type)

        share = {}
        range_begin = 0
        while True:
            if count < 0 or share:
                break
            share = self._get_share_by_name_range(share_name,
                                                  range_begin,
                                                  share_url_type,
                                                  vstore_id)
            range_begin += 100
            count -= 100

        return share

    def _get_share_count(self, share_url_type):
        """Get share count."""
        url = "/" + share_url_type + "/count"
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Get share count error!')

        return int(result['data']['COUNT'])

    def _get_share_by_name_range(self, share_name,
                                 range_begin, share_url_type, vstore_id=None):
        """Get share by share name."""
        range_end = range_begin + 100
        url = ("/" + share_url_type + "?range=["
               + six.text_type(range_begin) + "-"
               + six.text_type(range_end) + "]")
        params = {'vstoreId': vstore_id} if vstore_id else None
        if params:
            params = jsonutils.dumps(params)
        result = self.call(url, params, "GET")
        self._assert_rest_result(result, 'Get share by name error!')
        share_path = self._get_share_path(share_name)
        share = {}
        for item in result.get('data', []):
            if share_path == item['SHAREPATH']:
                share['ID'] = item['ID']
                share['FSID'] = item['FSID']
                break

        return share

    def _get_share_url_type(self, share_proto):
        share_url_type = None
        if share_proto == 'NFS':
            share_url_type = "NFSHARE"
        elif share_proto == 'CIFS':
            share_url_type = "CIFSHARE"
        else:
            raise exception.InvalidInput(
                reason=(_('Invalid NAS protocol supplied: %s.')
                        % share_proto))

        return share_url_type

    def get_fsid_by_name(self, share_name):
        url = "/FILESYSTEM?range=[0-8191]"
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Get filesystem by name error!')
        share_name = share_name.replace("-", "_")

        for item in result.get('data', []):
            if share_name == item['NAME']:
                return item['ID']

    def _get_fs_info_by_name(self, share_name):
        name = share_name.replace('-', '_')
        url = "/filesystem?filter=NAME::%s" % name
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Get filesystem by name %s error.'
                                 % name)
        if 'data' in result and result['data']:
            return result['data'][0]

    def _get_fs_info_by_id(self, fsid):
        url = "/filesystem/%s" % fsid
        result = self.call(url, None, "GET")

        msg = "Get filesystem info by id error!"
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

        fs_result = result['data']
        fs_result.update(
            {"POOLNAME": fs_result.pop("PARENTNAME"),
             "COMPRESSION": fs_result.pop("ENABLECOMPRESSION"),
             "DEDUP": fs_result.pop("ENABLEDEDUP"),
             "SMARTPARTITIONID": fs_result.pop("CACHEPARTITIONID"),
             "SMARTCACHEID": fs_result.pop("SMARTCACHEPARTITIONID")})
        return fs_result

    def _get_share_path(self, share_name):
        share_path = "/" + share_name.replace("-", "_") + "/"
        return share_path

    def get_share_name_by_id(self, share_id):
        share_name = "share_" + share_id
        return share_name

    def _get_share_name_by_export_location(self, export_location, share_proto):
        share_name = None
        share_ip = None
        if export_location:
            if share_proto == 'NFS':
                export_location_split = export_location.split(':/')
                if len(export_location_split) == 2:
                    share_name = export_location_split[1]
                    share_ip = export_location_split[0]
            elif share_proto == 'CIFS':
                export_location_split = export_location.split('\\')
                if (len(export_location_split) == 4 and
                        export_location_split[0] == "" and
                        export_location_split[1] == ""):
                    share_ip = export_location_split[2]
                    share_name = export_location_split[3]

        if share_name is None:
            raise exception.InvalidInput(
                reason=(_('No share with export location %s could be found.')
                        % export_location))

        target_ips = huawei_utils.get_logical_ips(self)
        if share_ip not in target_ips:
            raise exception.InvalidInput(
                reason=_('The share IP %s is not configured.') % share_ip)

        return share_name

    def _get_snapshot_id(self, fs_id, snap_name):
        snapshot_id = (fs_id + "@" + "share_snapshot_"
                       + snap_name.replace("-", "_"))
        return snapshot_id

    def _change_share_size(self, fsid, new_size):
        url = "/filesystem/%s" % fsid

        capacityinfo = {
            "CAPACITY": new_size,
        }

        data = jsonutils.dumps(capacityinfo)
        result = self.call(url, data, "PUT")

        msg = "Change a share size error!"
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

    def _change_fs_name(self, fsid, name):
        url = "/filesystem/%s" % fsid
        fs_param = {
            "NAME": name.replace("-", "_"),
        }
        data = jsonutils.dumps(fs_param)
        result = self.call(url, data, "PUT")

        msg = _("Change filesystem name error.")
        self._assert_rest_result(result, msg)

    def _change_extra_specs(self, fsid, extra_specs):
        url = "/filesystem/%s" % fsid
        fs_param = {
            "ENABLEDEDUP": extra_specs['dedupe'],
            "ENABLECOMPRESSION": extra_specs['compression']
        }
        data = jsonutils.dumps(fs_param)
        result = self.call(url, data, "PUT")

        msg = _("Change extra_specs error.")
        self._assert_rest_result(result, msg)

    def _get_partition_id_by_name(self, name):
        url = "/cachepartition"
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, _('Get partition by name error.'))

        if "data" in result:
            for item in result['data']:
                if name == item['NAME']:
                    return item['ID']
        return None

    def get_partition_info_by_id(self, partitionid):
        url = '/cachepartition/' + partitionid
        result = self.call(url, None, "GET")
        self._assert_rest_result(result,
                                 _('Get partition by partition id error.'))

        return result['data']

    def _add_fs_to_partition(self, fs_id, partition_id):
        url = "/filesystem/associate/cachepartition"
        data = jsonutils.dumps({"ID": partition_id,
                                "ASSOCIATEOBJTYPE": 40,
                                "ASSOCIATEOBJID": fs_id,
                                "TYPE": 268})
        result = self.call(url, data, "POST")

        self._assert_rest_result(result,
                                 _('Add filesystem to partition error.'))

    def _remove_fs_from_partition(self, fs_id, partition_id):
        url = "/smartPartition/removeFs"
        data = jsonutils.dumps({"ID": partition_id,
                                "ASSOCIATEOBJTYPE": 40,
                                "ASSOCIATEOBJID": fs_id,
                                "TYPE": 268})
        result = self.call(url, data, "PUT")

        self._assert_rest_result(result,
                                 _('Remove filesystem from partition error.'))

    def _rename_share_snapshot(self, snapshot_id, new_name):
        url = "/FSSNAPSHOT/" + snapshot_id
        data = jsonutils.dumps({"NAME": new_name})
        result = self.call(url, data, "PUT")
        msg = _('Rename share snapshot on array error.')
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

    def _get_cache_id_by_name(self, name):
        url = "/SMARTCACHEPARTITION"
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, _('Get cache by name error.'))

        if "data" in result:
            for item in result['data']:
                if name == item['NAME']:
                    return item['ID']
        return None

    def get_cache_info_by_id(self, cacheid):
        url = "/SMARTCACHEPARTITION/" + cacheid
        data = jsonutils.dumps({"TYPE": "273",
                                "ID": cacheid})

        result = self.call(url, data, "GET")
        self._assert_rest_result(
            result, _('Get smartcache by cache id error.'))

        return result['data']

    def _add_fs_to_cache(self, fs_id, cache_id):
        url = "/SMARTCACHEPARTITION/CREATE_ASSOCIATE"
        data = jsonutils.dumps({"ID": cache_id,
                                "ASSOCIATEOBJTYPE": 40,
                                "ASSOCIATEOBJID": fs_id,
                                "TYPE": 273})
        result = self.call(url, data, "PUT")

        self._assert_rest_result(result, _('Add filesystem to cache error.'))

    def get_qos(self):
        url = "/ioclass"
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, _('Get QoS information error.'))
        return result

    def find_available_qos(self, qos):
        """"Find available QoS on the array."""
        qos_id = None
        fs_list = []
        temp_qos = copy.deepcopy(qos)
        result = self.get_qos()

        if 'data' in result:
            if 'LATENCY' not in temp_qos:
                temp_qos['LATENCY'] = '0'
            for item in result['data']:
                for key in constants.OPTS_QOS_VALUE:
                    if temp_qos.get(key.upper()) != item.get(key.upper()):
                        break
                else:
                    fs_num = len(item['FSLIST'].split(","))
                    # We use this QoS only if the filesystems in it is less
                    # than 64, else we cannot add filesystem to this QoS
                    # any more.
                    if (item['RUNNINGSTATUS'] == constants.STATUS_QOS_ACTIVE
                            and fs_num < constants.MAX_FS_NUM_IN_QOS
                            and item['NAME'].startswith(
                                constants.QOS_NAME_PREFIX)
                            and item['LUNLIST'] == '[""]'):
                        qos_id = item['ID']
                        fs_list = item['FSLIST']
                        break

        return (qos_id, fs_list)

    def add_share_to_qos(self, qos_id, fs_id, fs_list):
        """Add filesystem to QoS."""
        url = "/ioclass/" + qos_id
        new_fs_list = []
        fs_list_string = fs_list[1:-1]
        for fs_string in fs_list_string.split(","):
            tmp_fs_id = fs_string[1:-1]
            if '' != tmp_fs_id and tmp_fs_id != fs_id:
                new_fs_list.append(tmp_fs_id)

        new_fs_list.append(fs_id)

        data = jsonutils.dumps({"FSLIST": new_fs_list,
                                "TYPE": 230,
                                "ID": qos_id})
        result = self.call(url, data, "PUT")
        msg = _('Associate filesystem to Qos error.')
        self._assert_rest_result(result, msg)

    def create_qos_policy(self, qos, fs_id):
        # Get local time.
        localtime = time.strftime('%Y%m%d%H%M%S', time.localtime(time.time()))
        # Package QoS name.
        qos_name = constants.QOS_NAME_PREFIX + fs_id + '_' + localtime

        mergedata = {
            "TYPE": "230",
            "NAME": qos_name,
            "FSLIST": ["%s" % fs_id],
            "CLASSTYPE": "1",
            "SCHEDULEPOLICY": "1",
            "SCHEDULESTARTTIME": "1410969600",
            "STARTTIME": "00:00",
            "DURATION": "86400",
        }
        mergedata.update(qos)
        data = jsonutils.dumps(mergedata)
        url = "/ioclass"

        result = self.call(url, data)
        self._assert_rest_result(result, _('Create QoS policy error.'))

        return result['data']['ID']

    def activate_deactivate_qos(self, qos_id, enablestatus):
        """Activate or deactivate QoS.

        enablestatus: true (activate)
        enablestatus: false (deactivate)
        """
        url = "/ioclass/active/" + qos_id
        data = jsonutils.dumps({
            "TYPE": 230,
            "ID": qos_id,
            "ENABLESTATUS": enablestatus})
        result = self.call(url, data, "PUT")
        self._assert_rest_result(
            result, _('Activate or deactivate QoS error.'))

    def change_fs_priority_high(self, fs_id):
        """Change fs priority to high."""
        url = "/filesystem/" + fs_id
        data = jsonutils.dumps({"IOPRIORITY": "3"})

        result = self.call(url, data, "PUT")
        self._assert_rest_result(
            result, _('Change filesystem priority error.'))

    def delete_qos_policy(self, qos_id):
        """Delete a QoS policy."""
        url = "/ioclass/" + qos_id
        data = jsonutils.dumps({"TYPE": "230",
                                "ID": qos_id})

        result = self.call(url, data, 'DELETE')
        self._assert_rest_result(result, _('Delete QoS policy error.'))

    def get_qosid_by_fsid(self, fs_id):
        """Get QoS id by fs id."""
        url = "/filesystem/" + fs_id
        result = self.call(url, None, "GET")
        self._assert_rest_result(
            result, _('Get QoS id by filesystem id error.'))

        return result['data'].get('IOCLASSID')

    def get_fs_list_in_qos(self, qos_id):
        """Get the filesystem list in QoS."""
        qos_info = self.get_qos_info(qos_id)

        fs_list = []
        fs_string = qos_info['FSLIST'][1:-1]

        for fs in fs_string.split(","):
            fs_id = fs[1:-1]
            fs_list.append(fs_id)

        return fs_list

    def get_qos_info(self, qos_id):
        """Get QoS information."""
        url = "/ioclass/" + qos_id
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, _('Get QoS information error.'))

        return result['data']

    def remove_fs_from_qos(self, fs_id, fs_list, qos_id):
        """Remove filesystem from QoS."""
        fs_list = [i for i in fs_list if i != fs_id]
        url = "/ioclass/" + qos_id
        data = jsonutils.dumps({"FSLIST": fs_list,
                                "TYPE": 230,
                                "ID": qos_id})
        result = self.call(url, data, "PUT")

        msg = _('Remove filesystem from QoS error.')
        self._assert_rest_result(result, msg)

    def _remove_fs_from_cache(self, fs_id, cache_id):
        url = "/SMARTCACHEPARTITION/REMOVE_ASSOCIATE"
        data = jsonutils.dumps({"ID": cache_id,
                                "ASSOCIATEOBJTYPE": 40,
                                "ASSOCIATEOBJID": fs_id,
                                "TYPE": 273})
        result = self.call(url, data, "PUT")

        self._assert_rest_result(result,
                                 _('Remove filesystem from cache error.'))

    def get_all_eth_port(self):
        url = "/ETH_PORT"
        result = self.call(url, None, 'GET')
        self._assert_rest_result(result, _('Get all eth port error.'))

        all_eth = {}
        if "data" in result:
            all_eth = result['data']

        return all_eth

    def get_eth_port_by_id(self, port_id):
        url = "/ETH_PORT/" + port_id
        result = self.call(url, None, 'GET')
        self._assert_rest_result(result, _('Get eth port by id error.'))

        if "data" in result:
            return result['data']

        return None

    def get_all_bond_port(self):
        url = "/BOND_PORT"
        result = self.call(url, None, 'GET')
        self._assert_rest_result(result, _('Get all bond port error.'))

        all_bond = {}
        if "data" in result:
            all_bond = result['data']

        return all_bond

    def get_port_id(self, port_name, port_type):
        if port_type == constants.PORT_TYPE_ETH:
            all_eth = self.get_all_eth_port()
            for item in all_eth:
                if port_name == item['LOCATION']:
                    return item['ID']
        elif port_type == constants.PORT_TYPE_BOND:
            all_bond = self.get_all_bond_port()
            for item in all_bond:
                if port_name == item['NAME']:
                    return item['ID']

        return None

    def get_all_vlan(self):
        url = "/vlan"
        result = self.call(url, None, 'GET')
        self._assert_rest_result(result, _('Get all vlan error.'))

        all_vlan = {}
        if "data" in result:
            all_vlan = result['data']

        return all_vlan

    def get_vlan(self, port_id, vlan_tag):
        url = "/vlan"
        result = self.call(url, None, 'GET')
        self._assert_rest_result(result, _('Get vlan error.'))

        vlan_tag = six.text_type(vlan_tag)
        if "data" in result:
            for item in result['data']:
                if port_id == item['PORTID'] and vlan_tag == item['TAG']:
                    return True, item['ID']

        return False, None

    def create_vlan(self, port_id, port_type, vlan_tag):
        url = "/vlan"
        data = jsonutils.dumps({"PORTID": port_id,
                                "PORTTYPE": port_type,
                                "TAG": six.text_type(vlan_tag),
                                "TYPE": "280"})
        result = self.call(url, data, "POST")
        self._assert_rest_result(result, _('Create vlan error.'))

        return result['data']['ID']

    def check_vlan_exists_by_id(self, vlan_id):
        all_vlan = self.get_all_vlan()
        return any(vlan['ID'] == vlan_id for vlan in all_vlan)

    def delete_vlan(self, vlan_id):
        url = "/vlan/" + vlan_id
        result = self.call(url, None, 'DELETE')
        if result['error']['code'] == constants.ERROR_LOGICAL_PORT_EXIST:
            LOG.warning('Cannot delete vlan because there is '
                        'a logical port on vlan.')
            return

        self._assert_rest_result(result, _('Delete vlan error.'))

    def get_logical_port(self, home_port_id, ip, subnet):
        url = "/LIF"
        result = self.call(url, None, 'GET')
        self._assert_rest_result(result, _('Get logical port error.'))

        if "data" not in result:
            return False, None

        for item in result['data']:
            if (home_port_id == item['HOMEPORTID']
                    and ip == item['IPV4ADDR']
                    and subnet == item['IPV4MASK']):
                if item['OPERATIONALSTATUS'] != 'true':
                    self._activate_logical_port(item['ID'])
                return True, item['ID']

        return False, None

    def _activate_logical_port(self, logical_port_id):
        url = "/LIF/" + logical_port_id
        data = jsonutils.dumps({"OPERATIONALSTATUS": "true"})
        result = self.call(url, data, 'PUT')
        self._assert_rest_result(result, _('Activate logical port error.'))

    def create_logical_port(self, home_port_id, home_port_type, ip, subnet):
        url = "/LIF"
        info = {
            "ADDRESSFAMILY": 0,
            "CANFAILOVER": "true",
            "HOMEPORTID": home_port_id,
            "HOMEPORTTYPE": home_port_type,
            "IPV4ADDR": ip,
            "IPV4GATEWAY": "",
            "IPV4MASK": subnet,
            "NAME": ip,
            "OPERATIONALSTATUS": "true",
            "ROLE": 2,
            "SUPPORTPROTOCOL": 3,
            "TYPE": "279",
        }

        data = jsonutils.dumps(info)
        result = self.call(url, data, 'POST')
        self._assert_rest_result(result, _('Create logical port error.'))

        return result['data']['ID']

    def check_logical_port_exists_by_id(self, logical_port_id):
        all_logical_port = self.get_all_logical_port()
        return any(port['ID'] == logical_port_id for port in all_logical_port)

    def get_all_logical_port(self):
        url = "/LIF"
        result = self.call(url, None, 'GET')
        self._assert_rest_result(result, _('Get all logical port error.'))

        all_logical_port = {}
        if "data" in result:
            all_logical_port = result['data']

        return all_logical_port

    def get_logical_port_by_id(self, logical_port_id):
        url = "/LIF/%s" % logical_port_id
        result = self.call(url, None, 'GET')
        self._assert_rest_result(result, _('Get logical port error.'))
        return result.get('data', {})

    def modify_logical_port(self, logical_port_id, vstore_id):
        logical_port_info = self.get_logical_port_by_id(logical_port_id)
        logical_port_info.update({'vstoreId': vstore_id,
                                  'dnsZoneName': ""})
        url = "/LIF/%s" % logical_port_id
        result = self.call(url, jsonutils.dumps(logical_port_info), 'PUT')
        self._assert_rest_result(result, _('Modify logical port error.'))

    def delete_logical_port(self, logical_port_id):
        url = "/LIF/" + logical_port_id
        result = self.call(url, None, 'DELETE')
        self._assert_rest_result(result, _('Delete logical port error.'))

    def set_DNS_ip_address(self, dns_ip_list):
        if len(dns_ip_list) > 3:
            message = _('Most three ips can be set to DNS.')
            LOG.error(message)
            raise exception.InvalidInput(reason=message)

        url = "/DNS_Server"
        dns_info = {
            "ADDRESS": jsonutils.dumps(dns_ip_list),
            "TYPE": "260",
        }
        data = jsonutils.dumps(dns_info)
        result = self.call(url, data, 'PUT')
        self._assert_rest_result(result, _('Set DNS ip address error.'))

        if "data" in result:
            return result['data']

        return None

    def get_DNS_ip_address(self):
        url = "/DNS_Server"
        result = self.call(url, None, 'GET')
        self._assert_rest_result(result, _('Get DNS ip address error.'))

        ip_address = {}
        if "data" in result:
            ip_address = jsonutils.loads(result['data']['ADDRESS'])

        return ip_address

    def add_AD_config(self, user, password, domain, system_name):
        url = "/AD_CONFIG"
        info = {
            "ADMINNAME": user,
            "ADMINPWD": password,
            "DOMAINSTATUS": 1,
            "FULLDOMAINNAME": domain,
            "OU": "",
            "SYSTEMNAME": system_name,
            "TYPE": "16414",
        }
        data = jsonutils.dumps(info)
        result = self.call(url, data, 'PUT')
        self._assert_rest_result(result, _('Add AD config error.'))

    def delete_AD_config(self, user, password):
        url = "/AD_CONFIG"
        info = {
            "ADMINNAME": user,
            "ADMINPWD": password,
            "DOMAINSTATUS": 0,
            "TYPE": "16414",
        }
        data = jsonutils.dumps(info)
        result = self.call(url, data, 'PUT')
        self._assert_rest_result(result, _('Delete AD config error.'))

    def get_AD_config(self):
        url = "/AD_CONFIG"
        result = self.call(url, None, 'GET')
        self._assert_rest_result(result, _('Get AD config error.'))

        if "data" in result:
            return result['data']

        return None

    def get_AD_domain_name(self):
        result = self.get_AD_config()
        if result and result['DOMAINSTATUS'] == '1':
            return True, result['FULLDOMAINNAME']

        return False, None

    def add_LDAP_config(self, server, domain):
        url = "/LDAP_CONFIG"
        info = {
            "BASEDN": domain,
            "LDAPSERVER": server,
            "PORTNUM": 389,
            "TRANSFERTYPE": "1",
            "TYPE": "16413",
            "USERNAME": "",
        }
        data = jsonutils.dumps(info)
        result = self.call(url, data, 'PUT')
        self._assert_rest_result(result, _('Add LDAP config error.'))

    def delete_LDAP_config(self):
        url = "/LDAP_CONFIG"
        result = self.call(url, None, 'DELETE')
        self._assert_rest_result(result, _('Delete LDAP config error.'))

    def get_LDAP_config(self):
        url = "/LDAP_CONFIG"
        result = self.call(url, None, 'GET')
        self._assert_rest_result(result, _('Get LDAP config error.'))

        if "data" in result:
            return result['data']

        return None

    def get_LDAP_domain_server(self):
        result = self.get_LDAP_config()
        if result and result['LDAPSERVER']:
            return True, result['LDAPSERVER']

        return False, None

    def _get_array_info(self):
        url = "/system/"
        result = self.call(url, None, "GET")
        msg = _('Get array info error.')
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)
        return result.get('data')

    def find_array_version(self):
        info = self._get_array_info()
        return info.get('PRODUCTVERSION')

    def get_array_wwn(self):
        info = self._get_array_info()
        return info.get('wwn')

    def _get_all_remote_devices(self):
        url = "/remote_device"
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, _('Get all remote devices error.'))
        return result.get('data', [])

    def get_remote_device_by_wwn(self, wwn):
        devices = self._get_all_remote_devices()
        for device in devices:
            if device.get('WWN') == wwn:
                return device
        return {}

    def create_replication_pair(self, pair_params):
        url = "/REPLICATIONPAIR"
        data = jsonutils.dumps(pair_params)
        result = self.call(url, data, "POST")

        msg = _('Failed to create replication pair of %s.') % pair_params
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)
        return result['data']

    def split_replication_pair(self, pair_id):
        url = '/REPLICATIONPAIR/split'
        data = jsonutils.dumps({"ID": pair_id, "TYPE": "263"})
        result = self.call(url, data, "PUT")

        msg = _('Failed to split replication pair %s.') % pair_id
        self._assert_rest_result(result, msg)

    def switch_replication_pair(self, pair_id):
        url = '/REPLICATIONPAIR/switch'
        data = jsonutils.dumps({"ID": pair_id, "TYPE": "263"})
        result = self.call(url, data, "PUT")

        msg = _('Failed to switch replication pair %s.') % pair_id
        self._assert_rest_result(result, msg)

    def delete_replication_pair(self, pair_id):
        url = "/REPLICATIONPAIR/" + pair_id
        data = None
        result = self.call(url, data, "DELETE")

        if (result['error']['code'] ==
                constants.ERROR_REPLICATION_PAIR_NOT_EXIST):
            LOG.warning('Replication pair %s was not found.',
                        pair_id)
            return

        msg = _('Failed to delete replication pair %s.') % pair_id
        self._assert_rest_result(result, msg)

    def sync_replication_pair(self, pair_id):
        url = "/REPLICATIONPAIR/sync"
        data = jsonutils.dumps({"ID": pair_id, "TYPE": "263"})
        result = self.call(url, data, "PUT")

        msg = _('Failed to sync replication pair %s.') % pair_id
        self._assert_rest_result(result, msg)

    def cancel_pair_secondary_write_lock(self, pair_id):
        url = "/REPLICATIONPAIR/CANCEL_SECODARY_WRITE_LOCK"
        data = jsonutils.dumps({"ID": pair_id, "TYPE": "263"})
        result = self.call(url, data, "PUT")

        msg = _('Failed to cancel replication pair %s '
                'secondary write lock.') % pair_id
        self._assert_rest_result(result, msg)

    def set_pair_secondary_write_lock(self, pair_id):
        url = "/REPLICATIONPAIR/SET_SECODARY_WRITE_LOCK"
        data = jsonutils.dumps({"ID": pair_id, "TYPE": "263"})
        result = self.call(url, data, "PUT")

        msg = _('Failed to set replication pair %s '
                'secondary write lock.') % pair_id
        self._assert_rest_result(result, msg)

    def get_replication_pair_by_id(self, pair_id):
        url = "/REPLICATIONPAIR/" + pair_id
        result = self.call(url, None, "GET")

        msg = _('Failed to get replication pair %s.') % pair_id
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)
        return result.get('data')

    def get_replication_pair_by_localres_name(self, local_res):
        url = "/REPLICATIONPAIR?filter=LOCALRESNAME::" + local_res
        result = self.call(url, None, "GET")

        msg = _('Failed to query replication pair by '
                'local resource name %s.') % local_res
        self._assert_rest_result(result, msg)
        return result.get('data')

    def rollback_snapshot(self, snap_id):
        url = "/FSSNAPSHOT/ROLLBACK_FSSNAPSHOT"
        data = jsonutils.dumps({"ID": snap_id})
        result = self.call(url, data, "PUT")

        msg = _('Failed to rollback snapshot %s.') % snap_id
        self._assert_rest_result(result, msg)

    def get_controller_by_name(self, name):
        controlers = self._get_all_controllers()
        for controller in controlers:
            if controller.get('LOCATION') == name:
                return controller.get('ID')

        return None

    def _get_all_controllers(self):
        url = "/controller"
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, _('Get all controller error.'))
        return result.get('data', [])

    def split_clone_fs(self, fs_id):
        data = jsonutils.dumps(
            {"ID": fs_id,
             "SPLITENABLE": True,
             "SPLITSPEED": 4,
             }
        )
        result = self.call("/filesystem_split_switch", data, "PUT")

        msg = _('Split clone fs %s error.') % fs_id
        self._assert_rest_result(result, msg)

    def create_hypermetro_pair(self, params):
        data = jsonutils.dumps(params)
        result = self.call("/HyperMetroPair", data, "POST")
        self._assert_rest_result(result, 'Create HyperMetro pair %s error.'
                                 % params)
        return result['data']

    def get_hypermetro_pair_by_id(self, pair_id):
        url = "/HyperMetroPair/%s" % pair_id
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Get HyperMetro pair %s error.'
                                 % pair_id)
        return result['data']

    def suspend_hypermetro_pair(self, pair_id):
        params = {"ID": pair_id}
        data = jsonutils.dumps(params)
        url = "/HyperMetroPair/disable_hcpair"
        result = self.call(url, data, "PUT")
        self._assert_rest_result(result, 'Suspend HyperMetro pair %s error.'
                                 % pair_id)

    def sync_hypermetro_pair(self, pair_id):
        params = {"ID": pair_id}
        data = jsonutils.dumps(params)
        result = self.call("/HyperMetroPair/synchronize_hcpair", data, "PUT")
        self._assert_rest_result(result, 'Sync HyperMetro pair %s error.'
                                 % pair_id)

    def delete_hypermetro_pair(self, pair_id):
        url = "/HyperMetroPair/%s" % pair_id
        result = self.call(url, None, "DELETE")
        if result['error']['code'] == constants.ERROR_HYPERMETRO_NOT_EXIST:
            LOG.warning('Hypermetro pair %s to delete not exist.', pair_id)
            return
        self._assert_rest_result(result, 'Delete HyperMetro pair %s error.'
                                 % pair_id)

    def get_hypermetro_domain_id(self, domain_name):
        result = self.call("/HyperMetroDomain?range=[0-100]", None, "GET")
        self._assert_rest_result(result, "Get HyperMetro domains info error.")
        for item in result.get("data", []):
            if item.get("NAME") == domain_name:
                return item.get("ID")

    def get_hypermetro_vstore_id(self, domain_name, local_vstore_name,
                                 remote_vstore_name):
        result = self.call("/vstore_pair?range=[0-100]", None, "GET")
        self._assert_rest_result(result, "Get Metro vStore pair id error.")
        for item in result.get("data", []):
            if item.get("DOMAINNAME") == domain_name and item.get(
                    "LOCALVSTORENAME") == local_vstore_name and item.get(
                    "REMOTEVSTORENAME") == remote_vstore_name:
                return item.get("ID")
        return None

    def get_hypermetro_vstore_by_pair_id(self, vstore_pair_id):
        url = "/vstore_pair/%s" % vstore_pair_id
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, "Get HyperMetro vStore pair info "
                                         "by id error.")
        return result["data"]
