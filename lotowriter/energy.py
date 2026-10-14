"""Energy Source Legend configuration (config/energy_types.json)."""
import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "energy_types.json"


@dataclass(frozen=True)
class EnergyType:
    code: str
    name: str
    fill: str
    pattern: str
    pattern_color: str
    text_color: str
    units: tuple
    also_covers: str


def load_energy_types(path=DEFAULT_CONFIG):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    types = {}
    for t in data["types"]:
        et = EnergyType(
            code=t["code"],
            name=t["name"],
            fill=t["fill"],
            pattern=t["pattern"],
            pattern_color=t["pattern_color"],
            text_color=t["text_color"],
            units=tuple(t.get("units", [])),
            also_covers=t.get("also_covers", ""),
        )
        types[et.code] = et
    return types
