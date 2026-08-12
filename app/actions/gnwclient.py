import httpx
import json
import logging
import asyncio
import pydantic
import random
import re
import backoff
from enum import Enum
from urllib.parse import urlparse, parse_qs

import httpx
from pydantic import HttpUrl
from datetime import datetime, timedelta, timezone
from typing import ClassVar, Optional, List, Set, Tuple, Dict, Any


logger = logging.getLogger(__name__)


def random_string(n=4):
    return "".join(random.sample([chr(x) for x in range(97, 97 + 26)], n))


class DatasetStatus(pydantic.BaseModel):
    latest_updated_on: datetime = pydantic.Field(
        default_factory=lambda: datetime(1970, 1, 1, tzinfo=timezone.utc)
    )
    version: Optional[str] = ""
    dataset: Optional[str] = ""

    class Config:
        json_encoders = {datetime: lambda val: val.isoformat()}


class DataAPIToken(pydantic.BaseModel):
    access_token: str
    token_type: str

    # In case GFW's Oauth2 token does not provide expiration, we assume it's good for a day
    expires_in: int = 86400
    expires_at: datetime = None

    @pydantic.root_validator
    def calculator(cls, values):
        expires_at = values.get("expires_at")
        if not expires_at:
            values["expires_at"] = datetime.now(tz=timezone.utc) + timedelta(
                seconds=values["expires_in"]
            )
        return values


class DatasetResponseItem(pydantic.BaseModel):
    created_on: datetime
    updated_on: datetime
    dataset: str
    version: str
    is_latest: bool
    is_mutable: bool

    @pydantic.validator("created_on", "updated_on")
    def clean_timestamp(val):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)


class DataAPIKey(pydantic.BaseModel):
    created_on: datetime
    updated_on: datetime
    alias: str
    user_id: str
    api_key: str
    organization: str
    email: str
    domains: List[str]
    expires_on: datetime

    @pydantic.validator("created_on", "updated_on", "expires_on")
    def sanitize_datetimes(val):
        if not val.tzinfo:
            return val.replace(tzinfo=timezone.utc)
        return val


class DataAPIKeyResponse(pydantic.BaseModel):
    data: DataAPIKey


class DataAPIKeysResponse(pydantic.BaseModel):
    data: List[DataAPIKey]


class DataAPIAuthException(Exception):
    pass


class DataAPIQueryException(Exception):
    pass

class GFWClientException(Exception):
    pass


class DownloadLinkExpiredException(Exception):
    """Raised when a batch job's download link has expired."""
    pass


def is_download_link_expired(download_link: str) -> bool:
    """
    Check if a download link has expired by parsing its Expires parameter.

    The download_link contains an 'Expires' query parameter with a Unix timestamp.
    Returns True if the link has expired, False otherwise.
    """
    try:
        parsed = urlparse(download_link)
        params = parse_qs(parsed.query)

        if 'Expires' in params:
            expires_timestamp = int(params['Expires'][0])
            expires_at = datetime.fromtimestamp(expires_timestamp, tz=timezone.utc)
            return datetime.now(tz=timezone.utc) >= expires_at

        # If no Expires param, assume it's valid
        return False
    except (ValueError, KeyError, IndexError):
        # If we can't parse, assume it's valid and let the request fail naturally
        return False


def get_download_link_expiry(download_link: str) -> Optional[datetime]:
    """
    Get the expiry datetime of a download link.

    Returns the expiry datetime or None if it can't be parsed.
    """
    try:
        parsed = urlparse(download_link)
        params = parse_qs(parsed.query)

        if 'Expires' in params:
            expires_timestamp = int(params['Expires'][0])
            return datetime.fromtimestamp(expires_timestamp, tz=timezone.utc)

        return None
    except (ValueError, KeyError, IndexError):
        return None


class AOIAttributes(pydantic.BaseModel):
    name: Optional[str]
    application: Optional[str]
    geostore: Optional[str] = None
    createdAt: datetime
    updatedAt: datetime
    datasets: Optional[List[str]] = []
    use: dict
    env: str
    tags: Optional[List[str]]
    status: str
    public: bool
    fireAlerts: Optional[bool] = True
    deforestationAlerts: Optional[bool] = True
    webhookUrl: Optional[str]
    monthlySummary: Optional[bool] = False
    subscriptionId: Optional[str]
    email: Optional[str]
    language: Optional[str]
    confirmed: Optional[bool] = True


