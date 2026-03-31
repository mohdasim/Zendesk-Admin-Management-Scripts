import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass(frozen=True)
class ZendeskConfig:
    """Zendesk API configuration loaded from environment variables."""

    subdomain: str
    email: str
    api_token: str

    @property
    def base_url(self) -> str:
        return f"https://{self.subdomain}.zendesk.com"

    @property
    def auth(self) -> tuple:
        return (f"{self.email}/token", self.api_token)


def load_config(env_path: str = ".env") -> ZendeskConfig:
    """Load Zendesk configuration from environment variables or .env file.

    Required environment variables:
        ZENDESK_SUBDOMAIN: Your Zendesk subdomain (e.g. 'mycompany')
        ZENDESK_EMAIL: Admin email address
        ZENDESK_API_TOKEN: API token from Admin Center
    """
    load_dotenv(dotenv_path=env_path)

    subdomain = os.environ.get("ZENDESK_SUBDOMAIN")
    email = os.environ.get("ZENDESK_EMAIL")
    api_token = os.environ.get("ZENDESK_API_TOKEN")

    missing = [
        name
        for name, value in {
            "ZENDESK_SUBDOMAIN": subdomain,
            "ZENDESK_EMAIL": email,
            "ZENDESK_API_TOKEN": api_token,
        }.items()
        if not value
    ]

    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Set them in your environment or in a .env file.\n"
            f"See .env.example for reference."
        )

    return ZendeskConfig(subdomain=subdomain, email=email, api_token=api_token)
