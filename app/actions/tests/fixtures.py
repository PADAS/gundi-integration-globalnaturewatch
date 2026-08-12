import json
import pytest
import datetime as _datetime
from unittest.mock import AsyncMock, MagicMock


TOKEN_PAYLOAD = {"data": {"access_token": "tok123", "token_type": "bearer", "expires_in": 3600}}

def api_key_item(created_delta_days=0, expires_delta_days=365, key="key-abc"):
    now = _datetime.datetime.now(tz=_datetime.timezone.utc)
    return {
        "created_on": (now + _datetime.timedelta(days=created_delta_days)).isoformat(),
        "updated_on": now.isoformat(),
        "alias": "user-abcd", "user_id": "u1", "api_key": key,
        "organization": "EarthRanger", "email": "user@example.com", "domains": [],
        "expires_on": (now + _datetime.timedelta(days=expires_delta_days)).isoformat(),
    }


class StubPipeline:
    def __init__(self, store):
        self.store, self.ops = store, []
    def setex(self, key, ttl, value): self.ops.append(("setex", key, ttl, value))
    def sadd(self, key, member): self.ops.append(("sadd", key, member))
    def delete(self, key): self.ops.append(("delete", key))
    def srem(self, key, member): self.ops.append(("srem", key, member))
    def zscore(self, key, member): self.ops.append(("zscore", key, member))
    async def execute(self):
        results = []
        for op in self.ops:
            if op[0] == "setex":
                self.store[op[1]] = op[3]
                results.append(None)
            elif op[0] == "sadd":
                self.store.setdefault(op[1], set()).add(op[2])
                results.append(None)
            elif op[0] == "delete":
                self.store.pop(op[1], None)
                results.append(None)
            elif op[0] == "srem":
                self.store.get(op[1], set()).discard(op[2])
                results.append(None)
            elif op[0] == "zscore":
                results.append(self.store.get(op[1], {}).get(op[2]))
        return results


def _zremrangebyscore(store, key, min_score, max_score):
    zset = store.get(key)
    if not zset:
        return
    lo = float("-inf") if min_score == "-inf" else float(min_score)
    hi = float("inf") if max_score == "+inf" else float(max_score)
    for member in [m for m, score in zset.items() if lo <= score <= hi]:
        del zset[member]


@pytest.fixture
def stub_state_manager():
    store = {}
    manager = MagicMock()
    manager.db_client.setex = AsyncMock(side_effect=lambda k, ttl, v: store.__setitem__(k, v))
    manager.db_client.get = AsyncMock(side_effect=lambda k: store.get(k))
    manager.db_client.set = AsyncMock(side_effect=lambda k, v: store.__setitem__(k, v))
    manager.db_client.exists = AsyncMock(side_effect=lambda k: 1 if k in store else 0)
    manager.db_client.smembers = AsyncMock(side_effect=lambda k: store.get(k, set()))
    manager.db_client.srem = AsyncMock(side_effect=lambda k, *m: [store.get(k, set()).discard(x) for x in m] and None)
    manager.db_client.zadd = AsyncMock(side_effect=lambda k, mapping: store.setdefault(k, {}).update(mapping))
    manager.db_client.zscore = AsyncMock(side_effect=lambda k, m: store.get(k, {}).get(m))
    manager.db_client.zremrangebyscore = AsyncMock(
        side_effect=lambda k, min_score, max_score: _zremrangebyscore(store, k, min_score, max_score))
    manager.db_client.pipeline = MagicMock(side_effect=lambda transaction=True: StubPipeline(store))
    manager.get_state = AsyncMock(return_value=None)
    manager.set_state = AsyncMock()
    manager._store = store
    return manager