class AOIData(pydantic.BaseModel):
    type: str
    id: str
    attributes: AOIAttributes


class JobResponse(pydantic.BaseModel):
    class Data(pydantic.BaseModel):
        job_id: str
        job_link: Optional[HttpUrl] = None  # May be null when polling job status
        status: str
        message: Optional[str] = None
        download_link: Optional[HttpUrl] = None
        failed_geometries_link: Optional[HttpUrl] = None
        progress: Optional[str] = None  # May not be present in all responses

    data: Data
    status: str # "success" or ?

class GeostoreAttributes(pydantic.BaseModel):
    geojson: dict
    hash: str
    provider: dict
    areaHa: float
    bbox: List[float]
    lock: bool
    info: dict


class Geostore(pydantic.BaseModel):
    type: str = pydantic.Field("geoStore", const=True)
    id: str
    attributes: GeostoreAttributes
    area: float = 0


class DatasetMetadata(pydantic.BaseModel):
    created_on: Optional[datetime] = None
    updated_on: Optional[datetime] = None
    resolution: Optional[int] = None
    geographic_coverage: Optional[str] = None
    update_frequency: Optional[str] = None
    scale: Optional[str] = None
    citation: Optional[str] = None
    title: Optional[str] = None
    source: Optional[str] = None
    license: Optional[str] = None
    data_language: Optional[str] = None
    overview: Optional[str] = None
    function: Optional[str] = None
    cautions: Optional[str] = None
    key_restrictions: Optional[str] = None
    tags: Optional[List[str]] = pydantic.Field(default_factory=list)
    why_added: Optional[Any] = None
    learn_more: Optional[Any] = None
    id: Optional[str] = None

    @pydantic.validator("created_on", "updated_on")
    def clean_timestamp(val):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)


class Dataset(pydantic.BaseModel):
    created_on: datetime
    updated_on: datetime
    dataset: str
    is_downloadable: bool
    metadata: DatasetMetadata
    versions: Optional[List[str]] = pydantic.Field(default_factory=list)

    @pydantic.validator("created_on", "updated_on")
    def clean_timestamp(val):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)


class DatasetsResponse(pydantic.BaseModel):
    data: List[Dataset] = []
    status: Optional[str] = None

class DatasetResponse(pydantic.BaseModel):
    data: Optional[Dataset] = None
    status: Optional[str] = None

class DatasetField(pydantic.BaseModel):
    name: str
    alias: str
    description: Any
    data_type: str
    unit: Any
    is_feature_info: bool
    is_filter: bool


class DatasetFields(pydantic.BaseModel):
    data: Optional[List[DatasetField]] = None
    status: Optional[str] = None


def backoff_hdlr(details):
    logger.warning("Backing off {wait:0.1f} seconds after {tries} tries "
           "calling function {target} with args {args} and kwargs "
           "{kwargs}".format(**details))


# Custom backoff strategy starting at 5, incrementing by 10, and capping at 45
def custom_backoff():
    delay = 5
    while delay <= 45:
        yield delay
        delay += 10

