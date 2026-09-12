"""Four layers, per setting: flag > TDSYNC_* > config file > default.

Anything from the environment is converted here and nowhere else. A bad
value from the environment is a refusal that says what the run uses
instead; the same bad value typed as a flag is a usage error, because a
person who typed it wants to know they typed it wrong.
"""

import os
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

DEFAULTS = {
    "nas_root": None,
    "nas_host": None,
    "batch_size": 5,
    "reclaim_after": timedelta(hours=6),
    "project_url": None,
    "ids_file": Path("project-board-ids.json"),
    "work_dir": Path("work"),
    "metadata_csv": Path("metadata_livre.csv"),
    "persons_csv": Path("metadata_personne.csv"),
}


class SettingsError(Exception):
    """A value typed on the command line that cannot be used."""


def _positive_int(raw):
    value = int(raw)
    if value < 1:
        raise ValueError(f"must be 1 or more, not {value}")
    return value


def _duration(raw):
    text = str(raw).strip().lower()
    unit = {"h": 3600, "m": 60, "s": 1}.get(text[-1:])
    if unit is None:
        raise ValueError(f"expected a duration like '6h', not {raw!r}")
    amount = float(text[:-1])
    if amount <= 0:
        raise ValueError(f"must be positive, not {raw!r}")
    return timedelta(seconds=amount * unit)


CONVERTERS = {
    "batch_size": _positive_int,
    "reclaim_after": _duration,
    "nas_root": Path,
    "ids_file": Path,
    "work_dir": Path,
    "nas_host": str,
    "project_url": str,
    "metadata_csv": Path,
    "persons_csv": Path,
}


@dataclass
class Settings:
    values: dict
    origins: dict
    refusals: list = field(default_factory=list)

    def origin(self, name):
        return self.origins[name]

    def __getattr__(self, name):
        try:
            return self.values[name]
        except KeyError:
            raise AttributeError(name) from None


def resolve(flags, config_file=None):
    from_file = {}
    if config_file and Path(config_file).is_file():
        from_file = tomllib.loads(Path(config_file).read_text("utf-8"))

    values, origins, refusals = {}, {}, []
    for name, default in DEFAULTS.items():
        convert = CONVERTERS[name]

        if flags.get(name) is not None:
            try:
                values[name], origins[name] = convert(flags[name]), "flag"
                continue
            except ValueError as why:
                # Typed by a person: tell them, do not paper over it.
                raise SettingsError(f"--{name.replace('_', '-')}: {why}") from None

        env_name = f"TDSYNC_{name.upper()}"
        for raw, where, label in ((os.environ.get(env_name), "env", env_name),
                                  (from_file.get(name), "file", f"{name} in the config file")):
            if raw is None:
                continue
            try:
                values[name], origins[name] = convert(raw), where
                break
            except ValueError as why:
                refusals.append(
                    f"{label}: {why} — using {default!r} instead")
        else:
            values[name], origins[name] = default, "default"

    return Settings(values=values, origins=origins, refusals=refusals)
