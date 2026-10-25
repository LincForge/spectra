"""Input validation for values that reach a shell or a debugger prompt.

``adb shell <args>`` joins its arguments into one string that the device
re-parses with ``sh -c``, so an unvalidated package name is device-side
command injection. jdb reads commands line by line from stdin, so a method
or class name containing a line break injects jdb commands. Everything that
flows into either sink is checked here first (linc-spectra#47).
"""

from __future__ import annotations

import re

# Maximum lengths checked BEFORE regex evaluation.
_MAX_PACKAGE_LEN = 255
_MAX_FQN_LEN = 1024
_MAX_SERIAL_LEN = 128

# Android application id: dot-separated segments, each starting with a
# letter, letters/digits/underscore only, at least two segments.
_PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")

# Apple bundle id: dot-separated segments of letters, digits and hyphens,
# each segment starting with an alphanumeric character (never '-').
_BUNDLE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9][A-Za-z0-9-]*)+")

# Android device serial: starts alphanumeric (never an option flag), followed
# by up to 127 characters from [A-Za-z0-9._:-] (covers emulator-5554, host:port,
# and adb mDNS names).
_ANDROID_SERIAL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

# iOS Simulator UDID: canonical 8-4-4-4-12 hex UUID (either case) or 'booted'.
_SIMULATOR_UDID_RE = re.compile(
    r"(?:booted|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"
)

# JVM fully-qualified class: dot-separated Java identifiers (``$`` allowed
# for nested/synthetic names), at least two segments.
_IDENT = r"[A-Za-z_$][A-Za-z0-9_$]*"
_FQN_RE = re.compile(rf"{_IDENT}(?:\.{_IDENT})+")

# jdb ``stop in`` target: <class>.<method>, where the method may be <init> /
# <clinit>, optionally with the argument-type list jdb needs to pick one
# overload, e.g. ``com.app.Repo.save(java.lang.String,int[])``. No spaces:
# jdb splits its command line on whitespace.
_TYPE = rf"{_IDENT}(?:\.{_IDENT})*(?:\[\])*"
_METHOD_FQN_RE = re.compile(
    rf"{_IDENT}(?:\.{_IDENT})*\.(?:{_IDENT}|<init>|<clinit>)"
    rf"(?:\((?:{_TYPE}(?:,{_TYPE})*)?\))?"
)


class InvalidInputError(ValueError):
    """A caller-supplied identifier failed validation."""


def _check(
    pattern: re.Pattern[str],
    value: str,
    field: str,
    expected: str,
    max_len: int,
) -> str:
    if not isinstance(value, str) or len(value) > max_len or pattern.fullmatch(value) is None:
        raise InvalidInputError(f"invalid {field} {value!r}: expected {expected}")
    return value


def validate_package(package: str) -> str:
    """Return *package* if it is a well-formed Android package name."""
    return _check(
        _PACKAGE_RE,
        package,
        "package",
        "an Android package like com.example.app",
        _MAX_PACKAGE_LEN,
    )


def validate_bundle_id(bundle_id: str) -> str:
    """Return *bundle_id* if it is a well-formed iOS bundle identifier."""
    return _check(
        _BUNDLE_ID_RE,
        bundle_id,
        "bundle id",
        "an iOS bundle id like com.example.my-app",
        _MAX_PACKAGE_LEN,
    )


def validate_android_serial(serial: str) -> str:
    """Return *serial* if it is a well-formed Android device serial."""
    return _check(
        _ANDROID_SERIAL_RE,
        serial,
        "device_serial",
        "an Android device serial like emulator-5554",
        _MAX_SERIAL_LEN,
    )


def validate_simulator_udid(udid: str) -> str:
    """Return *udid* if it is a Simulator UUID or ``'booted'``."""
    return _check(
        _SIMULATOR_UDID_RE,
        udid,
        "device_serial",
        "an iOS Simulator UDID (UUID or 'booted')",
        _MAX_SERIAL_LEN,
    )


def validate_device_id(device_id: str, platform: str) -> str:
    """Validate an Android serial or iOS Simulator UDID for *platform*."""
    if platform == "ios":
        return validate_simulator_udid(device_id)
    return validate_android_serial(device_id)


def validate_fqn(fqn: str, field: str) -> str:
    """Return *fqn* if it is a dotted JVM class/method name (no parentheses)."""
    return _check(
        _FQN_RE,
        fqn,
        field,
        "a fully-qualified name like com.app.Clazz.method",
        _MAX_FQN_LEN,
    )


def validate_method_fqn(method_fqn: str) -> str:
    """Return *method_fqn* if it is a jdb method target.

    ``package.Class.method``, ``<init>`` / ``<clinit>`` allowed, with an
    optional argument-type list for overloads: ``com.app.Repo.save(int)``.
    """
    return _check(
        _METHOD_FQN_RE,
        method_fqn,
        "method_fqn",
        "package.Class.method, optionally with jdb argument types like "
        "com.app.Repo.save(java.lang.String)",
        _MAX_FQN_LEN,
    )


def validate_app_id(app_id: str, platform: str) -> str:
    """Validate a package (android) or bundle id (ios) for *platform*."""
    if platform == "ios":
        return validate_bundle_id(app_id)
    return validate_package(app_id)