DEFAULT_REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=3.1)
class DataAPI:

    DATA_API_URL = "https://data-api.globalforestwatch.org"
    RESOURCE_WATCH_URL = "https://api.resourcewatch.org"

    def __init__(self, *, username: str = None, password: str = None):

        self._username = username
        self._password = password
        self._auth_gen = None
        self._api_keys = []

    @backoff.on_exception(backoff.expo, (httpx.TimeoutException, httpx.HTTPStatusError), max_tries=3)
    async def get_access_token(self):

        async with httpx.AsyncClient(timeout=DEFAULT_REQUEST_TIMEOUT) as client:
            response = await client.post(
                url=f"{self.DATA_API_URL}/auth/token",
                data={"username": self._username, "password": self._password},
                follow_redirects=True
            )

            # Raise HTTPStatusError for non-success status codes so backoff can retry
            response.raise_for_status()

            dapitoken = DataAPIToken.parse_obj(response.json()["data"])
            return dapitoken

    async def auth_generator(self):
        """
        Simple generator to provide a header and keep it for a designated TTL.
        """
        expire_at = datetime(1970, 1, 1, tzinfo=timezone.utc)

        while True:
            present = datetime.now(tz=timezone.utc)
            try:
                if expire_at <= present:
                    token = await self.get_access_token()

                    ttl_seconds = token.expires_in - 5
                    expire_at = present + timedelta(seconds=ttl_seconds)
                if logger.isEnabledFor(logging.DEBUG):
                    ttl = (expire_at - present).total_seconds()
                    logger.debug(f"Using cached auth, expires in {ttl} seconds.")

            except httpx.HTTPStatusError as e:
                logger.exception(f"Failed to authenticate with GFW Data API for user {self._username}: {e}")
                raise e
            else:
                yield token

    async def get_auth_header(self, refresh=False) -> Dict[str, str]:
        if not self._auth_gen or refresh:
            self._auth_gen = self.auth_generator()
        try:
            token = await anext(self._auth_gen)
        except StopIteration:
            self._auth_gen = self.auth_generator()
            token = await anext(self._auth_gen)
        return {"authorization": f"{token.token_type} {token.access_token}"}

    @backoff.on_exception(backoff.expo, (httpx.TimeoutException, httpx.HTTPStatusError), factor=5, max_tries=3)
    async def create_api_key(self) -> DataAPIKey:

        headers = await self.get_auth_header()

        payload = {
            "alias": "-".join((self._username, random_string())),
            "email": self._username,
            "organization": "EarthRanger",
            "domains": [],
        }

        async with httpx.AsyncClient(timeout=DEFAULT_REQUEST_TIMEOUT) as client:
            response = await client.post(
                url=f"{self.DATA_API_URL}/auth/apikey",
                headers=headers,
                json=payload,
                follow_redirects=True
            )

            if httpx.codes.is_success(response.status_code):
                return DataAPIKeyResponse.parse_obj(response.json()).data

            response.raise_for_status()

    @backoff.on_exception(backoff.expo, (httpx.TimeoutException, httpx.HTTPStatusError), factor=5, max_tries=3)
    async def get_api_keys(self) -> List[DataAPIKey]:

        headers = await self.get_auth_header()

        async with httpx.AsyncClient(timeout=DEFAULT_REQUEST_TIMEOUT) as client:
            response = await client.get(
                f"{self.DATA_API_URL}/auth/apikeys", headers=headers,
                follow_redirects=True
            )

            response.raise_for_status()

            if httpx.codes.is_success(response.status_code):
                response = DataAPIKeysResponse.parse_obj(response.json())
                return response.data

    async def get_a_valid_api_key(self) -> DataAPIKey:

        # If we already have API keys, filter to only those that are still valid.
        if not self._api_keys:
            try:
                self._api_keys = await self.get_api_keys()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    # No API keys exist, we'll create one below
                    self._api_keys = []
                else:
                    # Re-raise other HTTP errors
                    raise

        # Filter to only those that are still valid.
        if good_api_keys := [
            api_key for api_key in self._api_keys
            if api_key.expires_on > datetime.now(tz=timezone.utc)
        ]:
            return good_api_keys[-1]

        data = await self.create_api_key()
        self._api_keys.append(data)
        return data

    async def delete_api_key(self, api_key: str):
        headers = await self.get_auth_header()
        async with httpx.AsyncClient(timeout=DEFAULT_REQUEST_TIMEOUT) as client:
            response = await client.delete(
                f"{self.DATA_API_URL}/auth/apikey/{api_key}", headers=headers, follow_redirects=True
            )
            response.raise_for_status()

    async def validate_api_key(self, api_key: str):
        headers = await self.get_auth_header()
        async with httpx.AsyncClient(timeout=DEFAULT_REQUEST_TIMEOUT) as client:
            response = await client.get(
                f"{self.DATA_API_URL}/auth/apikey/{api_key}/validate", headers=headers, follow_redirects=True
            )
            response.raise_for_status()
            return response.json()

    @backoff.on_exception(backoff.constant, httpx.HTTPError, max_tries=3, interval=10)
    async def get_aoi(self, aoi_id: str):
        async with httpx.AsyncClient(timeout=DEFAULT_REQUEST_TIMEOUT) as client:
            response = await client.get(
                url=f"{self.RESOURCE_WATCH_URL}/v2/area/{aoi_id}",
                headers=await self.get_auth_header()
,
                follow_redirects=True
            )
            response.raise_for_status()
            response = response.json()

            try:
                return AOIData.parse_obj(response.get("data"))
            except pydantic.ValidationError as e:
                logger.exception(f"Unexpected error parsing AOI data: {e}")

            logger.error(
                "Failed to get AOI for id: %s. result is: %s", aoi_id, response.text[:250]
            )

            raise GFWClientException(f"Failed to get AOI for id: '{aoi_id}'")


    @backoff.on_exception(backoff.constant, httpx.HTTPError, max_tries=3, interval=10)
    async def get_geostore(self, geostore_id=None):
        async with httpx.AsyncClient(timeout=DEFAULT_REQUEST_TIMEOUT) as client:
            response = await client.get(
                url=f"{self.RESOURCE_WATCH_URL}/v2/geostore/{geostore_id}",
                follow_redirects=True,
                headers=await self.get_auth_header()
            )
            response.raise_for_status()
            response = response.json()

            try:
                return Geostore.parse_obj(response.get("data"))
            except pydantic.ValidationError as e:
                logger.exception(f"Unexpected error parsing Geostore data: {e}")

            logger.error(
                "Failed to get Geostore for id: %s. result is: %s", geostore_id, response.text[:250]
            )

            raise GFWClientException(f"Failed to get Geostore for id: '{geostore_id}'")


    @backoff.on_exception(backoff.expo, (httpx.TimeoutException, httpx.HTTPStatusError), max_tries=3, factor=3)
    async def get_dataset_metadata(self, dataset: str = "", version: str = "latest"):

        api_key = await self.get_a_valid_api_key()
        headers = {"x-api-key": api_key.api_key}

        async with httpx.AsyncClient(timeout=DEFAULT_REQUEST_TIMEOUT) as client:
            response = await client.get(
                f"{self.DATA_API_URL}/dataset/{dataset}/{version}",
                headers=headers,
                follow_redirects=True
            )

            if httpx.codes.is_success(response.status_code):
                data = response.json()
                return DatasetResponseItem.parse_obj(data.get("data"))


    @backoff.on_exception(backoff.constant, httpx.HTTPError, max_tries=3, interval=10)
    async def aoi_from_url(self, url) -> str:
        """
        Extracts the AOI ID from a GFW share link URL.
        """

        URL_PATTERN = r".*(?:globalforestwatch|globalnaturewatch)\.org.*aoi/([^/]+).*"
        if matches := re.match(URL_PATTERN, url):
            return matches[1]

        async with httpx.AsyncClient(timeout=DEFAULT_REQUEST_TIMEOUT) as client:
            head = await client.head(url, follow_redirects=True)

        if matches := re.match(URL_PATTERN, str(head.url)):
            return matches[1]

        logger.error("Unable to parse AOI from URL: %s (resolved to: %s)", url, head.url)
        raise GFWClientException(f"Unable to parse AOI from URL: '{url}' (resolved to: '{head.url}')")


    @backoff.on_exception(backoff.constant, httpx.HTTPError, max_tries=3, interval=10)
    async def get_datasets(self):
        async with httpx.AsyncClient(timeout=DEFAULT_REQUEST_TIMEOUT) as client:
            response = await client.get(
                f"{self.DATA_API_URL}/datasets",
                follow_redirects=True
            )
            response.raise_for_status()
            content = response.json()
            datasets_response = DatasetsResponse.parse_obj(content)
            return datasets_response.data

    @backoff.on_exception(backoff.constant, httpx.HTTPError, max_tries=3, interval=10)
    async def get_dataset_fields(self, *, dataset:str, version:str="latest"):
        async with httpx.AsyncClient(timeout=DEFAULT_REQUEST_TIMEOUT) as client:
            response = await client.get(
                f"{self.DATA_API_URL}/dataset/{dataset}/{version}/fields",
                follow_redirects=True
            )
            response.raise_for_status()
            content = response.json()
            fields_response = DatasetFields.parse_obj(content)

            return fields_response.data

    @backoff.on_exception(custom_backoff, (httpx.TimeoutException, httpx.HTTPStatusError),
                          max_tries=3,
                          on_backoff=backoff_hdlr)
    async def query_sync(
        self,
        *,
        dataset: str,
        sql: str,
        geostore_id: str
    ):

        api_key = await self.get_a_valid_api_key()
        headers = {"x-api-key": api_key.api_key}

        payload = {
            'geostore_id': geostore_id,
            'sql': sql
        }

        async def fn():
            async with httpx.AsyncClient(timeout=DEFAULT_REQUEST_TIMEOUT) as client:

                response = await client.get(f"{self.DATA_API_URL}/dataset/{dataset}/latest/query/json",
                    headers=headers,
                    params=payload,
                    follow_redirects=True
                )

                if httpx.codes.is_success(response.status_code):
                    data = response.json()
                    data_len = len(data.get("data"))
                    logger.info(f"Extracted {data_len} rows from dataset {dataset}, geostore_id: {geostore_id}.")
                    return data.get("data", [])
                else:
                    logger.error(
                        f"Failed getting data for dataset {dataset}. status: {response.status_code}, text: {response.text}",
                        extra=payload,
                    )
                    response.raise_for_status()

        return await fn()

    @backoff.on_exception(custom_backoff, (httpx.TimeoutException, httpx.HTTPStatusError),
                          max_tries=3,
                          on_backoff=backoff_hdlr)
    async def query_batch(
        self,
        *,
        dataset: str,
        sql: str,
        geostore_ids: List[str]
    ) -> JobResponse.Data:

        api_key = await self.get_a_valid_api_key()
        headers = {"x-api-key": api_key.api_key}

        payload = {
            'geostore_ids': geostore_ids,
            'sql': sql,
        }

        async def fn():
            async with httpx.AsyncClient(timeout=DEFAULT_REQUEST_TIMEOUT) as client:

                response = await client.post(f"{self.DATA_API_URL}/dataset/{dataset}/latest/query/batch",
                    headers=headers,
                    json=payload,
                    follow_redirects=True
                )

                if httpx.codes.is_success(response.status_code):
                    data = response.json()
                    return JobResponse.parse_obj(data).data
                else:
                    logger.error(
                        f"Failed getting data for dataset {dataset}. status: {response.status_code}, text: {response.text}",
                        extra=payload,
                    )
                    response.raise_for_status()

        return await fn()

    @backoff.on_exception(backoff.expo, (httpx.TimeoutException, httpx.HTTPStatusError), max_tries=3, factor=3)
    async def get_job_status(self, job_link: str) -> JobResponse.Data:
        """
        Poll a batch job's status using its job_link.

        Returns the job data with status, download_link, etc.
        """
        api_key = await self.get_a_valid_api_key()
        headers = {"x-api-key": api_key.api_key}

        async with httpx.AsyncClient(timeout=DEFAULT_REQUEST_TIMEOUT) as client:
            response = await client.get(
                job_link,
                headers=headers,
                follow_redirects=True
            )
            response.raise_for_status()

            if httpx.codes.is_success(response.status_code):
                data = response.json()
                return JobResponse.parse_obj(data).data

    @backoff.on_exception(backoff.expo, (httpx.TimeoutException,), max_tries=3, factor=3)
    async def download_job_results(self, download_link: str) -> List[dict]:
        """
        Download JSON results from a completed batch job's download_link.

        The response is an array of objects with 'result' lists and 'fid' strings:
        [
            {"result": [{alert}, {alert}, ...], "fid": "..."},
            {"result": [{alert}, {alert}, ...], "fid": "..."},
            ...
        ]

        Returns a flattened list of alert records from all result arrays.

        Raises:
            DownloadLinkExpiredException: If the download link has expired (either
                detected via Expires parameter or 403 response).
        """
        # Check if the download link has expired before making the request
        if is_download_link_expired(download_link):
            expiry = get_download_link_expiry(download_link)
            raise DownloadLinkExpiredException(
                f"Download link expired at {expiry.isoformat() if expiry else 'unknown time'}"
            )

        api_key = await self.get_a_valid_api_key()
        headers = {"x-api-key": api_key.api_key}

        async with httpx.AsyncClient(timeout=DEFAULT_REQUEST_TIMEOUT) as client:
            response = await client.get(
                download_link,
                headers=headers,
                follow_redirects=True
            )

            # Handle 403 Forbidden as expired link
            if response.status_code == 403:
                raise DownloadLinkExpiredException(
                    f"Download link returned 403 Forbidden (likely expired)"
                )

            response.raise_for_status()

            if httpx.codes.is_success(response.status_code):
                data = response.json()

                # Response is an array of objects with 'result' lists
                # Flatten all result arrays into a single list of alerts
                if isinstance(data, list):
                    alerts = []
                    for item in data:
                        if isinstance(item, dict) and 'result' in item:
                            alerts.extend(item['result'])
                    return alerts

                return []
