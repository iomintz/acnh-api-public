# © 2020 io mintz <io@mintz.cc>

# ACNH API is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.

# ACNH API is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with ACNH API. If not, see <https://www.gnu.org/licenses/>.

import enum
import logging
import re
from typing import ClassVar

import toml

# Based on code provided by Yannik Marchand under the MIT License.
# Copyright (c) 2017 Yannik Marchand

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from nintendo.baas import BAASClient
from nintendo.dauth import DAuthClient
from nintendo.aauth import AAuthClient
from nintendo.switch import ProdInfo, KeySet, TicketList
from nintendo.nex.backend import BackEndClient
from nintendo.nex.authentication import AuthenticationInfo
from nintendo.nex import matchmaking
from nintendo.games import ACNH

from utils import load_cached

class DodoCodeError(Exception):
	code: ClassVar[int]
	message: ClassVar[str]

	def __int__(self):
		return self.code

	def to_dict(self):
		return {'error': self.message, 'error_code': self.code}

class UnknownDodoCodeError(DodoCodeError):
	code = 1
	message = 'unknown dodo code'

DODO_CODE_RE = re.compile('[ABCDEFGHJKLMNPQRSTUVWXY0-9]{5}')

class InvalidDodoCodeError(DodoCodeError):
	code = 2
	message = 'invalid dodo code'
	regex = DODO_CODE_RE.pattern

	def to_dict(self):
		d = super().to_dict()
		d['validation_regex'] = self.regex
		return d

logging.basicConfig(level=logging.WARNING)

SYSTEM_VERSION = 1003  # 10.0.3
HOST = 'g%08x-lp1.s.n.srv.nintendo.net' % ACNH.GAME_SERVER_ID
PORT = 443

with open('config.toml') as f:
	config = toml.load(f)

keys = KeySet(config['keyset-path'])
prodinfo = ProdInfo(keys, config['prodinfo-path'])

cert = prodinfo.get_ssl_cert()
pkey = prodinfo.get_ssl_key()

with open(config['ticket-path'], 'rb') as f:
	ticket = f.read()

def authenticate():
	dauth = DAuthClient(keys)
	dauth.set_certificate(cert, pkey)
	dauth.set_system_version(SYSTEM_VERSION)
	device_token = load_cached('tokens/dauth-token.txt', lambda: dauth.device_token()['device_auth_token'])

	aauth = AAuthClient()
	aauth.set_system_version(SYSTEM_VERSION)
	app_token = load_cached('tokens/aauth-token.txt', lambda: aauth.auth_digital(
		ACNH.TITLE_ID, ACNH.TITLE_VERSION,
		device_token, ticket
	)['application_auth_token'])

	baas = BAASClient()
	baas.set_system_version(SYSTEM_VERSION)

	def get_id_token():
		baas.authenticate(device_token)
		response = baas.login(config['baas-user-id'], config['baas-password'], app_token)
		return toml.dumps({'user-id': int(response['user']['id'], base=16), 'id-token': response['idToken']})

	resp = toml.loads(load_cached('tokens/id-token.txt', get_id_token, duration=3 * 60 * 60))
	return resp['user-id'], resp['id-token']

def connect(user_id, id_token):
	# connect to game server
	backend = BackEndClient('switch.cfg')
	backend.configure(ACNH.ACCESS_KEY, ACNH.NEX_VERSION, ACNH.CLIENT_VERSION)
	backend.connect(HOST, PORT)

	# log in on game server
	auth_info = AuthenticationInfo()
	auth_info.token = id_token
	auth_info.ngs_version = 4  # Switch
	auth_info.token_type = 2
	backend.login(str(user_id), auth_info=auth_info)

	return backend

def _search_dodo_code(backend: BackEndClient, dodo_code: str):
	mm = matchmaking.MatchmakeExtensionClient(backend.secure_client)

	param = matchmaking.MatchmakeSessionSearchCriteria()
	param.attribs = ['', '', '', '', '', '']
	param.game_mode = '2'
	param.min_players = '1'
	param.max_players = '1,8'
	param.matchmake_system = '1'
	param.vacant_only = False
	param.exclude_locked = True
	param.exclude_non_host_pid = True
	param.selection_method = 0
	param.vacant_participants = 1
	param.exclude_user_password = True
	param.exclude_system_password = True
	param.refer_gid = 0
	param.codeword = dodo_code

	sessions = mm.browse_matchmake_session_no_holder_no_result_range(param)
	if not sessions:
		raise UnknownDodoCodeError

	session = sessions[0]
	data = session.application_data
	return dict(id=session.id, active_players=session.player_count, name=data[12:32].decode('utf-16'))

def search_dodo_code(dodo_code: str):
	if not DODO_CODE_RE.fullmatch(dodo_code):
		raise InvalidDodoCodeError

	user_id, id_token = authenticate()
	backend = connect(user_id, id_token)
	rv = _search_dodo_code(backend, dodo_code)
	# disconnect from game server
	backend.close()
	return rv