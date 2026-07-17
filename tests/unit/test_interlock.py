"""Tests for the §5.2 production safety interlock across its layers."""

from __future__ import annotations

import pytest
from app.core.config import Environment
from app.integrations._guard import ProductionFakeError
from app.integrations.email.fake import FakeEmailClient
from app.integrations.paylink.fake import FakePaylinkClient
from app.integrations.push.fake import FakePushClient
from app.integrations.sms.fake import FakeSmsClient


@pytest.mark.parametrize(
    "fake_cls",
    [FakePaylinkClient, FakeEmailClient, FakeSmsClient, FakePushClient],
)
def test_fakes_refuse_construction_in_production(fake_cls: type) -> None:
    with pytest.raises(ProductionFakeError):
        fake_cls(Environment.PRODUCTION)


@pytest.mark.parametrize(
    "fake_cls",
    [FakePaylinkClient, FakeEmailClient, FakeSmsClient, FakePushClient],
)
def test_fakes_construct_in_test(fake_cls: type) -> None:
    assert fake_cls(Environment.TEST) is not None
