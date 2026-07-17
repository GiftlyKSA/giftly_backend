"""The single place integration clients are constructed (SPEC SECTION 5.2).

Layer 2 of the production interlock: ``build_clients`` is the ONLY place a client
is built, and it raises at boot if ENVIRONMENT=production would select any fake.
The Fakes additionally guard their own ``__init__`` (layer 3), so a production fake
is impossible at two independent layers.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Environment, Settings
from app.integrations.email.base import EmailClient
from app.integrations.email.fake import FakeEmailClient
from app.integrations.email.sndr_client import SndrEmailClient
from app.integrations.paylink.base import PaymentGateway
from app.integrations.paylink.fake import FakePaylinkClient
from app.integrations.paylink.real import RealPaylinkClient
from app.integrations.push.base import PushClient
from app.integrations.push.fake import FakePushClient
from app.integrations.push.real import RealPushClient
from app.integrations.sms.base import SmsClient
from app.integrations.sms.fake import FakeSmsClient
from app.integrations.sms.real import RealSmsClient


@dataclass(frozen=True)
class Clients:
    """The bundle of integration clients wired for the active environment."""

    gateway: PaymentGateway
    email: EmailClient
    sms: SmsClient
    push: PushClient


def build_clients(settings: Settings) -> Clients:
    """Construct the integration clients for the active environment.

    In production, returns only Real clients. In development/test, returns Fakes.
    Selecting a Real client in production requires its config, which boot validation
    has already guaranteed present.

    Raises:
        RuntimeError: A fake would be selected in production (defence in depth).
    """
    if settings.is_production:
        return _build_production_clients(settings)
    return _build_fake_clients(settings.ENVIRONMENT)


def _build_production_clients(settings: Settings) -> Clients:
    assert settings.PAYLINK_API_ID is not None
    assert settings.PAYLINK_SECRET_KEY is not None
    assert settings.PAYLINK_WEBHOOK_SECRET is not None
    assert settings.SNDR_API_KEY is not None
    gateway = RealPaylinkClient(
        api_id=settings.PAYLINK_API_ID.get_secret_value(),
        secret_key=settings.PAYLINK_SECRET_KEY.get_secret_value(),
        webhook_secret=settings.PAYLINK_WEBHOOK_SECRET.get_secret_value(),
    )
    email = SndrEmailClient(
        base_url=settings.SNDR_BASE_URL or "",
        api_key=settings.SNDR_API_KEY.get_secret_value(),
        from_email=settings.SNDR_FROM_EMAIL or "",
        from_name=settings.SNDR_FROM_NAME or "",
    )
    sms = RealSmsClient(
        provider_key=(
            settings.SMS_PROVIDER_KEY.get_secret_value() if settings.SMS_PROVIDER_KEY else ""
        ),
        base_url=settings.SUPABASE_URL or "",
    )
    push = RealPushClient(
        supabase_url=settings.SUPABASE_URL or "",
        service_key=(
            settings.SUPABASE_SERVICE_KEY.get_secret_value()
            if settings.SUPABASE_SERVICE_KEY
            else ""
        ),
    )
    return Clients(gateway=gateway, email=email, sms=sms, push=push)


def _build_fake_clients(environment: Environment) -> Clients:
    return Clients(
        gateway=FakePaylinkClient(environment),
        email=FakeEmailClient(environment),
        sms=FakeSmsClient(environment),
        push=FakePushClient(environment),
    )
