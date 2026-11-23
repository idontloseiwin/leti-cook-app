"""This distribution supports only the explicitly configured self-hosted profile."""
from __future__ import annotations
import os
from collections.abc import Mapping


def selected_deployment_profile(environ: Mapping[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    if values.get("LIC_DEPLOYMENT_PROFILE", "").strip() != "self-hosted":
        raise RuntimeError("self_hosted_deployment_profile_required")
    return "self-hosted"
