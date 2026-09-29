"""Resolução central das políticas energéticas do PiBook."""

RECOMMENDED = {
    "mains": {
        "sleep": False,
        "network_reading": True,
        "network_sleep": True,
    },
    "battery": {
        "sleep": True,
        "network_reading": True,
        "network_sleep": True,
    },
    "powersave": {
        "sleep": True,
        "network_reading": False,
        "network_sleep": False,
    },
}

VALID_PROFILES = {"mains", "battery", "powersave"}


def normalize_profile(profile):
    profile = str(profile or "battery").strip().lower()
    return profile if profile in VALID_PROFILES else "battery"


def profile_settings(settings, profile):
    profile = normalize_profile(profile)
    profiles = settings.get("power_profiles", {})
    value = profiles.get(profile, {}) if isinstance(profiles, dict) else {}
    return value if isinstance(value, dict) else {}


def resolve_switch(settings, profile, key):
    profile = normalize_profile(profile)
    configured = str(
        profile_settings(settings, profile).get(key, "auto")
    ).strip().lower()

    if configured == "on":
        return True
    if configured == "off":
        return False

    return bool(RECOMMENDED[profile][key])


def sleep_policy(settings, profile):
    data = profile_settings(settings, profile)
    enabled = resolve_switch(settings, profile, "sleep")

    try:
        timeout = int(data.get("sleep_timeout", 300))
    except (TypeError, ValueError):
        timeout = 300

    return enabled, max(30, min(3600, timeout))


def network_reading_enabled(settings, profile):
    return resolve_switch(
        settings,
        profile,
        "network_reading",
    )


def network_sleep_enabled(settings, profile):
    return resolve_switch(
        settings,
        profile,
        "network_sleep",
    )


def reader_prefetch(settings, profile):
    data = profile_settings(settings, profile)

    try:
        radius = int(data.get("reader_prefetch", 3))
    except (TypeError, ValueError):
        radius = 3

    return max(0, min(20, radius))


def resolved_profile(settings, profile):
    profile = normalize_profile(profile)
    sleep_enabled, sleep_timeout = sleep_policy(
        settings,
        profile,
    )

    return {
        "profile": profile,
        "sleep_enabled": sleep_enabled,
        "sleep_timeout": sleep_timeout,
        "network_reading": network_reading_enabled(
            settings,
            profile,
        ),
        "network_sleep": network_sleep_enabled(
            settings,
            profile,
        ),
        "reader_prefetch": reader_prefetch(
            settings,
            profile,
        ),
    }


def effective_profile(
    settings,
    power_source,
    percentage,
    current_profile=None,
):
    """Resolve the effective profile from mode, source and battery SOC."""
    mode = str(
        settings.get("power_mode", "auto")
    ).strip().lower()

    if mode in VALID_PROFILES:
        return mode

    # Automatic mode: do not guess while the source is unknown.
    if power_source not in {"mains", "battery"}:
        if current_profile in VALID_PROFILES:
            return current_profile
        return None

    if power_source == "mains":
        return "mains"

    auto_powersave = bool(
        settings.get("auto_powersave_enabled", True)
    )

    try:
        threshold = int(
            settings.get("auto_powersave_threshold", 20)
        )
    except (TypeError, ValueError):
        threshold = 20

    threshold = max(5, min(80, threshold))
    restore_threshold = min(100, threshold + 5)

    if not auto_powersave:
        return "battery"

    try:
        percentage = int(percentage)
    except (TypeError, ValueError):
        return "battery"

    # Hysteresis: once in powersave, remain there until threshold + 5.
    if current_profile == "powersave":
        if percentage < restore_threshold:
            return "powersave"
        return "battery"

    if percentage <= threshold:
        return "powersave"

    return "battery"
