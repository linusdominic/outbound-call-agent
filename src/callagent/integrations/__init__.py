"""Ways for other voice systems to use the call engine. See ``docs/integrations``."""

from fastapi import APIRouter

from callagent.service import CallService


def routers(service: CallService, api_key: str | None) -> list[APIRouter]:
    from callagent.integrations import openai_compat, platforms, rest

    profiles = [
        openai_compat.generic_profile(),
        openai_compat.elevenlabs_profile(),
        openai_compat.vapi_profile(),
    ]
    return [
        rest.router(service, api_key),
        openai_compat.router(service, api_key, profiles),
        platforms.router(service, api_key),
    ]
