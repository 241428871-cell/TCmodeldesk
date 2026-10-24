"""Explicit account balance reads, independent of model creation or execution."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from threading import Lock
from typing import Final

import connection_accounts
import deepseek_billing
import model_registry as registry
from native_metadata import JsonMap

_LOCK: Final = Lock()
_CACHE: Final[dict[str, tuple[str, JsonMap]]] = {}


def balance(ident: str, *, force: bool = False) -> JsonMap:
    """Only a refresh contacts /user/balance; listing stays offline."""
    account = connection_accounts.get(ident)
    identity = sha256(account.model_dump_json().encode()).hexdigest()
    with _LOCK:
        previous = _CACHE.get(ident)
    cached = deepcopy(previous[1]) if previous and previous[0] == identity else {}
    if not force:
        return cached or {'state': 'unknown', 'message': '尚未读取账户余额', 'balance_infos': []}
    if not deepseek_billing.is_official({'api_base': account.api_base}):
        raise connection_accounts.AccountError('此账户没有已接入的官方余额接口')
    try:
        result: JsonMap = {'state': 'ready', **deepseek_billing.read_balance(
            connection_accounts.connection(ident, secret=True)), 'updated_at': registry.now()}
    except (OSError, ValueError, TimeoutError) as error:
        result = {'state': 'error', 'message': deepseek_billing.balance_error(error),
                  'balance_infos': [], 'updated_at': registry.now()}
    # A rotated key must not receive a result from an earlier in-flight request.
    if sha256(connection_accounts.get(ident).model_dump_json().encode()).hexdigest() != identity:
        return {'state': 'unknown', 'message': '账户已变更，请重新读取余额', 'balance_infos': []}
    with _LOCK:
        _CACHE[ident] = identity, result
    return deepcopy(result)
