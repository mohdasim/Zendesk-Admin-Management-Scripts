import time
import logging
from typing import Iterator, Any

import requests

from .config import ZendeskConfig

logger = logging.getLogger(__name__)


class RateLimitError(Exception):
    """Raised when the Zendesk API rate limit is exceeded after max retries."""


class ZendeskClient:
    """HTTP client for Zendesk REST API v2 with pagination and rate-limit handling."""

    MAX_RETRIES = 5

    def __init__(self, config: ZendeskConfig):
        self.config = config
        self.session = requests.Session()
        self.session.auth = config.auth
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Execute an HTTP request with automatic rate-limit retry.

        On a 429 response, waits for the duration specified in the Retry-After
        header before retrying, up to MAX_RETRIES times.
        """
        for attempt in range(1, self.MAX_RETRIES + 1):
            response = self.session.request(method, url, **kwargs)

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                logger.warning(
                    "Rate limited (429). Waiting %ds before retry %d/%d",
                    retry_after, attempt, self.MAX_RETRIES,
                )
                time.sleep(retry_after)
                continue

            response.raise_for_status()
            return response

        raise RateLimitError(
            f"Rate limited after {self.MAX_RETRIES} retries on {url}"
        )

    def _build_url(self, endpoint: str) -> str:
        """Convert a relative endpoint to an absolute URL."""
        if endpoint.startswith("http"):
            return endpoint
        return f"{self.config.base_url}{endpoint}"

    def get(self, endpoint: str, params: dict | None = None) -> dict:
        """Send a GET request and return the JSON response."""
        url = self._build_url(endpoint)
        return self._request("GET", url, params=params).json()

    def put(self, endpoint: str, json: dict | None = None) -> dict:
        """Send a PUT request and return the JSON response."""
        url = self._build_url(endpoint)
        return self._request("PUT", url, json=json).json()

    def paginate(
        self,
        endpoint: str,
        key: str,
        params: dict | None = None,
    ) -> Iterator[dict]:
        """Iterate over all records from a paginated Zendesk API endpoint.

        Supports both cursor-based pagination (preferred) and offset-based
        pagination (legacy fallback).

        Args:
            endpoint: API endpoint path (e.g. '/api/v2/triggers')
            key: JSON key containing the records (e.g. 'triggers')
            params: Optional query parameters for the first request

        Yields:
            Individual record dicts from the paginated response.
        """
        params = dict(params or {})
        params.setdefault("page[size]", 100)

        url = self._build_url(endpoint)
        is_first_request = True

        while url:
            response = self._request(
                "GET", url, params=params if is_first_request else None
            )
            data = response.json()
            is_first_request = False

            records = data.get(key, [])
            yield from records

            logger.debug(
                "Fetched %d %s records (total so far in this page)",
                len(records), key,
            )

            # Cursor-based pagination: meta.has_more + links.next
            meta = data.get("meta", {})
            links = data.get("links", {})
            if meta.get("has_more") and links.get("next"):
                url = links["next"]
            # Offset-based pagination fallback: next_page
            elif data.get("next_page"):
                url = data["next_page"]
            else:
                url = None
