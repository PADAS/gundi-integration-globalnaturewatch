import pytest
import datetime as _datetime


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
